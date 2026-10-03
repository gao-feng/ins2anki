#!/usr/bin/env python3
"""Minimal, dependency-free Chrome DevTools Protocol client.

Route 2 of the sync design reuses the browser the user is already logged into
instead of exporting cookies. Everything here is standard library only, so the
repository keeps its "stdlib plus external binaries" footprint:

* :func:`version`, :func:`list_pages`, :func:`open_tab`, :func:`close_tab`
  talk to the HTTP discovery endpoint (``http://127.0.0.1:9222`` by default).
* :class:`WebSocket` implements just enough RFC 6455 (client masking, text
  frames, ping/pong, continuation frames) to hold a DevTools connection.
* :class:`CdpSession` speaks ``Runtime.evaluate`` / ``Page.navigate`` and
  buffers domain events.

Why not Playwright: the scripts must run wherever ``python3`` and ``yt-dlp``
already run, including the project virtualenv that ships nothing else.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import socket
import ssl
import struct
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterable


DEFAULT_ENDPOINT = "http://127.0.0.1:9222"
_WS_GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
_OPCODE_CONTINUATION = 0x0
_OPCODE_TEXT = 0x1
_OPCODE_BINARY = 0x2
_OPCODE_CLOSE = 0x8
_OPCODE_PING = 0x9
_OPCODE_PONG = 0xA


class CdpError(RuntimeError):
    """Raised when the browser cannot be reached or a command fails."""


# --------------------------------------------------------------------------
# HTTP discovery endpoints
# --------------------------------------------------------------------------


def _http_json(
    endpoint: str,
    path: str,
    method: str = "GET",
    timeout: float = 10.0,
) -> Any:
    url = endpoint.rstrip("/") + path
    request = urllib.request.Request(url, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        raise CdpError(f"{method} {url} failed: HTTP {exc.code}") from exc
    except (urllib.error.URLError, OSError) as exc:
        raise CdpError(
            f"cannot reach the DevTools endpoint at {endpoint}: {exc}. "
            "Start the browser with --remote-debugging-port, see "
            "browser_sync.py launch."
        ) from exc
    if not body:
        return None
    try:
        return json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise CdpError(f"{url} returned invalid JSON") from exc


def version(endpoint: str = DEFAULT_ENDPOINT, timeout: float = 5.0) -> dict:
    """Return the browser handshake payload (``Browser``, ``webSocketDebuggerUrl``)."""
    payload = _http_json(endpoint, "/json/version", timeout=timeout)
    if not isinstance(payload, dict):
        raise CdpError(f"{endpoint} does not look like a DevTools endpoint")
    return payload


def list_pages(endpoint: str = DEFAULT_ENDPOINT, timeout: float = 5.0) -> list[dict]:
    """Return every open page target, newest first."""
    targets = _http_json(endpoint, "/json/list", timeout=timeout)
    if not isinstance(targets, list):
        raise CdpError(f"{endpoint}/json/list did not return a list")
    pages = [
        target
        for target in targets
        if isinstance(target, dict)
        and target.get("type") == "page"
        and target.get("webSocketDebuggerUrl")
    ]
    return pages


def find_page(
    endpoint: str = DEFAULT_ENDPOINT,
    url_prefix: str | None = None,
    timeout: float = 5.0,
) -> dict | None:
    """Return the first page whose URL starts with ``url_prefix``."""
    for page in list_pages(endpoint, timeout=timeout):
        if url_prefix is None or str(page.get("url", "")).startswith(url_prefix):
            return page
    return None


def open_tab(
    url: str,
    endpoint: str = DEFAULT_ENDPOINT,
    timeout: float = 15.0,
) -> dict:
    """Open ``url`` in a new tab and return its target descriptor.

    Chromium requires ``PUT`` since M111; ``GET`` is retried for older builds.
    """
    query = urllib.parse.urlencode({"url": url})
    try:
        target = _http_json(endpoint, f"/json/new?{query}", method="PUT", timeout=timeout)
    except CdpError:
        target = _http_json(endpoint, f"/json/new?{query}", method="GET", timeout=timeout)
    if not isinstance(target, dict) or not target.get("webSocketDebuggerUrl"):
        raise CdpError(f"cannot open a tab for {url}")
    return target


def close_tab(target_id: str, endpoint: str = DEFAULT_ENDPOINT, timeout: float = 5.0) -> None:
    """Close a tab, ignoring tabs that already disappeared."""
    try:
        _http_json(endpoint, f"/json/close/{target_id}", timeout=timeout)
    except CdpError:
        pass


def activate_tab(target_id: str, endpoint: str = DEFAULT_ENDPOINT, timeout: float = 5.0) -> None:
    """Bring a tab to the front, ignoring targets that vanished."""
    try:
        _http_json(endpoint, f"/json/activate/{target_id}", timeout=timeout)
    except CdpError:
        pass


# --------------------------------------------------------------------------
# WebSocket transport
# --------------------------------------------------------------------------


class WebSocket:
    """A tiny RFC 6455 client: text frames, ping/pong and fragmentation."""

    def __init__(self, url: str, timeout: float = 30.0):
        self.url = url
        self.timeout = timeout
        self._socket: socket.socket | None = None
        self._buffer = bytearray()
        self._closed = False

    # -- connection ------------------------------------------------------

    def connect(self) -> None:
        parts = urllib.parse.urlsplit(self.url)
        if parts.scheme not in ("ws", "wss"):
            raise CdpError(f"unsupported WebSocket scheme: {self.url}")
        host = parts.hostname or "127.0.0.1"
        port = parts.port or (443 if parts.scheme == "wss" else 80)
        path = parts.path or "/"
        if parts.query:
            path += f"?{parts.query}"

        try:
            raw = socket.create_connection((host, port), timeout=self.timeout)
        except OSError as exc:
            raise CdpError(f"cannot connect to {self.url}: {exc}") from exc
        if parts.scheme == "wss":
            context = ssl.create_default_context()
            raw = context.wrap_socket(raw, server_hostname=host)
        raw.settimeout(self.timeout)
        self._socket = raw

        key = base64.b64encode(os.urandom(16)).decode("ascii")
        handshake = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        )
        raw.sendall(handshake.encode("ascii"))

        header = self._read_until(b"\r\n\r\n")
        first_line = header.split(b"\r\n", 1)[0].decode("latin1", "replace")
        if "101" not in first_line:
            raise CdpError(f"WebSocket handshake rejected: {first_line}")
        expected = base64.b64encode(
            hashlib.sha1((key + _WS_GUID).encode("ascii")).digest()
        ).decode("ascii")
        if expected.lower() not in header.decode("latin1", "replace").lower():
            raise CdpError("WebSocket handshake returned an unexpected accept key")

    def close(self) -> None:
        if self._socket is None:
            return
        if not self._closed:
            try:
                self._send_frame(_OPCODE_CLOSE, b"")
            except (OSError, CdpError):
                pass
            self._closed = True
        try:
            self._socket.close()
        finally:
            self._socket = None

    def __enter__(self) -> "WebSocket":
        self.connect()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- framing ---------------------------------------------------------

    def _read_until(self, marker: bytes) -> bytes:
        while marker not in self._buffer:
            chunk = self._recv_raw()
            if not chunk:
                raise CdpError("WebSocket closed during the handshake")
            self._buffer.extend(chunk)
        index = self._buffer.index(marker) + len(marker)
        data = bytes(self._buffer[:index])
        del self._buffer[:index]
        return data

    def _recv_raw(self) -> bytes:
        if self._socket is None:
            raise CdpError("WebSocket is not connected")
        try:
            return self._socket.recv(65536)
        except socket.timeout as exc:
            raise CdpError("WebSocket read timed out") from exc
        except OSError as exc:
            raise CdpError(f"WebSocket read failed: {exc}") from exc

    def _read_exact(self, count: int, deadline: float) -> bytes:
        while len(self._buffer) < count:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CdpError("WebSocket read timed out")
            if self._socket is None:
                raise CdpError("WebSocket is not connected")
            self._socket.settimeout(max(remaining, 0.05))
            chunk = self._recv_raw()
            if not chunk:
                raise CdpError("WebSocket closed by the browser")
            self._buffer.extend(chunk)
        data = bytes(self._buffer[:count])
        del self._buffer[:count]
        return data

    def _read_frame(self, deadline: float) -> tuple[bool, int, bytes]:
        header = self._read_exact(2, deadline)
        fin = bool(header[0] & 0x80)
        opcode = header[0] & 0x0F
        masked = bool(header[1] & 0x80)
        length = header[1] & 0x7F
        if length == 126:
            length = struct.unpack(">H", self._read_exact(2, deadline))[0]
        elif length == 127:
            length = struct.unpack(">Q", self._read_exact(8, deadline))[0]
        mask = self._read_exact(4, deadline) if masked else b""
        payload = self._read_exact(length, deadline) if length else b""
        if masked:
            payload = bytes(
                byte ^ mask[index % 4] for index, byte in enumerate(payload)
            )
        return fin, opcode, payload

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        if self._socket is None:
            raise CdpError("WebSocket is not connected")
        header = bytearray()
        header.append(0x80 | opcode)
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack(">H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack(">Q", length))
        mask = os.urandom(4)
        header.extend(mask)
        masked = bytes(byte ^ mask[index % 4] for index, byte in enumerate(payload))
        try:
            self._socket.sendall(bytes(header) + masked)
        except OSError as exc:
            raise CdpError(f"WebSocket write failed: {exc}") from exc

    def send_text(self, text: str) -> None:
        self._send_frame(_OPCODE_TEXT, text.encode("utf-8"))

    def recv_text(self, timeout: float | None = None) -> str:
        """Return the next text message, transparently handling control frames."""
        deadline = time.monotonic() + (timeout if timeout is not None else self.timeout)
        fragments = bytearray()
        while True:
            fin, opcode, payload = self._read_frame(deadline)
            if opcode == _OPCODE_PING:
                self._send_frame(_OPCODE_PONG, payload)
                continue
            if opcode == _OPCODE_PONG:
                continue
            if opcode == _OPCODE_CLOSE:
                self._closed = True
                raise CdpError("the browser closed the DevTools connection")
            if opcode in (_OPCODE_TEXT, _OPCODE_BINARY, _OPCODE_CONTINUATION):
                fragments.extend(payload)
                if fin:
                    return fragments.decode("utf-8", "replace")
                continue
            raise CdpError(f"unsupported WebSocket opcode: {opcode}")


# --------------------------------------------------------------------------
# DevTools session
# --------------------------------------------------------------------------


class CdpSession:
    """A command channel to one page target."""

    def __init__(self, ws_url: str, timeout: float = 30.0):
        self.ws_url = ws_url
        self.timeout = timeout
        self._socket: WebSocket | None = None
        self._next_id = 0
        self.events: list[dict] = []

    # -- lifecycle -------------------------------------------------------

    def connect(self) -> None:
        self._socket = WebSocket(self.ws_url, timeout=self.timeout)
        self._socket.connect()

    def close(self) -> None:
        if self._socket is not None:
            self._socket.close()
            self._socket = None

    def __enter__(self) -> "CdpSession":
        self.connect()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # -- protocol --------------------------------------------------------

    def call(
        self,
        method: str,
        params: dict | None = None,
        timeout: float | None = None,
    ) -> dict:
        """Send a command and return its ``result`` payload.

        Domain events received while waiting are appended to :attr:`events`.
        """
        if self._socket is None:
            raise CdpError("CDP session is not connected")
        self._next_id += 1
        message_id = self._next_id
        payload = {"id": message_id, "method": method}
        if params:
            payload["params"] = params
        self._socket.send_text(json.dumps(payload))

        deadline = time.monotonic() + (timeout if timeout is not None else self.timeout)
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise CdpError(f"{method} timed out after {timeout or self.timeout}s")
            message = json.loads(self._socket.recv_text(timeout=remaining))
            if not isinstance(message, dict):
                continue
            if message.get("id") != message_id:
                if "method" in message:
                    self.events.append(message)
                continue
            error = message.get("error")
            if error:
                detail = error.get("message") if isinstance(error, dict) else str(error)
                raise CdpError(f"{method} failed: {detail}")
            result = message.get("result")
            return result if isinstance(result, dict) else {}

    def drain_events(self) -> list[dict]:
        """Return and clear the buffered domain events."""
        events, self.events = self.events, []
        return events

    def evaluate(
        self,
        expression: str,
        await_promise: bool = True,
        return_by_value: bool = True,
        timeout: float | None = None,
    ) -> Any:
        """Evaluate JavaScript in the page and return its JSON value."""
        result = self.call(
            "Runtime.evaluate",
            {
                "expression": expression,
                "awaitPromise": await_promise,
                "returnByValue": return_by_value,
                "userGesture": True,
                "allowUnsafeEvalBlockedByCSP": True,
            },
            timeout=timeout,
        )
        if result.get("exceptionDetails"):
            raise CdpError(_exception_message(result["exceptionDetails"]))
        return result.get("result", {}).get("value")

    def navigate(self, url: str, timeout: float = 45.0) -> None:
        """Navigate the tab and wait until the document reports ``complete``."""
        self.call("Page.enable", timeout=min(timeout, self.timeout))
        self.call("Page.navigate", {"url": url}, timeout=timeout)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                state = self.evaluate(
                    "document.readyState", await_promise=False, timeout=10.0
                )
            except CdpError:
                state = None
            if state == "complete":
                return
            time.sleep(0.25)
        raise CdpError(f"page did not finish loading in {timeout}s: {url}")


def _exception_message(details: dict) -> str:
    """Flatten a ``Runtime.evaluate`` exception into a readable message."""
    exception = details.get("exception")
    if isinstance(exception, dict):
        description = exception.get("description") or exception.get("value")
        if description:
            first_line = str(description).strip().splitlines()[0]
            if len(first_line) > 240:
                first_line = first_line[:237] + "..."
            return f"page JavaScript failed: {first_line}"
    text = details.get("text") or "unknown page JavaScript error"
    return f"page JavaScript failed: {text}"


def page_websocket_url(target: dict) -> str:
    """Extract the per-tab WebSocket URL from a target descriptor."""
    value = target.get("webSocketDebuggerUrl")
    if not isinstance(value, str) or not value:
        raise CdpError("target descriptor has no webSocketDebuggerUrl")
    return value


def wait_for_endpoint(
    endpoint: str = DEFAULT_ENDPOINT,
    timeout: float = 30.0,
    interval: float = 0.5,
) -> dict:
    """Poll the DevTools endpoint until it answers, then return its version."""
    deadline = time.monotonic() + timeout
    last: Exception | None = None
    while time.monotonic() < deadline:
        try:
            return version(endpoint, timeout=min(2.0, timeout))
        except CdpError as exc:
            last = exc
            time.sleep(interval)
    raise CdpError(f"DevTools endpoint {endpoint} never became ready: {last}")


def iter_events(events: Iterable[dict], method: str) -> list[dict]:
    """Filter buffered events by CDP method name."""
    return [event for event in events if event.get("method") == method]

#!/usr/bin/env python3
"""Platform adapters for recognizing and canonicalizing post URLs.

Supported platforms:

* ``instagram``   — posts, reels and TV items.
* ``xiaohongshu`` — notes under ``/explore/`` or ``/discovery/item/``.
* ``douyin``      — videos under ``/video/`` and image notes under ``/note/``.

``yt-dlp`` can only fetch single items on these platforms; none of them expose
a machine-readable favorites feed. Enumeration therefore comes from a browser
inventory, while this module keeps the per-item URLs canonical and stable.

Only :func:`resolve_short_link` performs network I/O. Everything else is pure
so it can be unit tested without a live session.
"""

from __future__ import annotations

import re
import urllib.error
import urllib.request
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


PLATFORMS = ("instagram", "xiaohongshu", "douyin")

# Xiaohongshu needs ``xsec_token`` on most note URLs; the token is short lived
# but required, so it must survive normalization instead of being stripped like
# tracking parameters.
XHS_KEEP_QUERY = frozenset({"xsec_token", "xsec_source"})

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"
)

_INSTAGRAM_RE = re.compile(
    r"https?://(?:www\.)?instagram\.com/(?:p|reel|tv)/([A-Za-z0-9_-]+)",
    re.IGNORECASE,
)
_XHS_RE = re.compile(
    r"https?://(?:www\.)?xiaohongshu\.com/(?:explore|discovery/item)/([\da-fA-F]{8,32})",
    re.IGNORECASE,
)
_DOUYIN_VIDEO_RE = re.compile(
    r"https?://(?:(?:www|m)\.)?(?:douyin|iesdouyin)\.com/(?:share/)?video/(\d+)",
    re.IGNORECASE,
)
_DOUYIN_NOTE_RE = re.compile(
    r"https?://(?:(?:www|m)\.)?douyin\.com/note/(\d+)",
    re.IGNORECASE,
)

_XHSLINK_RE = re.compile(r"https?://(?:www\.)?xhslink\.com/\S+", re.IGNORECASE)
_DOUYIN_SHORT_RE = re.compile(
    r"https?://(?:v|vm)\.douyin\.com/\S+", re.IGNORECASE
)


def detect_platform(value: str) -> str | None:
    """Return the platform for a URL, or ``None`` when it is unsupported."""
    url = (value or "").strip()
    if _INSTAGRAM_RE.search(url):
        return "instagram"
    if _XHS_RE.search(url):
        return "xiaohongshu"
    if _DOUYIN_VIDEO_RE.search(url) or _DOUYIN_NOTE_RE.search(url):
        return "douyin"
    if _XHSLINK_RE.search(url):
        return "xiaohongshu"
    if _DOUYIN_SHORT_RE.search(url):
        return "douyin"
    return None


def is_short_link(value: str) -> bool:
    """Report whether a URL is a share link that needs an HTTP redirect."""
    url = (value or "").strip()
    return bool(_XHSLINK_RE.search(url) or _DOUYIN_SHORT_RE.search(url))


def _with_kept_query(url: str, keep: frozenset[str]) -> str:
    parts = urlsplit(url)
    kept = [(key, value) for key, value in parse_qsl(parts.query) if key in keep]
    return urlunsplit(
        (parts.scheme or "https", parts.netloc, parts.path, urlencode(kept), "")
    )


def normalize(value: str, platform: str | None = None) -> tuple[str, str] | None:
    """Return ``(item_id, canonical_url)`` for a supported post URL.

    ``platform`` narrows the accepted input; when omitted the platform is
    detected from the URL itself.
    """
    url = (value or "").strip()
    if not url:
        return None
    resolved = platform or detect_platform(url)
    if resolved == "instagram":
        match = _INSTAGRAM_RE.search(url)
        if not match:
            return None
        code = match.group(1)
        return code, f"https://www.instagram.com/p/{code}/"
    if resolved == "xiaohongshu":
        match = _XHS_RE.search(url)
        if not match:
            return None
        note_id = match.group(1)
        canonical = _with_kept_query(
            f"https://www.xiaohongshu.com/explore/{note_id}",
            XHS_KEEP_QUERY,
        )
        # Preserve a token that was supplied inline on the discovery URL.
        inline = _with_kept_query(url, XHS_KEEP_QUERY)
        if "?" in inline:
            canonical = inline
        return note_id, canonical
    if resolved == "douyin":
        match = _DOUYIN_VIDEO_RE.search(url)
        if match:
            video_id = match.group(1)
            return video_id, f"https://www.douyin.com/video/{video_id}"
        match = _DOUYIN_NOTE_RE.search(url)
        if match:
            note_id = match.group(1)
            return note_id, f"https://www.douyin.com/note/{note_id}"
        return None
    return None


def normalize_many(
    values: list[str], platform: str | None = None
) -> list[tuple[str, str, str]]:
    """Normalize and deduplicate URLs, returning ``(platform, id, url)``.

    Duplicates are collapsed by ``(platform, id)``. The first occurrence wins,
    so a token-bearing URL is kept over a bare one.
    """
    result: list[tuple[str, str, str]] = []
    seen: set[tuple[str, str]] = set()
    for value in values:
        resolved = platform or detect_platform(value or "")
        if resolved not in PLATFORMS:
            continue
        normalized = normalize(value, resolved)
        if not normalized:
            continue
        item_id, canonical = normalized
        key = (resolved, item_id)
        if key in seen:
            continue
        seen.add(key)
        result.append((resolved, item_id, canonical))
    return result


def resolve_short_link(value: str, timeout: float = 15.0) -> str:
    """Follow a share-link redirect and return the final URL.

    Raises ``RuntimeError`` when the link cannot be resolved. Callers should
    fall back to the original URL rather than dropping the item.
    """
    url = (value or "").strip()
    request = urllib.request.Request(
        url,
        headers={"User-Agent": USER_AGENT, "Accept": "*/*"},
    )
    for method in ("HEAD", "GET"):
        request.method = method
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                return response.geturl()
        except (urllib.error.URLError, OSError, ValueError) as exc:
            last = exc
    raise RuntimeError(f"cannot resolve short link {url}: {last}")


def resolve_short_links(values: list[str], timeout: float = 15.0) -> list[str]:
    """Resolve every short link in ``values``, leaving other URLs untouched."""
    resolved: list[str] = []
    for value in values:
        if not is_short_link(value):
            resolved.append(value)
            continue
        try:
            resolved.append(resolve_short_link(value, timeout=timeout))
        except RuntimeError:
            resolved.append(value)
    return resolved


def collection_suffix(url: str) -> str:
    """Return a short, stable suffix used to disambiguate directory names."""
    match = re.search(r"/saved/_/([^/?#]+)", url or "")
    if match:
        return match.group(1)[-8:]
    host = urlsplit(url or "").netloc.lower()
    if "xiaohongshu" in host:
        match = re.search(r"/user/profile/([\da-fA-F]+)", url or "")
        if match:
            return match.group(1)[-8:]
    if "douyin" in host:
        match = re.search(r"/user/([\w.-]+)", url or "")
        if match:
            return match.group(1)[-8:]
    return ""

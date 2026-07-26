#!/usr/bin/env python3
"""Import a confirmed vocabulary selection through AnkiConnect."""

from __future__ import annotations

import argparse
import base64
import html
import json
import mimetypes
import urllib.error
import urllib.request
from pathlib import Path


def invoke(endpoint: str, action: str, params: dict | None = None):
    payload = json.dumps({"action": action, "version": 6, "params": params or {}}).encode()
    request = urllib.request.Request(endpoint, payload, {"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=20) as response:
            body = json.load(response)
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Cannot reach AnkiConnect at {endpoint}: {exc}") from exc
    if body.get("error"):
        raise RuntimeError(f"AnkiConnect {action} failed: {body['error']}")
    return body.get("result")


def upload(endpoint: str, path: Path) -> str:
    if not path.is_file():
        raise FileNotFoundError(path)
    return invoke(endpoint, "storeMediaFile", {
        "filename": path.name,
        "data": base64.b64encode(path.read_bytes()).decode("ascii"),
    })


def media_html(filename: str) -> str:
    mime = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    safe = html.escape(filename, quote=True)
    if mime.startswith("image/"):
        return f'<img src="{safe}">'
    if mime.startswith("audio/"):
        return f"[sound:{safe}]"
    if mime.startswith("video/"):
        return f'<video controls src="{safe}"></video>'
    return f'<a href="{safe}">{safe}</a>'


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--selection", required=True, type=Path)
    parser.add_argument("--endpoint", default="http://127.0.0.1:8765")
    args = parser.parse_args()
    data = json.loads(args.selection.read_text(encoding="utf-8"))
    deck = data.get("deck", "ins")
    entries = data.get("entries")
    if not isinstance(entries, list) or not entries:
        parser.error("selection must contain a non-empty entries array")

    invoke(args.endpoint, "version")
    invoke(args.endpoint, "createDeck", {"deck": deck})
    cache: dict[str, str] = {}
    notes = []
    for index, entry in enumerate(entries, 1):
        word = str(entry.get("word", "")).strip()
        meaning = str(entry.get("meaning", "")).strip()
        if not word or not meaning:
            parser.error(f"entry {index} requires word and meaning")
        meaning_html = html.escape(meaning).replace("\n", "<br>")
        blocks = [f"<div>{meaning_html}</div>"]
        front = html.escape(word)
        pron = entry.get("pronunciation")
        if pron:
            p = Path(str(pron)).resolve()
            if str(p) not in cache:
                cache[str(p)] = upload(args.endpoint, p)
            front = f"{front}[sound:{cache[str(p)]}]"
        for key, label in (("context", "Context"), ("position", "Position"), ("source_url", "Source")):
            value = str(entry.get(key, "")).strip()
            if value:
                rendered = f'<a href="{html.escape(value, quote=True)}">Instagram source</a>' if key == "source_url" else html.escape(value)
                blocks.append(f"<div><b>{label}:</b> {rendered}</div>")
        for raw_path in entry.get("media", []):
            path = Path(raw_path).resolve()
            if str(path) not in cache:
                cache[str(path)] = upload(args.endpoint, path)
            blocks.append(media_html(cache[str(path)]))
        notes.append({
            "deckName": deck,
            "modelName": "Basic",
            "fields": {"Front": front, "Back": "\n".join(blocks)},
            "options": {"allowDuplicate": False},
            "tags": entry.get("tags", ["instagram-to-anki"]),
        })

    note_ids = invoke(args.endpoint, "addNotes", {"notes": notes})
    result = {"deck": deck, "requested": len(notes), "addedNoteIds": note_ids, "uploadedMedia": sorted(set(cache.values()))}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if all(note_ids) else 2


if __name__ == "__main__":
    raise SystemExit(main())

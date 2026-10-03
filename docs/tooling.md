# Media and transcription tooling

## Download prerequisites

Install `yt-dlp` using the platform's supported package mechanism. These
platforms change frequently; use a current release. The downloader accepts a
Netscape cookies file via `--cookies` when the user authorizes access to content
visible in their logged-in session.

## Supported platforms

`scripts/download_media.py` probes each item before downloading, so it never
mistakes a video cover for the item's content:

| Platform | Accepted URLs | Notes |
| --- | --- | --- |
| `instagram` | `/p/<code>`, `/reel/<code>`, `/tv/<code>` | Saved collections can be enumerated with `--collection-url`. |
| `xiaohongshu` | `/explore/<note_id>`, `/discovery/item/<note_id>`, `xhslink.com` short links | `xsec_token` is required for most notes and is preserved on normalization. Cookies are effectively mandatory. |
| `douyin` | `/video/<id>`, `/note/<id>`, `v.douyin.com` short links | Needs fresh cookies (`s_v_web_id`); `yt-dlp` reports "Fresh cookies (not necessarily logged in) are needed" otherwise. |

Strategy selection:

- **video** — the probe finds real formats; download with
  `--write-info-json --write-description --write-thumbnail`.
- **images** — Xiaohongshu only: no video formats but a populated `imageList`;
  download the images with `--write-all-thumbnails`, then keep only each image's
  full-size `!nd_dft_...` file. The extractor lists a `!nd_prv_...` preview
  beside every image with *identical* `width`/`height`, so the URL variant — not
  the resolution, and not the file size — is what identifies it (measured over
  700 images: 21 KB vs 122 KB median). `scripts/clean_images.py --root xhs-saved`
  applies the same choice to items synced before this, and repoints their
  manifests.
- **unsupported** — everything else, including Douyin image notes, where
  `yt-dlp` exposes only a cover image. The item is marked `failed` with an
  explicit reason rather than being saved as a cover.

Naming: every downloader writes `<item id>_<title>.<n>.<ext>` into
`<output>/<item id>`, which is what keeps a retry reproducible. `sync_common`
then renames the folder to `<title>` and the files to `<title>.<n>.<ext>` (a
numbered sequence is renumbered from 1; a duplicate title becomes `<title> (2)`)
and writes `"file_names": "title"` into the manifest. The title is byte-limited
to 120 UTF-8 bytes and cut at a word boundary because a Chinese title costs
three bytes per character. A note without a title field of its own — yt-dlp
reports `XiaoHongShu video #<id>` — is named after the first line of its
description, which is what the feed shows in place of a title. Items whose
platform reports no title at all — Instagram only ever reports `Video by
<user>` — keep their shortcode. `scripts/retitle_items.py --root <tree>` applies
the same rename to a tree that was synced earlier and repoints its
`sync-state.json`. Run `clean_images.py` *before* retitling: pairing previews
needs the thumbnail ids in the old names.

None of these platforms expose an enumerable favorites feed to `yt-dlp`.

Preferred path (all three platforms): reuse the logged-in browser through CDP
instead of exporting cookies. `scripts/browser_sync.py launch` opens a dedicated
profile once, `check` verifies the session, and `sync --all-collections`
enumerates, downloads the signed CDN URLs directly, and writes the same layout
as the yt-dlp path. No cookies, no Keychain prompts, no agent. Instagram's
saved/collection listings describe a reel by its cover image only, so the
downloader asks `/api/v1/media/<pk>/info/` for the reel's direct URL before
streaming; items the API refuses go through the yt-dlp fallback.

`scripts/favorites_sync.py` does the same for 小红书 and 抖音 收藏, which have no
enumerable feed at all: it installs a harvest hook with
`Page.addScriptToEvaluateOnNewDocument` *before* the favorites page loads, scrolls
the page, and reads the JSON the page fetched for itself (note id plus
`xsec_token` on Xiaohongshu, `play_addr`/`images` URLs on Douyin) out of
`sessionStorage`. Both platforms also server-render their first page, so the same
walker is run over the boot state.

A fresh `xsec_token` is what makes a Xiaohongshu note readable: measured on the
same note, a token-bearing URL with **no** cookies returned the full image list,
while the cookie-authenticated URL **without** a token returned an empty shell.
That is why the session path needs neither `--cookies` nor
`--cookies-from-browser`; the token is short lived, so re-harvest and re-run
instead of reusing an older URL list.

Enumeration for Instagram tries three routes in order: its REST feed, then the
JSON the page fetches for itself (read off the CDP `Network` domain, which
survives Instagram retiring a REST path such as `/api/v1/collections/list/`),
then DOM link harvesting with yt-dlp as the media fallback.
`scripts/browser_sync.py diagnose` (and `favorites_sync.py diagnose`) prints the
API paths the page actually called when none of them work.

Fallback path (no dedicated window): use `scripts/browser/export_collection.js`
on the logged-in 收藏 page to produce an inventory — plus
`scripts/browser/harvest_xhs_tokens.js` for the `xsec_token` values Xiaohongshu
note links need — then feed it to `sync_saved.py --urls-file` or
`sync_collections.py --inventory`.

## Local transcription

Prefer a locally installed Whisper-compatible CLI. Examples:

```powershell
whisper media.mp4 --language English --task transcribe --output_format json --output_dir transcript
```

or, with whisper.cpp:

```powershell
ffmpeg -i media.mp4 -ar 16000 -ac 1 audio.wav
whisper-cli -m model.bin -f audio.wav -oj -of transcript
```

Keep timestamps. If no speech recognizer is available, ask permission before installing or using a remote transcription service. Never upload private media without explicit authorization.

## Anki

Anki Desktop and the AnkiConnect add-on must be running. The default endpoint is `http://127.0.0.1:8765`. The importer uses the built-in `Basic` note type and HTML5 `<video>` tags for video; playback support can vary by Anki client.

## Local pronunciation (TTS)

`scripts/tts_word.py` synthesizes an English pronunciation MP3 per word, fully local. On Windows it uses System.Speech (SAPI) for synthesis and `ffmpeg` to encode the MP3:

```powershell
python scripts/tts_word.py "shareholder" --output-dir downloads/audio
```

The script picks the first enabled `en-*` SAPI voice and writes `ig2anki_<word>.mp3` to the output dir. If no English voice is installed, install one (Windows Settings → Time & Language → Speech → Add voices) and retry. On other platforms, adapt the synth step to `say` (macOS) or `espeak-ng` (Linux).

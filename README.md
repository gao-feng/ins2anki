# ins2anki

An [opencode](https://opencode.ai) skill that turns a short-video post or reel into Anki vocabulary flashcards. It downloads the media, transcribes spoken English (or reads the caption), surfaces genuinely challenging words, and — after you confirm the selection — imports them through AnkiConnect as Basic notes with detailed Chinese meanings, IPA, local TTS pronunciation, and the original source media.

Supported sources: **Instagram** posts/reels and saved collections, **Xiaohongshu (小红书)** notes, and **Douyin (抖音)** videos. All three share one incremental sync engine.

## Workflow

1. **Collect** — `scripts/download_media.py` fetches the post with `yt-dlp` and writes `manifest.json` (media list + caption + metadata). `scripts/download_instagram.py` is a thin Instagram-pinned wrapper kept for compatibility. Pass `--cookies` / `--cookies-from-browser` for login-gated content only with the user's authorization.
2. **Transcribe & extract** — Whisper transcribes the audio with timestamps (OCR/caption for images and carousels). Challenging B2–C2 words and fixed expressions are selected with Chinese meanings, IPA, a faithful source excerpt, and a difficulty note.
3. **Confirm** — a numbered candidate list is presented; the user confirms (1) which words, (2) the meanings, (3) media attachment, and (4) whether to add TTS pronunciation. Nothing is written to Anki until the user replies.
4. **Save** — `scripts/tts_word.py` generates per-word pronunciation MP3s with local TTS; a UTF-8 `selection.json` is written per `references/selection-schema.md`; then `scripts/anki_import.py` creates the deck, uploads each media file once, and adds Basic notes — word plus optional `[sound:...]` on the front; meaning, context, position, source link, and optional media on the back.
5. **Report** — deck, note count, attached media, and any skipped duplicates or failures.

## Repository layout

```
instagram-to-anki/
  SKILL.md                    # skill definition (workflow the agent follows)
  agents/openai.yaml          # agent interface metadata
  scripts/
    platforms.py              # URL detection/normalization for all 3 platforms
    sync_common.py            # shared incremental engine + state/validation
    download_media.py         # multi-platform downloader + manifest writer
    sync_saved.py             # incremental sync CLI (--platform)
    sync_collections.py       # multi-collection inventory coordinator
    browser/
      export_collection.js    # console script: export a 收藏夹 to inventory JSON
    download_instagram.py     # thin Instagram wrapper (compatibility)
    sync_instagram_saved.py   # thin Instagram wrapper (compatibility)
    sync_instagram_collections.py # thin Instagram wrapper (compatibility)
    tts_word.py               # local TTS pronunciation (Windows SAPI + ffmpeg)
    anki_import.py            # AnkiConnect importer (notes + media)
  references/
    selection-schema.md       # confirmed-selection JSON schema
    tooling.md                # download / transcription / TTS / Anki notes
```

`downloads/` (fetched media, transcripts, `selection.json`) and `__pycache__/` are gitignored — downloaded media is never committed or redistributed.

## Prerequisites

- [opencode](https://opencode.ai) — the skill runs inside an opencode session
- Python 3.10+
- [`yt-dlp`](https://github.com/yt-dlp/yt-dlp) — Instagram download
- [`ffmpeg`](https://ffmpeg.org/) — audio extraction for transcription and MP3 encoding for TTS
- A local Whisper-compatible STT (e.g. `pip install -U openai-whisper`)
- Anki Desktop with the [AnkiConnect](https://ankiweb.net/shared/info/2055492159) add-on running at `http://127.0.0.1:8765`
- For pronunciation: an English SAPI voice on Windows (Settings → Time & Language → Speech → Add voices), or adapt `tts_word.py` to `say` (macOS) / `espeak-ng` (Linux)

```powershell
pip install -U yt-dlp openai-whisper
winget install --id=Gyan.FFmpeg -e
```

Xiaohongshu and Douyin reject anonymous extraction, so their syncs need an
already logged-in browser profile (`--cookies-from-browser chrome`) or an
exported Netscape cookies file. Cookies are read directly by `yt-dlp`; the sync
state never stores them.

## Usage

Inside an opencode session, give an Instagram URL and ask to study its English:

> https://www.instagram.com/p/DYaN8HxT-pt/

The agent runs the workflow, presents candidates, and waits for your confirmation before touching Anki. The default deck is `ins`.

### Incrementally sync a saved collection

Use the saved collection URL and an already logged-in browser profile. Cookies are
read directly by `yt-dlp`; the sync state does not store them:

```bash
python instagram-to-anki/scripts/sync_saved.py \
  --platform instagram \
  --collection-url 'https://www.instagram.com/USER/saved/_/COLLECTION_ID/' \
  --cookies-from-browser chrome \
  --output-dir instagram-saved
```

The command writes `instagram-saved/sync-state.json`. A later run discovers the
collection again, skips posts with a valid manifest and media file, downloads only
new posts, and retries prior failures. Use `--no-retry-failed` to skip failures or
`--limit 20` to cap one run.

Instagram may temporarily prevent `yt-dlp` from enumerating a saved collection.
In that case, export one post/reel URL per line and use the same incremental engine:

```bash
python instagram-to-anki/scripts/sync_saved.py \
  --urls-file saved-urls.txt \
  --cookies-from-browser chrome \
  --output-dir instagram-saved
```

The older entry points `sync_instagram_saved.py` and
`sync_instagram_collections.py` still work as Instagram-pinned aliases of
`sync_saved.py` and `sync_collections.py`.

Use `--dry-run` to update discovery state and display pending URLs without
downloading. Do not share cookie files or downloaded private media.

### Incrementally sync Xiaohongshu and Douyin favorites (收藏夹)

`yt-dlp` only addresses single items on these platforms — `XiaoHongShu` matches
`/explore/<id>` and `/discovery/item/<id>`, `Douyin` matches `/video/<id>` — so a
收藏夹 cannot be enumerated from the command line. Export it from the browser
you are already logged into.

1. Open the favorites page:

   | Platform | Page |
   | --- | --- |
   | Xiaohongshu | `https://www.xiaohongshu.com/user/profile/<uid>` → **收藏** |
   | Douyin | `https://www.douyin.com/user/self?showTab=collection` |
   | Instagram | `https://www.instagram.com/<user>/saved/<collection-id>/` |

2. Open the browser console and paste [`scripts/browser/export_collection.js`](instagram-to-anki/scripts/browser/export_collection.js).
   It auto-scrolls until no new links appear, merges the result into
   `localStorage`, then logs, copies and downloads `favorites-inventory.json`.
   Run it once per 收藏夹 — results accumulate. To name a collection explicitly,
   set `window.__FAVORITES_NAME = "英语"` before running.

3. Sync one collection incrementally:

   ```bash
   python instagram-to-anki/scripts/sync_saved.py \
     --urls-file collection-urls.txt \
     --platform xiaohongshu \
     --cookies-from-browser chrome \
     --output-dir xhs-saved/英语
   ```

   `--urls-file` takes one post URL per line (`jq -r '.collections[0].posts[]'`
   extracts a list). `--platform` may be omitted to detect each URL, but both
   platforms require cookies. Short links (`xhslink.com`, `v.douyin.com`) are
   resolved automatically.

4. Or sync every collection in the exported inventory at once — each gets its
   own directory and its own incremental state:

   ```bash
   python instagram-to-anki/scripts/sync_collections.py \
     --inventory favorites-inventory.json \
     --cookies-from-browser chrome \
     --output-dir saved
   ```

   Re-export the inventory and re-run to download only new posts.

### Mirror multiple saved collections

The local coordinator creates one safe directory per collection and gives every
directory its own incremental `sync-state.json`:

```bash
python instagram-to-anki/scripts/sync_collections.py \
  --inventory collections.json \
  --cookies-from-browser chrome \
  --output-dir instagram-saved
```

Inventory format (`platform` and `url` are optional; `platform` overrides
`--platform` for that entry):

```json
{
  "collections": [
    {
      "name": "英语",
      "platform": "instagram",
      "url": "https://www.instagram.com/USER/saved/_/COLLECTION_ID/",
      "posts": ["https://www.instagram.com/reel/POST_ID/"]
    }
  ]
}
```

Directory names preserve Unicode names, replace filesystem-reserved characters,
and add a collection-ID suffix when two collections share a name. Run the same
command again after refreshing the browser inventory to download only new or
missing posts.

## Notes

- Download only content you are authorized to access; do not bypass access controls or redistribute fetched media.
- The importer never reports success unless AnkiConnect returns `addedNoteIds` for every requested note; duplicate or partial failures are reported verbatim, not silently retried.
- Xiaohongshu image notes (图文) are downloaded as their full image list. Douyin image notes (`/note/<id>`) are **not** supported: `yt-dlp` exposes only a video cover for them, so they are marked `failed` with an explicit reason instead of being saved as a single cover image.
- Item state (`sync-state.json`) is per output directory. An item counts as completed only while its manifest still references a non-empty media file; if the media is deleted, the next run re-downloads it.
- After editing any skill file, restart opencode so the change takes effect.

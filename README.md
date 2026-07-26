# ins2anki

An [opencode](https://opencode.ai) skill that turns an Instagram post or reel into Anki vocabulary flashcards. It downloads the media, transcribes spoken English (or reads the caption), surfaces genuinely challenging words, and — after you confirm the selection — imports them through AnkiConnect as Basic notes with detailed Chinese meanings, IPA, local TTS pronunciation, and the original source media.

## Workflow

1. **Collect** — `scripts/download_instagram.py` fetches the post/reel with `yt-dlp` and writes `manifest.json` (media list + caption + metadata). Pass `--cookies` for login-gated content only with the user's authorization.
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
    download_instagram.py     # yt-dlp downloader + manifest writer
    tts_word.py               # local TTS pronunciation (Windows SAPI + ffmpeg)
    anki_import.py            # AnkiConnect importer (notes + media)
  references/
    selection-schema.md       # confirmed-selection JSON schema
    tooling.md                # download / transcription / TTS / Anki notes
```

`downloads/` (fetched media, transcripts, `selection.json`) and `__pycache__/` are gitignored — downloaded Instagram media is never committed or redistributed.

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

## Usage

Inside an opencode session, give an Instagram URL and ask to study its English:

> https://www.instagram.com/p/DYaN8HxT-pt/

The agent runs the workflow, presents candidates, and waits for your confirmation before touching Anki. The default deck is `ins`.

## Notes

- Download only content you are authorized to access; do not bypass access controls or redistribute fetched media.
- The importer never reports success unless AnkiConnect returns `addedNoteIds` for every requested note; duplicate or partial failures are reported verbatim, not silently retried.
- After editing any skill file, restart opencode so the change takes effect.

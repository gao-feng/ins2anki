---
name: instagram-to-anki
description: Download or incrementally mirror authorized Instagram posts and saved collections, Xiaohongshu (小红书) notes, and Douyin (抖音) videos and favorites (收藏夹), transcribe spoken English, identify contextually challenging vocabulary, confirm selections, and save approved material as Anki notes through AnkiConnect. Use for Instagram/Xiaohongshu/Douyin collection sync, local media mirroring, English study, vocabulary extraction, or Anki import.
---

# Instagram / Xiaohongshu / Douyin to Anki

Follow the workflow in order. Treat user confirmation as a mandatory commit boundary.

Supported sources: `instagram`, `xiaohongshu`, `douyin`. One shared incremental
engine handles all three; only the URL shapes and login requirements differ.

## 1. Collect the source

1. Obtain the post/reel/note URL. If the user has not supplied one, ask for it.
2. Run `scripts/download_media.py URL --output-dir DIR`. Use `--platform` only to override detection.
3. If a download fails because login is required, use an available browser session only with the user's authorization and pass a Netscape cookies file with `--cookies`, or use `--cookies-from-browser BROWSER` so credentials stay in the browser profile. Never expose or preserve cookies in artifacts.
4. Inspect `manifest.json`. Preserve the original media; do not recompress it unless a downstream tool requires a compatible copy.

Source content may be copyrighted or private. Download only content the user is authorized to access. Do not bypass access controls, and do not redistribute the downloaded media.

### Incrementally sync a saved collection

When the user asks to mirror a saved collection or 收藏夹 locally, run
`scripts/sync_saved.py` with `--output-dir` plus either `--urls-file` or, for
Instagram only, `--collection-url`. For private content, prefer
`--cookies-from-browser BROWSER` so credentials remain in the browser profile.
The sync command records `sync-state.json`, validates completed media before
skipping it, downloads only newly discovered or incomplete posts, and retries
failed posts on later runs. Use `--dry-run` for discovery without downloads.

`yt-dlp` cannot enumerate Xiaohongshu or Douyin favorites — it only addresses
single items (`/explore/<id>`, `/video/<id>`). For those platforms, ask the user
to open the 收藏 page in their logged-in browser, run
`scripts/browser/export_collection.js` in the console, and pass the exported
`posts` array via `--urls-file`. The exporter auto-scrolls, preserves
Xiaohongshu `xsec_token` parameters, and accumulates collections in
`localStorage` so it can be run once per 收藏夹. Never fabricate a favorites
list: if the user cannot export one, say so instead of guessing URLs.

Collection sync only downloads authorized source media. It does not imply
permission to create Anki notes; continue to require the confirmation in step 3
before importing any synchronized post into Anki.

Platform limitations to report honestly:

- Douyin image notes (`/note/<id>`) are unsupported: `yt-dlp` exposes only a
  video cover, which is not the note content. They are marked `failed` with an
  explicit reason.
- Xiaohongshu image notes (图文) are supported and downloaded as their full
  image list.

For all-collection sync, inspect the user's logged-in Saved UI with the
available browser tool, or have the user run the console exporter. Enumerate
every collection, scroll each collection until no new post links appear, and
write a UTF-8 inventory JSON with a `collections` array. Each entry must contain
`name` and a deduplicated `posts` array, plus an optional `platform` and `url`.
Then run `scripts/sync_collections.py --inventory FILE --output-dir DIR`,
adding the authorized cookie option when downloads require login. The
coordinator creates a safe directory for every collection and maintains a
separate incremental state inside it. Do not store browser cookies in the
inventory.

## 2. Transcribe and extract vocabulary

For video, transcribe audible English with timestamps. Prefer an available local speech-to-text tool; see [references/tooling.md](references/tooling.md) for reliable command patterns. For image/carousel posts, extract visible English with OCR or visual inspection. Include caption text when it is available in `manifest.json`.

Select genuinely challenging, useful words or short fixed expressions. Exclude names, URLs, obvious OCR errors, basic function words, and terms unsupported by the source. Judge difficulty from context rather than word length alone. Default to CEFR B2-C2 candidates when the learner's level is unknown.

For each candidate provide:

- word or expression;
- detailed Chinese meaning, structured as one part per line: part of speech; IPA phonetics when it aids pronunciation; the precise sense in this exact context; other common senses briefly, only when they help learning; and a short usage or collocation note when relevant. Put each part on its own line so the importer can render line breaks. Stay context-appropriate and do not pad with unrelated senses;
- source sentence or a short faithful context excerpt;
- timestamp for video, or image index for a carousel;
- brief reason it may be difficult (idiom, phrasal verb, academic word, uncommon sense, etc.).

Do not invent dialogue or definitions. Mark uncertain transcription explicitly.

## 3. Ask for confirmation

Present a numbered candidate list and ask the user to confirm all four dimensions:

1. which word numbers to save;
2. whether to use the proposed meanings or provide edits;
3. whether to attach the original video/image to every selected note, attach it only to specified notes, or omit it;
4. whether to add a local TTS pronunciation audio of the word to each note. It is synthesized locally with `scripts/tts_word.py` (Windows SAPI + ffmpeg) and embedded on the Front so the word plays with the card. Default to on.

Also ask for an Anki deck name only if the user has not already specified one; default to `ins` when they express no preference.

Stop here. Do not invoke AnkiConnect, create a deck, upload media, or add notes until an explicit reply confirms the selection. A vague response such as “looks good” counts only when the presented choices and default media behavior were unambiguous.

## 4. Save the confirmed notes

Create a UTF-8 JSON file matching [references/selection-schema.md](references/selection-schema.md). Include only confirmed entries and meanings.

If pronunciation was confirmed, generate the audio first — for each confirmed word run:

```powershell
python scripts/tts_word.py "WORD" --output-dir DIR
```

The script prints the generated `ig2anki_<word>.mp3` path; put that path in the entry's `pronunciation` field. The importer uploads it once and embeds `[sound:...]` on the Front.

Then run:

```powershell
python scripts/anki_import.py --selection selection.json
```

Anki Desktop must be running with AnkiConnect reachable at `http://127.0.0.1:8765`. The script creates the deck, uploads each referenced media file once, and adds Basic notes with the word (plus optional `[sound:...]` pronunciation) on the front and meaning, context, source link, position, and optional media on the back.

If AnkiConnect is unavailable, explain how to start Anki/install AnkiConnect and retain the selection JSON for retry. Never report success unless the script returns `addedNoteIds` for every requested note. If some notes fail, report exact failures and do not silently retry with modified content.

## 5. Report results

State the deck, number of notes added, attached media, and any skipped duplicates or errors. Keep downloaded media and the selection file until the user confirms they are no longer needed.

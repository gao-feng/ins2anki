---
name: instagram-to-anki
description: Download video or images from an Instagram post or reel, transcribe spoken English, identify contextually challenging English vocabulary, ask the user which words, meanings, and media to keep, then save only the confirmed material as Anki notes through AnkiConnect. Use when a user provides an Instagram URL and asks to study its English, extract difficult words, make vocabulary flashcards, or import the post into Anki.
---

# Instagram to Anki

Follow the workflow in order. Treat user confirmation as a mandatory commit boundary.

## 1. Collect the source

1. Obtain the Instagram post/reel URL. If the user has not supplied one, ask for it.
2. Run `scripts/download_instagram.py URL --output-dir DIR`.
3. If public download fails because login is required, use an available browser session only with the user's authorization and pass a Netscape cookies file with `--cookies`. Never expose or preserve cookies in artifacts.
4. Inspect `manifest.json`. Preserve the original media; do not recompress it unless a downstream tool requires a compatible copy.

Instagram content may be copyrighted or private. Download only content the user is authorized to access. Do not bypass access controls, and do not redistribute the downloaded media.

## 2. Transcribe and extract vocabulary

For video, transcribe audible English with timestamps. Prefer an available local speech-to-text tool; see [references/tooling.md](references/tooling.md) for reliable command patterns. For image/carousel posts, extract visible English with OCR or visual inspection. Include caption text when it is available in `manifest.json`.

Select genuinely challenging, useful words or short fixed expressions. Exclude names, URLs, obvious OCR errors, basic function words, and terms unsupported by the source. Judge difficulty from context rather than word length alone. Default to CEFR B2-C2 candidates when the learner's level is unknown.

For each candidate provide:

- word or expression;
- concise Chinese meaning appropriate to this exact context;
- source sentence or a short faithful context excerpt;
- timestamp for video, or image index for a carousel;
- brief reason it may be difficult (idiom, phrasal verb, academic word, uncommon sense, etc.).

Do not invent dialogue or definitions. Mark uncertain transcription explicitly.

## 3. Ask for confirmation

Present a numbered candidate list and ask the user to confirm all three dimensions:

1. which word numbers to save;
2. whether to use the proposed meanings or provide edits;
3. whether to attach the original video/image to every selected note, attach it only to specified notes, or omit it.

Also ask for an Anki deck name only if the user has not already specified one; default to `Instagram English` when they express no preference.

Stop here. Do not invoke AnkiConnect, create a deck, upload media, or add notes until an explicit reply confirms the selection. A vague response such as “looks good” counts only when the presented choices and default media behavior were unambiguous.

## 4. Save the confirmed notes

Create a UTF-8 JSON file matching [references/selection-schema.md](references/selection-schema.md). Include only confirmed entries and meanings. Run:

```powershell
python scripts/anki_import.py --selection selection.json
```

Anki Desktop must be running with AnkiConnect reachable at `http://127.0.0.1:8765`. The script creates the deck, uploads each referenced media file once, and adds Basic notes with the word on the front and meaning, context, source link, position, and optional media on the back.

If AnkiConnect is unavailable, explain how to start Anki/install AnkiConnect and retain the selection JSON for retry. Never report success unless the script returns `addedNoteIds` for every requested note. If some notes fail, report exact failures and do not silently retry with modified content.

## 5. Report results

State the deck, number of notes added, attached media, and any skipped duplicates or errors. Keep downloaded media and the selection file until the user confirms they are no longer needed.

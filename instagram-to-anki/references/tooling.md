# Media and transcription tooling

## Download prerequisites

Install `yt-dlp` using the platform's supported package mechanism. Instagram changes frequently; use a current release. The downloader accepts a Netscape cookies file via `--cookies` when the user authorizes access to content visible in their logged-in session.

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

#!/usr/bin/env python3
"""Generate an English pronunciation MP3 for a word with local TTS.

Windows: System.Speech (SAPI) synthesizes a WAV, then ffmpeg encodes the MP3.
The script picks the first enabled en-* SAPI voice. On other platforms, adapt
the synth step to `say` (macOS) or `espeak-ng` (Linux).
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path


def slugify(word: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "_", word.lower().strip())
    return s.strip("_")


def synth_wav(word: str, wav_path: Path) -> None:
    env = os.environ.copy()
    env["IG2ANKI_WORD"] = word
    env["IG2ANKI_WAV"] = str(wav_path)
    script = ";".join([
        "Add-Type -AssemblyName System.Speech",
        "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer",
        "$en = $s.GetInstalledVoices() | Where-Object { $_.Enabled -and $_.VoiceInfo.Culture.Name -like 'en-*' } | Select-Object -First 1",
        "if (-not $en) { throw 'No enabled English SAPI voice found' }",
        "$s.SelectVoice($en.VoiceInfo.Name)",
        "$s.SetOutputToWaveFile($env:IG2ANKI_WAV)",
        "$s.Speak($env:IG2ANKI_WORD)",
        "$s.Dispose()",
    ])
    subprocess.run(["powershell", "-NoProfile", "-Command", script], check=True, env=env)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("word")
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()

    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        parser.error("ffmpeg is not installed or not on PATH")

    out_dir = args.output_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"ig2anki_{slugify(args.word)}.mp3"

    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "tts.wav"
        try:
            synth_wav(args.word, wav)
        except subprocess.CalledProcessError as exc:
            print(f"TTS failed: {exc}", file=sys.stderr)
            return exc.returncode or 1
        cmd = [ffmpeg, "-y", "-i", str(wav), "-codec:a", "libmp3lame", "-b:a", "64k", "-ac", "1", str(out_path)]
        completed = subprocess.run(cmd, text=True)
        if completed.returncode:
            return completed.returncode

    print(out_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

"""Transcribe videos locally with mlx-whisper: free, offline, Apple Silicon GPU.

For when ElevenLabs Scribe is out of reach (no key, geo-blocked). Writes a
Scribe-shaped JSON (`words` of type/text/start/end) to the same
<edit_dir>/transcripts/<stem>.json, so pack_transcripts, timeline_view and
render read it unchanged and the cache is shared with transcribe.py.

What Scribe gives and this does not: speaker diarization, audio events,
fully verbatim fillers. Whisper tends to clean up "эм" and stutters; for
Russian and English a disfluent initial prompt keeps most of them, not all.
Any other language Whisper knows works too, without that prompt, so expect
fewer fillers in its text. Word edges come from
cross-attention alignment and are looser than Scribe's, so pad cuts toward
the top of the 30-200ms window and check boundaries by ear.

Needs the `mlx_whisper` CLI on PATH (`uv tool install mlx-whisper`).

Usage:
    python helpers/transcribe_local.py <video_or_dir> --language ru
    python helpers/transcribe_local.py <video_or_dir> --language ru --audio-track 1
    python helpers/transcribe_local.py <video_or_dir> --language de
"""

from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

from transcribe import count_audio_tracks, extract_audio, peak_dbfs, transcript_path
from transcribe_batch import find_videos


MODEL = "mlx-community/whisper-large-v3-turbo"

# Whisper imitates the style of its prompt: a prompt full of fillers makes it
# write fillers down instead of cleaning them out of the transcript.
FILLER_PROMPTS = {
    "ru": "Ну, э-э, короче, мы, эм, сейчас... ну, то есть, ммм, вот. Так, э-э, давайте, эм, ещё раз.",
    "en": "Umm, so, uh, we're, like, gonna... hmm, I mean, uh, let me, um, start again.",
}


def run_whisper(audio: Path, language: str, out_dir: Path) -> dict:
    prompt = FILLER_PROMPTS.get(language)
    cmd = [
        "mlx_whisper", str(audio),
        "--model", MODEL,
        "--language", language,
        *(["--initial-prompt", prompt] if prompt else []),
        "--word-timestamps", "True",
        "--hallucination-silence-threshold", "2",
        "--output-format", "json",
        "--output-dir", str(out_dir),
        "--verbose", "False",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    out = out_dir / f"{audio.stem}.json"
    # mlx_whisper prints the traceback and still exits 0 when transcription fails
    if proc.returncode != 0 or not out.exists():
        raise RuntimeError(
            f"mlx_whisper failed on {audio.name} (exit {proc.returncode}): {proc.stderr[-1500:]}"
        )
    return json.loads(out.read_text())


def to_scribe_words(result: dict) -> list[dict]:
    words: list[dict] = []
    for seg in result["segments"]:
        for w in seg.get("words", []):
            text = w["word"].strip()
            if not text:
                continue
            # Whisper splits "э-э" into "Э" and "-э,": a token opening with a hyphen
            # continues the word before it, or captions read "Э -Э"
            if words and re.match(r"-\w", text):
                words[-1] = {**words[-1], "text": words[-1]["text"] + text, "end": w["end"]}
                continue
            words.append({"text": text, "start": w["start"], "end": w["end"], "type": "word"})
    return words


def transcribe_local_one(video: Path, edit_dir: Path, language: str, audio_track: int) -> Path:
    out_path = transcript_path(edit_dir, video, audio_track)
    if out_path.exists():
        print(f"cached: {out_path.name}")
        return out_path

    n_tracks = count_audio_tracks(video)
    if n_tracks > 1:
        print(f"  note: {video.name} has {n_tracks} audio tracks, using track "
              f"{audio_track + 1} (--audio-track to change)", flush=True)

    t0 = time.time()
    with tempfile.TemporaryDirectory() as tmp:
        audio = Path(tmp) / f"{video.stem}.wav"
        extract_audio(video, audio, audio_track)
        # Whisper fills silence with invented text, so a silent track is an error, not an empty transcript
        peak = peak_dbfs(audio)
        if peak < -60.0:
            raise RuntimeError(
                f"track {audio_track + 1} of {video.name} is silent (peak {peak:.1f} dBFS); "
                f"the file has {n_tracks} audio tracks, pick another with --audio-track"
            )
        print(f"  transcribing {video.name}", flush=True)
        result = run_whisper(audio, language, Path(tmp))

    words = to_scribe_words(result)
    payload = {"language_code": language, "text": result["text"], "words": words, "model": MODEL}
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2))
    print(f"  saved: {out_path.name} ({len(words)} words) in {time.time() - t0:.1f}s")
    return out_path


def main() -> None:
    ap = argparse.ArgumentParser(description="Local mlx-whisper transcription in Scribe format")
    ap.add_argument("source", type=Path, help="Video file or directory of videos")
    ap.add_argument(
        "--edit-dir",
        type=Path,
        default=None,
        help="Edit output directory (default: <videos_dir>/edit)",
    )
    ap.add_argument(
        "--language",
        required=True,
        help="Spoken language as Whisper's code (ru, en, de, es...); ru and en get a filler prompt",
    )
    ap.add_argument(
        "--audio-track",
        type=int,
        default=0,
        help="Zero-based audio track to transcribe (OBS: 0 = game, 1 = mic).",
    )
    args = ap.parse_args()

    source = args.source.resolve()
    if not source.exists():
        sys.exit(f"not found: {source}")
    videos = find_videos(source) if source.is_dir() else [source]
    if not videos:
        sys.exit(f"no videos found in {source}")
    videos_dir = source if source.is_dir() else source.parent
    edit_dir = (args.edit_dir or (videos_dir / "edit")).resolve()

    for video in videos:
        transcribe_local_one(video, edit_dir, args.language, args.audio_track)


if __name__ == "__main__":
    main()

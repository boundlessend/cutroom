"""Self-eval of a rendered cut against its EDL, in one pass instead of one image per cut.

  duration   rendered video stream vs the frame-quantized EDL total (render.py cuts whole frames),
             and the decoded audio vs the picture: sound longer than the picture has drifted
             off lip sync somewhere
  seams      one sheet: the frame just before and just after every cut → <edit>/verify/seams.png
  edges      cuts whose first frame has a dark border strip: a zoom sampling outside the frame,
             or just dark content at the edge (a black object) — the seams sheet tells which
  loudness   integrated LUFS and true peak of the delivered file
  words      --retranscribe LANG: transcribes the render locally (mlx_whisper) and diffs its words
             with the words the EDL keeps. A mismatch next to a cut is a clipped or leftover word;
             elsewhere it is almost always the ASR reading the same audio differently.

No click detector: an RMS jump at a cut is nearly always speech starting right after a tightened
pause. The 30 ms fades (Hard Rule 3) handle clicks; look at timeline_view on a cut you doubt.

Usage:
    python helpers/verify_render.py <rendered.mp4> <edl.json>
    python helpers/verify_render.py <rendered.mp4> <edl.json> --retranscribe ru
"""

from __future__ import annotations

import argparse
import difflib
import json
import re
import subprocess
import sys
import tempfile
from fractions import Fraction
from pathlib import Path

import numpy as np
from PIL import Image, ImageDraw, ImageFont

from render import loudness, output_offsets, probe_source_fps, resolve_path, segment_duration
from transcribe import extract_audio, transcript_path
from transcribe_local import run_whisper

NEAR_CUT_S = 0.4
DARK_EDGE_LUMA = 12


def video_duration(path: Path) -> float:
    """The video stream's duration: the container's is the longer of video and audio."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=duration",
         "-of", "default=noprint_wrappers=1:nokey=1", str(path)],
        capture_output=True, text=True, check=True,
    )
    return float(out.stdout)


def decoded_audio_duration(path: Path) -> float:
    """The audio as a player hears it: decoded samples. The stream's duration field said
    +0.145 s while the decoded audio ran 1.26 s past the picture (AAC segments stacked
    up by the -c copy concat), so only the samples count."""
    out = subprocess.run(
        ["ffmpeg", "-v", "error", "-i", str(path), "-map", "0:a:0", "-ac", "1", "-ar", "8000",
         "-f", "s16le", "-"],
        capture_output=True, check=True,
    )
    return len(out.stdout) / 2 / 8000


def grab(video: Path, t: float, dest: Path, width: int) -> Image.Image:
    subprocess.run(
        ["ffmpeg", "-y", "-ss", f"{max(0.0, t):.3f}", "-i", str(video), "-frames:v", "1",
         "-vf", f"scale={width}:-2", str(dest)],
        check=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    return Image.open(dest).convert("RGB")


def seams_and_edges(video: Path, cuts: list[float], fps: float, sheet_path: Path) -> list[int]:
    """Write the seams sheet; return the cuts whose incoming frame has a dark border."""
    font = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", 16) if Path("/System/Library/Fonts/Menlo.ttc").exists() else ImageFont.load_default()
    pairs: list[tuple[Image.Image, Image.Image]] = []
    dark: list[int] = []
    with tempfile.TemporaryDirectory() as tmp:
        for i, c in enumerate(cuts):
            before = grab(video, c - 2 / fps, Path(tmp) / f"a{i}.png", 120)
            after = grab(video, c + 1 / fps, Path(tmp) / f"b{i}.png", 120)
            luma = np.asarray(grab(video, c + 1 / fps, Path(tmp) / f"e{i}.png", 360).convert("L"), dtype=np.float32)
            if min(luma[:2].mean(), luma[-2:].mean(), luma[:, :2].mean(), luma[:, -2:].mean()) < DARK_EDGE_LUMA:
                dark.append(i)
            pairs.append((before, after))
    w, h = pairs[0][0].size
    cols = 12
    cell_w = 2 * w + 10
    sheet = Image.new("RGB", (cols * cell_w, ((len(pairs) + cols - 1) // cols) * (h + 4)), "black")
    d = ImageDraw.Draw(sheet)
    for n, (a, b) in enumerate(pairs):
        x, y = (n % cols) * cell_w, (n // cols) * (h + 4)
        sheet.paste(a, (x, y))
        sheet.paste(b, (x + w + 2, y))
        d.text((x + 2, y + 1), f"{n:02d}", fill="yellow", font=font)
    sheet_path.parent.mkdir(parents=True, exist_ok=True)
    sheet.save(sheet_path)
    return dark


def norm(word: str) -> str:
    return re.sub(r"[^\w]", "", word.lower().replace("ё", "е"))


def expected_words(edl: dict, edit_dir: Path, rate: str) -> list[str]:
    words: list[str] = []
    cache: dict[str, list[dict]] = {}
    for r in edl["ranges"]:
        src = r["source"]
        if src not in cache:
            path = transcript_path(edit_dir, resolve_path(edl["sources"][src], edit_dir), int(edl.get("audio_track", 0)))
            if not path.exists():
                sys.exit(f"transcript not found: {path}")
            cache[src] = json.loads(path.read_text())["words"]
        start = float(r["start"])
        end = start + segment_duration(start, float(r["end"]), rate)
        words += [w["text"] for w in cache[src]
                  if w.get("type", "word") == "word" and start <= w["start"] and w["end"] <= end + 0.05]
    return words


def word_diff(video: Path, language: str, expected: list[str], cuts: list[float]) -> list[str]:
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / f"{video.stem}.wav"
        extract_audio(video, wav, 0)
        result = run_whisper(wav, language, Path(tmp))
    heard = [(w["word"], w["start"]) for s in result["segments"] for w in s.get("words", [])]
    exp = [n for n in (norm(x) for x in expected) if n]
    got = [(norm(x), t) for x, t in heard if norm(x)]
    sm = difflib.SequenceMatcher(a=exp, b=[g for g, _ in got], autojunk=False)
    report = [f"words: expected {len(exp)}, heard {len(got)}, match {sm.ratio():.3f}"]
    for op, a1, a2, b1, b2 in sm.get_opcodes():
        if op == "equal":
            continue
        t = got[min(b1, len(got) - 1)][1]
        nearest = min(cuts, key=lambda c: abs(c - t)) if cuts else float("inf")
        flag = "NEAR CUT" if abs(nearest - t) <= NEAR_CUT_S else "        "
        report.append(f"  {flag} @{t:7.2f}s  {op:7}  expected {' '.join(exp[a1:a2])!r}  heard {' '.join(g for g, _ in got[b1:b2])!r}")
    return report


def main() -> None:
    ap = argparse.ArgumentParser(description="Self-eval of a rendered cut against its EDL")
    ap.add_argument("video", type=Path, help="Rendered file")
    ap.add_argument("edl", type=Path, help="The EDL it was rendered from")
    ap.add_argument("--retranscribe", metavar="LANG", default=None,
                    help="Transcribe the render locally and diff its words with the EDL's")
    args = ap.parse_args()

    video, edl_path = args.video.resolve(), args.edl.resolve()
    edl = json.loads(edl_path.read_text())
    edit_dir = edl_path.parent
    # the render's own rate, so a render.py --fps needs no repeating here
    rate = probe_source_fps(video)
    if rate is None:
        sys.exit(f"no frame rate in {video}")
    fps = float(Fraction(rate))
    offsets = output_offsets(edl, rate)
    last = edl["ranges"][-1]
    expected_total = offsets[-1] + segment_duration(float(last["start"]), float(last["end"]), rate)
    cuts = offsets[1:]

    video_s = video_duration(video)
    drift = video_s - expected_total
    audio_gap = decoded_audio_duration(video) - video_s
    print(f"duration  video {video_s:.3f}s, {expected_total:.3f}s expected ({drift:+.3f}s)"
          f"{'' if abs(drift) <= 2 / fps else '  <-- CHECK: timeline drift'}; "
          f"audio {audio_gap:+.3f}s vs video"
          f"{'' if abs(audio_gap) <= 2 / fps else '  <-- CHECK: the sound drifts off the picture'}")

    sheet = edit_dir / "verify" / "seams.png"
    dark = seams_and_edges(video, cuts, fps, sheet)
    print(f"seams     {len(cuts)} cuts → {sheet}")
    print(f"edges     {'none dark' if not dark else f'dark border after cuts {dark} — check the seams sheet'}")

    i_lufs, tp = loudness(video)
    print(f"loudness  {i_lufs:.1f} LUFS, true peak {tp:.1f} dBFS"
          f"{'' if tp <= -1.0 else '  <-- CHECK: above -1 dBTP'}")

    if args.retranscribe:
        for line in word_diff(video, args.retranscribe, expected_words(edl, edit_dir, rate), cuts):
            print(line)


if __name__ == "__main__":
    main()

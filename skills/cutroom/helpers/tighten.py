"""Build an EDL skeleton from one source's transcript: speech ranges with tightened pauses.

The mechanical half of "cut the pauses and dead air". Every gap of --gap seconds or
more between words becomes a cut; each kept range is padded by --pad-before and
--pad-after (Hard Rule 7). The editorial half stays with you:

  --remove A-B   drop a stretch of source time (a retake, a slip, walking out of frame)
  --topic T      a new subject starts at source time T: the range is split there if it
                 runs through T, and the range starting at T is marked "topic": true
                 (the zoom pushes into that cut)

Two rules keep the rhythm from turning choppy: a range shorter than --min-range is
glued to its predecessor together with the pause between them (no framing change
on a single word), and a range longer than --split-long is split at sentence ends
without removing any time, so reframing still has places to change the shot.

Every pause cut is audited against the audio: a gap with speech-level sound for 0.3 s
or more is most likely words the ASR dropped (local Whisper drops "э-э", "ну, как бы").
Listen to those, or re-transcribe the snippet, before trusting the cut.

Usage:
    python helpers/tighten.py <video> -o <edit>/edl.json
    python helpers/tighten.py <video> --remove 296.78-307.61 --topic 18.58 --topic 80.18 -o edl.json
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
import wave
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

import numpy as np

from render import probe_source_fps
from transcribe import extract_audio, transcript_path


@dataclass(frozen=True)
class Word:
    text: str
    start: float
    end: float


@dataclass(frozen=True)
class Range:
    start: float
    end: float
    text: str


@dataclass(frozen=True)
class Tightening:
    gap: float
    pad_before: float
    pad_after: float
    min_range: float
    split_long: float
    split_every: float
    removals: list[tuple[float, float]]
    topics: list[float]


SENTENCE_GAP_S = 0.10
ANY_GAP_S = 0.25
MIN_PIECE_S = 2.5
LOUD_BELOW_SPEECH_DB = 15.0
LOUD_MIN_S = 0.3


def load_words(path: Path) -> list[Word]:
    raw = json.loads(path.read_text())["words"]
    return [Word(w["text"], w["start"], w["end"]) for w in raw if w.get("type", "word") == "word"]


def words_in(words: list[Word], s: float, e: float) -> list[Word]:
    return [w for w in words if s <= w.start and w.end <= e + 0.05]


def text_of(words: list[Word], s: float, e: float) -> str:
    return " ".join(w.text for w in words_in(words, s, e))


def speech_ranges(words: list[Word], t: Tightening) -> list[Range]:
    groups: list[list[Word]] = [[words[0]]]
    for prev, nxt in zip(words, words[1:]):
        if nxt.start - prev.end >= t.gap:
            groups.append([nxt])
        else:
            groups[-1].append(nxt)
    return [
        Range(max(0.0, g[0].start - t.pad_before), g[-1].end + t.pad_after, " ".join(w.text for w in g))
        for g in groups
    ]


def subtract(ranges: list[Range], removals: list[tuple[float, float]], words: list[Word]) -> list[Range]:
    out: list[Range] = []
    for r in ranges:
        pieces = [(r.start, r.end)]
        for a, b in removals:
            kept: list[tuple[float, float]] = []
            for s, e in pieces:
                if b <= s or a >= e:
                    kept.append((s, e))
                    continue
                if s < a:
                    kept.append((s, a))
                if b < e:
                    kept.append((b, e))
            pieces = kept
        for s, e in pieces:
            text = text_of(words, s, e)
            if text:
                out.append(Range(s, e, text))
    return out


def merge_short(ranges: list[Range], words: list[Word], t: Tightening) -> list[Range]:
    """A range under min_range grows onto its predecessor, pause included, unless a removal sits between."""
    out: list[Range] = []
    for r in ranges:
        prev = out[-1] if out else None
        removed_between = prev is not None and any(prev.end <= a and b <= r.start for a, b in t.removals)
        if prev is not None and r.end - r.start < t.min_range and not removed_between:
            out[-1] = Range(prev.start, r.end, text_of(words, prev.start, r.end))
        else:
            out.append(r)
    return out


def split_points(r: Range, words: list[Word], t: Tightening) -> list[float]:
    """Shot-change points that remove no time: topic starts, and sentence ends in long ranges."""
    inner = words_in(words, r.start, r.end)
    gaps = list(zip(inner, inner[1:]))
    points = [(p.end + n.start) / 2 for p, n in gaps if any(p.end < tp <= n.start + 0.01 for tp in t.topics)]
    if r.end - r.start > t.split_long:
        candidates = [
            (p.end + n.start) / 2 for p, n in gaps
            if (p.text[-1] in ".?!" and n.start - p.end >= SENTENCE_GAP_S) or n.start - p.end >= ANY_GAP_S
        ]
        last = r.start
        for c in candidates:
            if c - last >= t.split_every and r.end - c >= MIN_PIECE_S:
                points.append(c)
                last = c
    return sorted(set(points))


def split_long(ranges: list[Range], words: list[Word], t: Tightening, fps: Fraction) -> list[Range]:
    """Split points land on whole frames from the range start: the pieces then join
    seamlessly in render.py, with no audio fade dipping the continuous sound."""
    out: list[Range] = []
    for r in ranges:
        snapped = sorted({r.start + float(round((p - r.start) * fps) / fps) for p in split_points(r, words, t)})
        edges = [r.start, *(p for p in snapped if r.start < p < r.end), r.end]
        for s, e in zip(edges, edges[1:]):
            out.append(Range(s, e, text_of(words, s, e)))
    return out


def is_topic_start(r: Range, t: Tightening) -> bool:
    return any(r.start <= tp <= r.start + t.pad_before + 0.15 for tp in t.topics)


def pause_cuts(words: list[Word], t: Tightening) -> list[tuple[float, float, str, str]]:
    """Every pause the tightening removes: (gap start, gap end, word before, word after)."""
    cuts: list[tuple[float, float, str, str]] = []
    for prev, nxt in zip(words, words[1:]):
        inside_removal = any(a <= prev.end and nxt.start <= b for a, b in t.removals)
        if nxt.start - prev.end >= t.gap and not inside_removal:
            cuts.append((prev.end, nxt.start, prev.text, nxt.text))
    return cuts


def loud_gaps(video: Path, audio_track: int, cuts: list[tuple[float, float, str, str]]) -> list[tuple[float, float, float, str, str]]:
    """Pause cuts holding speech-level audio for LOUD_MIN_S or more: (start, end, loud seconds, before, after)."""
    with tempfile.TemporaryDirectory() as tmp:
        wav = Path(tmp) / "a.wav"
        extract_audio(video, wav, audio_track)
        with wave.open(str(wav), "rb") as w:
            sr = w.getframerate()
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32) / 32768
    win = int(0.05 * sr)
    usable = (pcm.size // win) * win
    rms = np.sqrt(np.mean(pcm[:usable].reshape(-1, win) ** 2, axis=1))
    env = 20 * np.log10(np.maximum(rms, 1e-9))
    speech_level = float(np.percentile(env, 90))
    flagged: list[tuple[float, float, float, str, str]] = []
    for a, b, before, after in cuts:
        window = env[int(a / 0.05):int(b / 0.05)]
        loud = float((window > speech_level - LOUD_BELOW_SPEECH_DB).sum()) * 0.05
        if loud >= LOUD_MIN_S:
            flagged.append((a, b, loud, before, after))
    return flagged


def build_edl(video: Path, ranges: list[Range], t: Tightening) -> dict:
    edl_ranges: list[dict] = []
    for r in ranges:
        entry: dict = {"source": video.stem, "start": round(r.start, 3), "end": round(r.end, 3), "quote": r.text[:120]}
        if is_topic_start(r, t):
            entry["topic"] = True
        edl_ranges.append(entry)
    return {
        "version": 1,
        "sources": {video.stem: str(video)},
        "ranges": edl_ranges,
        "total_duration_s": round(sum(r.end - r.start for r in ranges), 2),
    }


def parse_span(text: str) -> tuple[float, float]:
    a, sep, b = text.partition("-")
    if not sep:
        raise argparse.ArgumentTypeError(f"expected START-END in seconds, got {text!r}")
    start, end = float(a), float(b)
    if end <= start:
        raise argparse.ArgumentTypeError(f"end must be after start in {text!r}")
    return start, end


def main() -> None:
    ap = argparse.ArgumentParser(description="EDL skeleton with tightened pauses from one source's transcript")
    ap.add_argument("video", type=Path, help="Source video")
    ap.add_argument("-o", "--output", type=Path, required=True, help="EDL to write")
    ap.add_argument("--transcript", type=Path, default=None,
                    help="Transcript JSON (default: the one transcribe*.py wrote for --audio-track)")
    ap.add_argument("--gap", type=float, default=0.45, help="Pauses at least this long are cut (default 0.45)")
    ap.add_argument("--pad-before", type=float, default=0.10, help="Air kept before a range's first word (default 0.10)")
    ap.add_argument("--pad-after", type=float, default=0.14, help="Air kept after a range's last word (default 0.14)")
    ap.add_argument("--min-range", type=float, default=0.8, help="Shorter ranges merge into the previous one (default 0.8)")
    ap.add_argument("--split-long", type=float, default=9.0, help="Ranges longer than this split at sentence ends (default 9)")
    ap.add_argument("--split-every", type=float, default=4.5, help="Minimum spacing of those splits (default 4.5)")
    ap.add_argument("--remove", type=parse_span, action="append", default=[], help="Source span START-END to drop")
    ap.add_argument("--topic", type=float, action="append", default=[], help="Source time where a new subject starts")
    ap.add_argument("--audio-track", type=int, default=0, help="Audio track for the loud-gap audit (default 0)")
    args = ap.parse_args()
    if args.gap <= args.pad_before + args.pad_after:
        ap.error(f"--gap {args.gap:g} must exceed --pad-before + --pad-after ({args.pad_before + args.pad_after:g}): "
                 "a shorter pause keeps more padding than it has, and its two ranges overlap and repeat the sound")

    video = args.video.resolve()
    if not video.exists():
        sys.exit(f"video not found: {video}")
    transcript = args.transcript or transcript_path(video.parent / "edit", video, args.audio_track)
    if not transcript.exists():
        sys.exit(f"transcript not found: {transcript} (transcribe first)")

    t = Tightening(args.gap, args.pad_before, args.pad_after, args.min_range, args.split_long,
                   args.split_every, sorted(args.remove), sorted(args.topic))
    words = load_words(transcript)
    rate = probe_source_fps(video)
    if rate is None:
        sys.exit(f"no frame rate in {video}")
    ranges = split_long(merge_short(subtract(speech_ranges(words, t), t.removals, words), words, t), words, t,
                        Fraction(rate))
    edl = build_edl(video, ranges, t)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(edl, ensure_ascii=False, indent=2))

    source_s = words[-1].end - words[0].start
    print(f"{len(ranges)} ranges, {edl['total_duration_s']:.1f}s kept of {source_s:.1f}s spoken → {args.output}")
    for i, r in enumerate(ranges):
        mark = " [topic]" if is_topic_start(r, t) else ""
        print(f"  {i:02d} {r.start:8.2f}-{r.end:8.2f} ({r.end - r.start:5.2f}s){mark} {r.text[:70]}")

    flagged = loud_gaps(video, args.audio_track, pause_cuts(words, t))
    if flagged:
        print(f"\n{len(flagged)} cut pause(s) hold speech-level audio — likely words the ASR dropped, check before trusting:")
        for a, b, loud, before, after in flagged:
            print(f"  {a:8.2f}-{b:8.2f}  {loud:.2f}s loud  between «{before}» and «{after}»")


if __name__ == "__main__":
    main()

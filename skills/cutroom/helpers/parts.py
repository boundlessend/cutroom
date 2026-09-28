"""Split a long cut into parts of at most --max seconds, each its own EDL.

A part starts at an EDL cut or at a pause of INNER_GAP_S or more between two
words inside a range (never inside a word), and never while a title from the
EDL's `ass` or an overlay is on screen. The latest such point that keeps the
part under --max wins, so parts come out as long as allowed.

Every part EDL lands in the edit dir next to the full EDL, so render.py renders
it from the full cut's cached segments. Its `ass` and `subtitles` files are cut
to the part and retimed to start at 0, and its overlays keep only those inside
the part. Captions built with `render.py --build-subtitles` need nothing: they
come from the transcripts.

Usage:
    python helpers/parts.py <edit>/edl.json --max 60
    python helpers/parts.py <edit>/edl.json --max 60 --min 30 --fps 30
then render every part:
    python helpers/render.py <edit>/edl_part1.json -o <edit>/parts/part1.mp4
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from render import output_offsets, parse_fps, resolve_edl_file, resolve_output_rate, resolve_path, segment_duration
from transcribe import transcript_path

INNER_GAP_S = 0.3
WINDOW_MARGIN_S = 0.15


def edl_duration(edl: dict, rate: str) -> float:
    last = edl["ranges"][-1]
    return output_offsets(edl, rate)[-1] + segment_duration(float(last["start"]), float(last["end"]), rate)


def candidates(edl: dict, edit_dir: Path, rate: str) -> list[tuple[float, int, float]]:
    """Where a part may start: (full-cut output time, range index, source time)."""
    audio_track = int(edl.get("audio_track", 0))
    words_by_source: dict[str, list[dict]] = {}
    for name, path in edl["sources"].items():
        tr = transcript_path(edit_dir, resolve_path(path, edit_dir), audio_track)
        words_by_source[name] = (
            [w for w in json.loads(tr.read_text())["words"] if w.get("type", "word") == "word"]
            if tr.exists() else []
        )
    out: list[tuple[float, int, float]] = []
    for i, (r, offset) in enumerate(zip(edl["ranges"], output_offsets(edl, rate))):
        start = float(r["start"])
        end = start + segment_duration(start, float(r["end"]), rate)
        if i > 0:
            out.append((offset, i, start))
        inner = [w for w in words_by_source[r["source"]] if start <= w["start"] and w["end"] <= end]
        for p, n in zip(inner, inner[1:]):
            if n["start"] - p["end"] >= INNER_GAP_S:
                mid = (p["end"] + n["start"]) / 2
                out.append((offset + mid - start, i, mid))
    return sorted(out)


def ass_events(text: str) -> list[tuple[float, float]]:
    return [(ass_seconds(m[1]), ass_seconds(m[2]))
            for m in re.finditer(r"^Dialogue:\s*[^,]*,([^,]+),([^,]+),", text, re.M)]


def boundaries(cands: list[tuple[float, int, float]], total: float, windows: list[tuple[float, float]],
               max_s: float, min_s: float) -> list[tuple[float, int, float]]:
    """Greedy: the latest free point at most max_s after the part's start."""
    chosen: list[tuple[float, int, float]] = []
    start = 0.0
    while total - start > max_s:
        free = [
            c for c in cands
            if start + min_s < c[0] <= start + max_s
            and not any(a - WINDOW_MARGIN_S < c[0] < b + WINDOW_MARGIN_S for a, b in windows)
        ]
        if not free:
            sys.exit(f"no free split point between {start + min_s:.1f}s and {start + max_s:.1f}s: "
                     "every pause there is under a title or an overlay. Lower --min or raise --max.")
        chosen.append(free[-1])
        start = free[-1][0]
    return chosen


def slice_ranges(ranges: list[dict], a: tuple[int, float] | None, b: tuple[int, float] | None) -> list[dict]:
    """The ranges between two split points (range index, source time); None is an end of the cut."""
    first = a[0] if a else 0
    last = b[0] if b else len(ranges) - 1
    out: list[dict] = []
    for i in range(first, last + 1):
        r = dict(ranges[i])
        if a and i == a[0]:
            r["start"] = round(a[1], 3)
        if b and i == b[0]:
            if b[1] <= float(ranges[i]["start"]) + 1e-6:
                continue
            r["end"] = round(b[1], 3)
        out.append(r)
    return out


class Retimer:
    """Maps a full-cut output time into one part's output time, through the range it falls
    in: exact under frame quantization, and right even if a source moment is used twice."""

    def __init__(self, edl: dict, part: dict, first: int, rate: str) -> None:
        self.full = list(zip(edl["ranges"], output_offsets(edl, rate)))
        self.part = list(zip(part["ranges"], output_offsets(part, rate)))
        self.first = first
        self.rate = rate
        self.total = edl_duration(part, rate)

    def __call__(self, t: float) -> float | None:
        """Part time of full-cut time t, None when t is outside the part."""
        for i, (r, offset) in enumerate(self.full):
            dur = segment_duration(float(r["start"]), float(r["end"]), self.rate)
            if offset <= t < offset + dur:
                k = i - self.first
                if not 0 <= k < len(self.part):
                    return None
                pr, poffset = self.part[k]
                src_t = float(r["start"]) + (t - offset)
                local = poffset + (src_t - float(pr["start"]))
                return local if 0 <= local <= self.total else None
        return None


def ass_seconds(stamp: str) -> float:
    h, m, s = stamp.strip().split(":")
    return int(h) * 3600 + int(m) * 60 + float(s)


def ass_stamp(t: float) -> str:
    cs = round(t * 100)
    return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"


def retime_ass(text: str, retime: Retimer) -> str:
    out: list[str] = []
    for line in text.splitlines(keepends=True):
        m = re.match(r"^(Dialogue:\s*[^,]*,)([^,]+),([^,]+),(.*)$", line, re.S)
        if not m:
            out.append(line)
            continue
        a, b = retime(ass_seconds(m[2])), retime(ass_seconds(m[3]) - 0.001)
        if a is not None and b is not None:
            out.append(f"{m[1]}{ass_stamp(a)},{ass_stamp(b)},{m[4]}")
    return "".join(out)


def srt_seconds(stamp: str) -> float:
    h, m, rest = stamp.strip().split(":")
    s, ms = rest.split(",")
    return int(h) * 3600 + int(m) * 60 + int(s) + int(ms) / 1000


def srt_stamp(t: float) -> str:
    ms = round(t * 1000)
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def retime_srt(text: str, retime: Retimer, part_start: float, part_end: float) -> str:
    """Cues are clipped to the part: a caption running across a split shows in both."""
    cues: list[str] = []
    for block in re.split(r"\n\s*\n", text.strip()):
        lines = block.splitlines()
        timing = next((i for i, ln in enumerate(lines) if "-->" in ln), None)
        if timing is None:
            continue
        a, b = (srt_seconds(x) for x in lines[timing].split("-->"))
        a, b = max(a, part_start), min(b, part_end - 0.001)
        if b <= a:
            continue
        ra, rb = retime(a), retime(b)
        if ra is None or rb is None:
            continue
        cues.append(f"{len(cues) + 1}\n{srt_stamp(ra)} --> {srt_stamp(rb)}\n" + "\n".join(lines[timing + 1:]))
    return "\n\n".join(cues) + "\n"


def main() -> None:
    ap = argparse.ArgumentParser(description="Split a long cut into part EDLs of at most --max seconds")
    ap.add_argument("edl", type=Path, help="The full cut's EDL")
    ap.add_argument("--max", type=float, required=True, help="Longest part, seconds")
    ap.add_argument("--min", type=float, default=None, help="Shortest part but the last, seconds (default: half of --max)")
    ap.add_argument("--fps", type=parse_fps, default=None, help="Rate passed to render.py --fps, if any")
    ap.add_argument("--prefix", default="edl_part", help="Part EDL names: <prefix><n>.json (default edl_part)")
    args = ap.parse_args()

    edl_path = args.edl.resolve()
    edl = json.loads(edl_path.read_text())
    edit_dir = edl_path.parent
    rate = resolve_output_rate(edl, edit_dir, args.fps)
    total = edl_duration(edl, rate)
    min_s = args.min if args.min is not None else args.max / 2

    ass_text = resolve_edl_file(edl["ass"], edit_dir, "ass").read_text(encoding="utf-8") if edl.get("ass") else None
    srt_text = (resolve_edl_file(edl["subtitles"], edit_dir, "subtitles").read_text(encoding="utf-8")
                if edl.get("subtitles") else None)
    windows = ass_events(ass_text) if ass_text else []
    windows += [(float(o["start_in_output"]), float(o["start_in_output"]) + float(o["duration"]))
                for o in edl.get("overlays") or []]

    chosen = boundaries(candidates(edl, edit_dir, rate), total, windows, args.max, min_s)
    points: list[tuple[int, float] | None] = [None, *[(i, src) for _, i, src in chosen], None]
    starts = [0.0, *[t for t, _, _ in chosen], total]

    for n, (a, b) in enumerate(zip(points, points[1:]), start=1):
        part = {**edl, "ranges": slice_ranges(edl["ranges"], a, b)}
        retime = Retimer(edl, part, a[0] if a else 0, rate)
        if ass_text is not None:
            name = f"{args.prefix}{n}.ass"
            (edit_dir / name).write_text(retime_ass(ass_text, retime), encoding="utf-8")
            part["ass"] = name
        if srt_text is not None:
            name = f"{args.prefix}{n}.srt"
            (edit_dir / name).write_text(retime_srt(srt_text, retime, starts[n - 1], starts[n]), encoding="utf-8")
            part["subtitles"] = name
        part["overlays"] = [
            {**o, "start_in_output": round(start, 3)}
            for o in edl.get("overlays") or []
            if (start := retime(float(o["start_in_output"]))) is not None
        ]
        part["total_duration_s"] = round(edl_duration(part, rate), 2)
        out = edit_dir / f"{args.prefix}{n}.json"
        out.write_text(json.dumps(part, ensure_ascii=False, indent=2))
        quote = (part["ranges"][0].get("quote") or "")[:50]
        print(f"part {n}: {len(part['ranges'])} ranges, {part['total_duration_s']:.2f}s → {out.name}  «{quote}»")
    print(f"{len(points) - 1} parts from {total:.2f}s")


if __name__ == "__main__":
    main()

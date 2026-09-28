"""Fill every EDL range's `vf` with a continuous zoom from its `frame` target.

Punch-ins that jump in scale at the cut read as harsh. Here scale and anchor are
continuous across every cut: a range starts exactly where its predecessor ended
and eases (in-out cubic) to its own target over --settle seconds, at most 70% of
the range. Inside the range a slow drift keeps the shot alive; before a range
marked "topic": true the outgoing range pushes in over its last --push seconds
and the next one carries on from there. The motion is timed to the range's last
frame (duration - 1/fps), otherwise the next range starts one frame's worth of
motion away and the cut jumps.

Zoom is done by `perspective` with `eval=frame` and cubic interpolation: subpixel
and steady, unlike zoompan, which rounds the crop to whole pixels and shimmers
on slow moves. Scale never goes below 1, so the frame never samples outside.

Range fields read:
  frame  {"z": 1.12, "ax": 0.5, "ay": 0.45}  target zoom and anchor, as fractions of
         the frame (ax/ay: the point the zoom closes in on; clamped to stay in frame)
  topic  true on the first range of a new subject (tighten.py --topic writes it)
  drift  optional total drift over the range as a fraction of zoom; default pushes
         in on an unzoomed shot and pulls out on a zoomed one, 0.8% per second up to 4%

Choosing the targets is the editor's call, not this script's: alternate shot sizes
between neighbouring ranges, and keep a close-up off ranges where the subject holds
something at chest height (it gets cropped). A range under ~0.8 s should not change
shot at all (tighten.py merges those).

Usage:
    python helpers/reframe.py <edl.json> -o <edl.json>
    python helpers/reframe.py <edl.json> -o <edl.json> --settle 0.8 --push 0.3 --push-amp 0.08
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass
from fractions import Fraction
from pathlib import Path

from render import resolve_output_rate, segment_duration


@dataclass(frozen=True)
class Frame:
    z: float
    ax: float
    ay: float


@dataclass(frozen=True)
class Move:
    start: Frame
    target: Frame
    dur: float
    last: float
    settle: float
    drift: float
    push_out: float
    push_amp: float


def ease_in_out(u: str) -> str:
    return f"if(lt({u},0.5),4*pow({u},3),1-pow(2-2*({u}),3)/2)"


def phase(t: str, a: float, b: float) -> str:
    if b - a <= 1e-3:
        return f"gte({t},{a:.4f})"
    return f"clip(({t}-{a:.4f})/{b - a:.4f},0,1)"


def drift_span(m: Move) -> tuple[float, float] | None:
    start, end = m.settle, m.last - m.push_out
    return (start, end) if end - start > 0.5 else None


def expressions(m: Move, fps: float) -> tuple[str, str, str]:
    """(z, ax, ay) as ffmpeg expressions of the output frame number."""
    t = f"(on/{fps:.6f})"
    e_settle = ease_in_out(phase(t, 0.0, m.settle))
    span = drift_span(m)
    e_drift = ease_in_out(phase(t, *span)) if span else "0"
    e_push = f"pow({phase(t, m.last - m.push_out, m.last)},3)" if m.push_out > 0 else "0"
    base = f"({m.target.z}*(1+{m.drift}*{e_drift}))"
    # a pull-out drift on a slightly zoomed shot would dip below 1 and sample outside the frame
    z = f"max(1,(({m.start.z}+({base}-{m.start.z})*{e_settle})*(1+{m.push_amp}*{e_push})))"
    ax = f"({m.start.ax}+({m.target.ax}-{m.start.ax})*{e_settle})"
    ay = f"({m.start.ay}+({m.target.ay}-{m.start.ay})*{e_settle})"
    return z, ax, ay


def end_frame(m: Move) -> Frame:
    z = m.target.z * (1 + (m.drift if drift_span(m) else 0.0))
    if m.push_out > 0:
        z *= 1 + m.push_amp
    return Frame(max(1.0, z), m.target.ax, m.target.ay)


def perspective_filter(z: str, ax: str, ay: str) -> str:
    hw, hh = f"(W/(2*{z}))", f"(H/(2*{z}))"
    cx = f"clip({ax}*W,{hw},W-{hw})"
    cy = f"clip({ay}*H,{hh},H-{hh})"
    left, right = f"({cx}-{hw})", f"({cx}+{hw})"
    top, bottom = f"({cy}-{hh})", f"({cy}+{hh})"
    return (
        f"perspective=x0='{left}':y0='{top}':x1='{right}':y1='{top}'"
        f":x2='{left}':y2='{bottom}':x3='{right}':y3='{bottom}'"
        ":interpolation=cubic:eval=frame"
    )


def target_of(r: dict, i: int) -> Frame:
    f = r.get("frame")
    if not isinstance(f, dict) or "z" not in f:
        raise ValueError(f"range {i} has no frame target (\"frame\": {{\"z\": ..., \"ax\": ..., \"ay\": ...}})")
    target = Frame(float(f["z"]), float(f.get("ax", 0.5)), float(f.get("ay", 0.5)))
    if target.z < 1:
        raise ValueError(f"range {i}: z={target.z} < 1 would sample outside the frame")
    return target


def default_drift(target: Frame, dur: float) -> float:
    magnitude = min(0.04, 0.008 * dur)
    return magnitude if target.z <= 1.001 else -magnitude


def reframe(edl: dict, rate: str, settle: float, push: float, push_amp: float) -> dict:
    fps = float(Fraction(rate))
    ranges = edl["ranges"]
    state = target_of(ranges[0], 0)
    out_ranges: list[dict] = []
    for i, r in enumerate(ranges):
        target = target_of(r, i)
        dur = segment_duration(float(r["start"]), float(r["end"]), rate)
        pushes = i + 1 < len(ranges) and bool(ranges[i + 1].get("topic"))
        drift = float(r["drift"]) if "drift" in r else default_drift(target, dur)
        move = Move(
            start=state,
            target=target,
            dur=dur,
            last=dur - 1 / fps,
            settle=min(settle, 0.7 * dur),
            drift=drift,
            push_out=min(push, 0.3 * dur) if pushes else 0.0,
            push_amp=push_amp,
        )
        out_ranges.append({**r, "vf": perspective_filter(*expressions(move, fps))})
        state = end_frame(move)
    return {**edl, "ranges": out_ranges}


def main() -> None:
    ap = argparse.ArgumentParser(description="Continuous zoom vf for every EDL range from its frame target")
    ap.add_argument("edl", type=Path, help="EDL whose ranges carry a frame target")
    ap.add_argument("-o", "--output", type=Path, required=True, help="EDL to write (may be the input)")
    ap.add_argument("--settle", type=float, default=1.0, help="Seconds to ease into a range's target (default 1.0)")
    ap.add_argument("--push", type=float, default=0.4, help="Push-in before a topic cut, seconds (default 0.4)")
    ap.add_argument("--push-amp", type=float, default=0.10, help="Push-in amount as a zoom fraction (default 0.10)")
    ap.add_argument("--fps", type=str, default=None, help="Output rate if render.py gets --fps (default: source rate)")
    args = ap.parse_args()

    edl_path = args.edl.resolve()
    if not edl_path.exists():
        sys.exit(f"edl not found: {edl_path}")
    edl = json.loads(edl_path.read_text())
    rate = resolve_output_rate(edl, edl_path.parent, args.fps)
    try:
        out = reframe(edl, rate, args.settle, args.push, args.push_amp)
    except ValueError as exc:
        sys.exit(str(exc))
    args.output.write_text(json.dumps(out, ensure_ascii=False, indent=2))
    pushes = sum(1 for r in edl["ranges"][1:] if r.get("topic"))
    print(f"vf written for {len(out['ranges'])} ranges @ {rate} fps, {pushes} topic push(es) → {args.output}")


if __name__ == "__main__":
    main()

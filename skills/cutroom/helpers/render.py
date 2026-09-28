"""Render a video from an EDL.

Implements the HEURISTICS render pipeline in the correct order:

  1. Per-segment extract with color grade + 30ms audio fades baked in, each
     range cut to whole frames and cached by what shapes it; audio stays PCM
  2. Lossless -c copy concat into a base MOV
  3. If overlays, ASS titles or subtitles: single filter graph that overlays
     animations (with PTS shift so frame 0 lands at the overlay window start),
     burns the EDL's `ass` file as designed, and applies `subtitles` LAST
  4. Loudness normalization and the one AAC encode of the audio → output

Optionally builds a master SRT from the per-source transcripts + EDL
output-timeline offsets, applies the proven force_style (2-word
UPPERCASE chunks, Helvetica 18 Bold, MarginV=35).

Usage:
    python helpers/render.py <edl.json> -o final.mp4
    python helpers/render.py <edl.json> -o preview.mp4 --preview
    python helpers/render.py <edl.json> -o final.mp4 --build-subtitles
    python helpers/render.py <edl.json> -o final.mp4 --no-subtitles
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from fractions import Fraction
from pathlib import Path

from grade import auto_grade_for_clip, get_preset  # same directory
from reframe import zoom_filters
from transcribe import count_audio_tracks


# -------- Subtitle style (bold-overlay, proven at 1920×1080 and 1080×1920) --
#
# MarginV is NOT taste — it is a platform safe-zone rule.
# TikTok / IG Reels / Shorts UI (caption, username, music, right-rail actions)
# covers roughly the bottom ~25–30% of a 1080×1920 frame. Captions placed near
# the bottom edge get clipped or obscured by the UI. libass auto-scales the
# render canvas relative to PlayResY=288, so MarginV=90 lands the caption
# baseline roughly 30% up from the bottom on any aspect — clear of the UI on
# every major vertical-video platform. Do not drop this below ~75 without a
# specific reason.
SUB_FORCE_STYLE = (
    "FontName=Helvetica,FontSize=18,Bold=1,"
    "PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,BackColour=&H00000000,"
    "BorderStyle=1,Outline=2,Shadow=0,"
    "Alignment=2,MarginV=90"
)

# -------- Helpers ------------------------------------------------------------


def ffmpeg(cmd: list[str]) -> None:
    """Run an ffmpeg command quietly. On failure raise with the tail of its stderr:
    the exit code alone says nothing, and a reframe `vf` makes the command line 11 KB."""
    proc = subprocess.run(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
    if proc.returncode != 0:
        line = " ".join(cmd)
        shown = line if len(line) <= 400 else line[:400] + " …"
        raise RuntimeError(f"ffmpeg failed (exit {proc.returncode}): {shown}\n{proc.stderr.strip()[-1500:]}")


def resolve_grade_filter(grade_field: str | None) -> str:
    """The EDL's 'grade' field can be a preset name, a raw ffmpeg filter, or 'auto'.

    Returns the filter string to embed into the per-segment -vf chain.
    For 'auto', returns the sentinel "__AUTO__" which is resolved per-segment.
    """
    if not grade_field:
        return ""
    if grade_field == "auto":
        return "__AUTO__"
    # Preset names are short identifiers, filter strings contain '=' or ','.
    if re.fullmatch(r"[a-zA-Z0-9_\-]+", grade_field):
        try:
            return get_preset(grade_field)
        except KeyError as exc:
            raise ValueError(
                f"EDL grade {grade_field!r}: {exc.args[0]}. "
                "A raw ffmpeg filter needs '=' or ',', e.g. 'eq=contrast=1.1'."
            ) from exc
    return grade_field


def resolve_path(maybe_path: str, base: Path) -> Path:
    """Resolve a path that may be absolute or relative to `base`."""
    p = Path(maybe_path)
    if p.is_absolute():
        return p
    return (base / p).resolve()


def resolve_edl_file(maybe_path: str, edit_dir: Path, field: str) -> Path:
    """Resolve a file the EDL names (subtitles, ass, an overlay): relative to the
    EDL's directory, else the current directory (agents often write "edit/master.srt").
    A missing file is an error: rendering on without it silently ships a video
    without the captions, titles or animation."""
    candidates = [resolve_path(maybe_path, edit_dir)]
    if not Path(maybe_path).is_absolute():
        candidates.append(Path(maybe_path).resolve())
    for c in candidates:
        if c.exists():
            return c
    tried = ", ".join(str(c) for c in candidates)
    sys.exit(f"{field} file in EDL not found (tried {tried}). Fix the path in the EDL.")


# -------- HDR → SDR tone mapping (HLG / PQ sources) --------------------------
#
# iPhone defaults to HLG HDR in Rec.2020 (and many mirrorless cameras ship PQ).
# If the source is HDR and we only downconvert bit depth (yuv420p10le → yuv420p)
# without tone-mapping, the output is 8-bit but still carries HLG/PQ transfer
# metadata. Players that honor the metadata (screen recorders, most social
# upload re-encodes) interpret 8-bit values in an HDR container and the result
# looks oversaturated / blown out. QuickTime on macOS can hide this locally —
# screen recording and uploaded renders cannot.
#
# Fix: detect HDR via color_transfer and prepend a zscale+tonemap chain to the
# vf graph so the output is clean Rec.709 SDR.

HDR_TRANSFERS = {"smpte2084", "arib-std-b67"}  # PQ (HDR10) and HLG

TONEMAP_CHAIN = (
    "zscale=t=linear:npl=100,"
    "format=gbrpf32le,"
    "zscale=p=bt709,"
    "tonemap=tonemap=hable:desat=0,"
    "zscale=t=bt709:m=bt709:r=tv,"
    "format=yuv420p"
)


def is_hdr_source(video: Path) -> bool:
    """Return True if the source uses a PQ or HLG transfer function."""
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=color_transfer",
             "-of", "default=noprint_wrappers=1:nokey=1", str(video)],
            capture_output=True, text=True, check=True,
        )
        return out.stdout.strip() in HDR_TRANSFERS
    except subprocess.CalledProcessError:
        return False


def display_size(video: Path) -> tuple[int, int]:
    """(width, height) of the video as displayed, including rotation."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0",
         "-show_entries",
         "stream=width,height:stream_side_data=rotation",
         "-of", "json", str(video)],
        capture_output=True, text=True,
    )
    if out.returncode != 0:
        raise RuntimeError(f"ffprobe failed on {video}: {out.stderr.strip()[-400:]}")
    streams = json.loads(out.stdout).get("streams") or []
    if not streams:
        raise RuntimeError(f"no video stream in {video}")
    stream = streams[0]
    w, h = int(stream["width"]), int(stream["height"])

    # ffmpeg autorotates display-matrix side data before applying filters.
    # Swap coded dimensions for quarter-turns so the canvas is chosen from the
    # dimensions the filter actually sees. A plain metadata tag is
    # intentionally ignored because it does not guarantee autorotation.
    rotation = 0
    for side_data in stream.get("side_data_list") or []:
        if side_data.get("rotation") is not None:
            rotation = side_data["rotation"]
            break
    if int(round(float(rotation))) % 360 in (90, 270):
        w, h = h, w
    return w, h


def parse_size(value: str) -> tuple[int, int]:
    """--size WxH: even positive dimensions, as libx264 with yuv420p needs."""
    m = re.fullmatch(r"([0-9]+)x([0-9]+)", value.strip())
    if not m or int(m[1]) <= 0 or int(m[2]) <= 0 or int(m[1]) % 2 or int(m[2]) % 2:
        raise argparse.ArgumentTypeError("size must be WIDTHxHEIGHT with even numbers, e.g. 1080x1920")
    return int(m[1]), int(m[2])


def even(x: float) -> int:
    return max(2, 2 * round(x / 2))


def canvas_size(first_source: tuple[int, int], size: tuple[int, int] | None, draft: bool) -> tuple[int, int]:
    """The one frame size of the whole render. Every segment must share it, or the
    `-c copy` concat switches resolution midstream.

    Default: the first source's shape at 1080 on the short side; `size` overrides.
    A draft is scaled down to 720 on the short side.
    """
    w, h = size or first_source
    short = 720 if draft else 1080
    if size is None or (draft and min(w, h) > short):
        scale = short / min(w, h)
        w, h = even(w * scale), even(h * scale)
    return w, h


def parse_fps(value: str) -> str:
    """Validate and canonicalize an ffmpeg frame rate."""
    text = value.strip()
    if len(text) > 32 or not re.fullmatch(
        r"(?:[0-9]+(?:\.[0-9]+)?|[0-9]+/[0-9]+)", text
    ):
        raise argparse.ArgumentTypeError(
            "FPS must be a positive number or rational, e.g. 30 or 30000/1001"
        )
    try:
        rate = Fraction(text)
    except (ValueError, ZeroDivisionError) as exc:
        raise argparse.ArgumentTypeError(
            "FPS must be a positive number or rational, e.g. 30 or 30000/1001"
        ) from exc
    if rate <= 0:
        raise argparse.ArgumentTypeError("FPS must be greater than zero")
    # FFmpeg stores video rates as AVRational (signed 32-bit components).
    # Bounding the reduced fraction keeps every accepted canonical value safe
    # for ffmpeg and makes parse_fps(parse_fps(value)) idempotent.
    max_component = 2_147_483_647
    if rate.numerator > max_component or rate.denominator > max_component:
        raise argparse.ArgumentTypeError("FPS precision or magnitude is too large")
    return f"{rate.numerator}/{rate.denominator}"


def probe_source_fps(video: Path) -> str | None:
    """Return an ffmpeg-ready source rate.

    ``avg_frame_rate`` is the observed average and the better default for
    variable-frame-rate inputs, but a phone's average sits a hair off its nominal
    rate: an iPhone take averaging 276925/9233 (29.993) rendered as a 29.993 fps
    file with a 1/276925 timebase. So the nominal ``r_frame_rate`` wins when the
    two agree within 0.05%, and is the fallback when there is no average. Values
    are normalized to an exact rational so rates such as ``30000/1001`` survive.
    """
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "error", "-select_streams", "v:0",
             "-show_entries", "stream=avg_frame_rate,r_frame_rate",
             "-of", "json", str(video)],
            capture_output=True, text=True, check=True,
        )
        streams = json.loads(out.stdout).get("streams") or []
        if not streams:
            return None
        rates: dict[str, str] = {}
        for field in ("avg_frame_rate", "r_frame_rate"):
            value = streams[0].get(field)
            if value and value != "0/0":
                try:
                    rates[field] = parse_fps(value)
                except argparse.ArgumentTypeError:
                    continue
    except (subprocess.CalledProcessError, json.JSONDecodeError, OSError):
        return None
    avg, nominal = rates.get("avg_frame_rate"), rates.get("r_frame_rate")
    if avg and nominal and abs(Fraction(avg) / Fraction(nominal) - 1) < Fraction(1, 2000):
        return nominal
    return avg or nominal


def resolve_output_rate(edl: dict, edit_dir: Path, fps: str | None) -> str:
    """ONE output frame rate for the whole render.

    The lossless concat (Rule 2, `-c copy`) requires all segments to share a
    frame rate; probing per-segment would diverge for multi-source EDLs that mix
    rates (e.g. a 30fps and a 60fps source) and break the concat. Explicit --fps
    wins; otherwise preserve the first source's rate, 24 if it can't be probed.
    """
    if fps is not None:
        return parse_fps(str(fps))
    ranges = edl["ranges"]
    if not ranges:
        return "24"
    first_src = resolve_path(edl["sources"][ranges[0]["source"]], edit_dir)
    return probe_source_fps(first_src) or "24"


# -------- Output timeline (frame-quantized) ----------------------------------
#
# A segment can only hold whole frames. Extracting `-t 4.44` at 30 fps gives 134
# frames (4.467 s) of video next to 4.440 s of audio, and every EDL range adds up
# to one frame of drift: 62 ranges put captions 1.2 s late by the end. So every
# range is cut to a whole number of frames, and every output-time computation
# (master SRT, overlays, titles) uses the same quantized durations.


def segment_frames(start: float, end: float, rate: str) -> int:
    """Whole frames a range occupies in the output."""
    return max(1, round((end - start) * Fraction(rate)))


def segment_duration(start: float, end: float, rate: str) -> float:
    return float(segment_frames(start, end, rate) / Fraction(rate))


def output_offsets(edl: dict, rate: str) -> list[float]:
    """Output-timeline start of every EDL range."""
    offsets: list[float] = []
    t = 0.0
    for r in edl["ranges"]:
        offsets.append(t)
        t += segment_duration(float(r["start"]), float(r["end"]), rate)
    return offsets


def source_to_output(edl: dict, rate: str, source: str, t: float) -> float:
    """Map a source timestamp to the output timeline. A cut-out moment is an error."""
    for r, offset in zip(edl["ranges"], output_offsets(edl, rate)):
        start = float(r["start"])
        if r["source"] == source and start <= t < start + segment_duration(start, float(r["end"]), rate):
            return offset + (t - start)
    raise ValueError(f"{source} @ {t:.3f}s is not inside any EDL range (cut out)")


# -------- Per-segment extraction (Rule 2 + Rule 3) --------------------------


def extract_segment(
    source: Path,
    seg_start: float,
    frames: int,
    grade_filter: str,
    out_path: Path,
    *,
    preview: bool,
    draft: bool,
    rate: str,
    audio_track: int,
    canvas: tuple[int, int],
    audio_filter: str,
) -> None:
    """Extract a cut range as its own MOV with grade + audio edge fades baked in.

    `-ss` before `-i` for fast accurate seeking. The picture is fitted into the
    render's one `canvas`: a source of the same shape fills it, another shape (a
    landscape insert in a vertical cut) gets bars rather than losing its edges.
    Video is capped at exactly `frames` frames and audio at the same duration, so
    the segment is as long as `segment_duration()` says and the timeline adds up.
    Streams are mapped explicitly: an iPhone file carries stereo AAC next to
    spatial APAC, and ffmpeg's default pick is the stream with the most channels.
    Audio stays PCM: an AAC segment carries 1024 samples of encoder delay plus
    padding to a whole AAC frame, and the `-c copy` concat stacks them, so speech
    fell 20–35 ms further behind the picture at every cut (1.26 s after 62 cuts).
    The mix is encoded to AAC once, at the end.

    Quality ladder:
      - final (default): libx264 fast CRF 20
      - preview:         libx264 medium CRF 22 (evaluable for QC)
      - draft:           libx264 ultrafast CRF 28 on a 720p canvas (cut-point check only)
    """
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_name(f"{out_path.stem}.{os.getpid()}.part{out_path.suffix}")
    duration = float(frames / Fraction(rate))

    w, h = canvas
    fit = (f"scale={w}:{h}:force_original_aspect_ratio=decrease:force_divisible_by=2,"
           f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1")

    # The output rate first: a reframe `vf` times its motion by the frame counter
    # `on`, which otherwise counts source frames (a 60 fps source rendered at 30
    # ran every move at double speed). start_time=0: after -ss the first source
    # frame can land a few ms past 0, and without it the segment starts a frame
    # late, leaving a hole at the join and the picture 33 ms behind its sound.
    vf_parts: list[str] = [f"fps={rate}:start_time=0"]
    if is_hdr_source(source):
        vf_parts.append(TONEMAP_CHAIN)
    vf_parts.append(fit)
    if grade_filter:
        vf_parts.append(grade_filter)
    vf = ",".join(vf_parts)

    if draft:
        preset, crf = "ultrafast", "28"
    elif preview:
        preset, crf = "medium", "22"
    else:
        preset, crf = "fast", "20"

    n_audio = count_audio_tracks(source)
    if n_audio == 0:
        # A source without sound (screen capture, B-roll) gets silence: every
        # segment needs the same streams for the concat.
        audio_in = ["-f", "lavfi", "-t", f"{duration:.6f}", "-i", "anullsrc=r=48000:cl=stereo"]
        audio_map = "1:a:0"
    elif audio_track < n_audio:
        audio_in, audio_map = [], f"0:a:{audio_track}"
    else:
        raise ValueError(
            f"{source.name} has {n_audio} audio track(s), the EDL asks for audio_track {audio_track} (zero-based)"
        )

    cmd = [
        "ffmpeg", "-y",
        "-ss", f"{seg_start:.6f}",
        "-i", str(source),
        *audio_in,
        "-t", f"{duration:.6f}",
        "-map", "0:v:0", "-map", audio_map,
        "-vf", vf,
        "-af", audio_filter,
        "-frames:v", str(frames),
        "-c:v", "libx264", "-preset", preset, "-crf", crf,
        "-pix_fmt", "yuv420p",
        "-c:a", "pcm_s16le", "-ar", "48000", "-ac", "2",
        str(tmp),
    ]
    # The cache trusts any file under the final name, and a failed or interrupted
    # ffmpeg still leaves a valid but short file, so only a finished segment gets it.
    try:
        ffmpeg(cmd)
        tmp.replace(out_path)
    finally:
        tmp.unlink(missing_ok=True)


def seamless_joins(ranges: list[dict], durations: list[float]) -> list[bool]:
    """joins[i]: range i picks up where range i-1's rendered frames end, in the same
    source, with no time removed (within 2 ms: EDL times are rounded to the
    millisecond). tighten.py splits long ranges this way only to change the shot."""
    joins = [False]
    for prev, r, prev_duration in zip(ranges, ranges[1:], durations):
        joins.append(
            r["source"] == prev["source"]
            and abs(float(r["start"]) - (float(prev["start"]) + prev_duration)) < 0.002
        )
    return joins


def edge_fades(duration: float, fade_in: bool, fade_out: bool) -> str:
    """30ms audio fades at a segment's cut edges (Rule 3): they prevent pops where time
    was removed. A seamless join gets none: the sound runs on, and a fade would
    dip it to silence for 60 ms."""
    fades: list[str] = []
    if fade_in:
        fades.append("afade=t=in:st=0:d=0.03")
    if fade_out:
        fades.append(f"afade=t=out:st={max(0.0, duration - 0.03):.3f}:d=0.03")
    return ",".join(fades) or "anull"


# Bump when the extract command changes: it invalidates every cached segment.
EXTRACT_VERSION = 5


def segment_cache_key(
    source: Path, start: float, frames: int, rate: str, seg_filter: str, quality: str, audio_track: int,
    canvas: tuple[int, int], audio_filter: str,
) -> str:
    """Everything that shapes a segment's pixels and samples, hashed.

    The source's size and mtime stand in for its content, so re-exporting a take
    under the same name still invalidates its segments.
    """
    st = source.stat()
    payload = json.dumps([
        EXTRACT_VERSION, str(source), st.st_size, st.st_mtime_ns,
        round(start, 6), frames, rate, seg_filter, quality, audio_track, list(canvas), audio_filter,
    ])
    return hashlib.sha1(payload.encode()).hexdigest()[:12]


def extract_all_segments(
    edl: dict,
    edit_dir: Path,
    preview: bool,
    draft: bool = False,
    fps: str | None = None,
    *,
    canvas: tuple[int, int],
) -> list[Path]:
    """Extract every EDL range into edit_dir/clips_<quality>/seg_<src>_<start>_<key>.mov.
    Returns the ordered list of segment paths.

    The clips dir is a content-addressed cache shared by every EDL in the edit
    dir: a segment whose key already has a file is reused, so re-rendering after
    changing one range re-extracts one range, and a sub-EDL (a part, a teaser)
    renders from the full cut's segments. Nothing is pruned automatically, since
    another EDL may still need a file; the clips dirs can be deleted at any time.

    If the EDL `grade` is "auto", analyze each segment range with
    `auto_grade_for_clip` and apply a per-segment subtle correction.
    Otherwise, apply the same preset/raw filter to every segment.
    """
    resolved = resolve_grade_filter(edl.get("grade"))
    is_auto = resolved == "__AUTO__"
    quality = "draft" if draft else ("preview" if preview else "final")
    clips_dir = edit_dir / (
        "clips_draft" if draft else ("clips_preview" if preview else "clips_graded")
    )
    clips_dir.mkdir(parents=True, exist_ok=True)

    ranges = edl["ranges"]
    sources = edl["sources"]
    audio_track = int(edl.get("audio_track", 0))
    out_rate = resolve_output_rate(edl, edit_dir, fps)

    durations = [segment_duration(float(r["start"]), float(r["end"]), out_rate) for r in ranges]
    try:
        zooms = zoom_filters(ranges, durations, out_rate, edl.get("reframe") or {})
    except ValueError as exc:
        sys.exit(str(exc))

    seg_paths: list[Path] = []
    print(f"extracting {len(ranges)} segment(s) → {clips_dir.name}/  @ {out_rate} fps"
          f"{' (forced)' if fps is not None else ' (from source)'}, {canvas[0]}x{canvas[1]}")
    if is_auto:
        print("  (auto-grade per segment: analyzing each range)")
    joins = seamless_joins(ranges, durations)
    starts: list[float] = []
    reused = 0
    for i, r in enumerate(ranges):
        src_name = r["source"]
        src_path = resolve_path(sources[src_name], edit_dir)
        end = float(r["end"])
        frames = segment_frames(float(r["start"]), end, out_rate)
        duration = durations[i]
        # a seamless follower starts exactly where the previous segment's frames end,
        # not at its millisecond-rounded EDL start, so the sound carries on sample-exact
        start = starts[i - 1] + durations[i - 1] if joins[i] else float(r["start"])
        starts.append(start)
        next_joins = i + 1 < len(ranges) and joins[i + 1]
        audio_filter = edge_fades(duration, fade_in=not joins[i], fade_out=not next_joins)

        if is_auto:
            # measured on what the grade acts on: an HDR source after its tone mapping
            seg_filter, _stats = auto_grade_for_clip(
                src_path, start=start, duration=duration, verbose=False,
                pre_filter=TONEMAP_CHAIN if is_hdr_source(src_path) else "",
            )
        else:
            seg_filter = resolved
        # The zoom from the range's `frame` target, then its own `vf`, ride after the
        # canvas fit and grade, inside the same extract (Rule 2)
        if zooms and str(r.get("vf", "")).startswith("perspective="):
            sys.exit(f"range {i} has a `frame` target and a zoom baked into `vf` by the old "
                     "reframe.py: drop the `vf`, render.py now zooms from `frame` itself")
        seg_filter = ",".join(f for f in (seg_filter, zooms[i] if zooms else "", r.get("vf", "")) if f)

        key = segment_cache_key(src_path, start, frames, out_rate, seg_filter, quality, audio_track, canvas,
                                audio_filter)
        out_path = clips_dir / f"seg_{src_name}_{start:09.3f}_{key}.mov"
        note = r.get("beat") or r.get("note") or ""
        cached = out_path.exists()
        print(f"  [{i:02d}] {src_name}  {start:7.2f}-{end:7.2f}  ({duration:5.2f}s)  "
              f"{'cached  ' if cached else ''}{note}")
        if is_auto:
            print(f"        grade: {seg_filter or '(none)'}")
        if cached:
            reused += 1
        else:
            extract_segment(src_path, start, frames, seg_filter, out_path,
                            preview=preview, draft=draft, rate=out_rate, audio_track=audio_track,
                            canvas=canvas, audio_filter=audio_filter)
        seg_paths.append(out_path)

    print(f"  {reused} reused, {len(ranges) - reused} extracted")
    return seg_paths


# -------- Lossless concat ----------------------------------------------------


def concat_segments(segment_paths: list[Path], out_path: Path) -> None:
    """Lossless concat via the concat demuxer. No re-encode; the audio is still PCM."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    concat_list = out_path.with_suffix(".concat.txt")
    # a quote inside a quoted concat path is written as '\''
    concat_list.write_text("".join(
        "file '" + str(p.resolve()).replace("'", r"'\''") + "'\n" for p in segment_paths
    ))

    cmd = [
        "ffmpeg", "-y",
        "-f", "concat", "-safe", "0",
        "-i", str(concat_list),
        "-c", "copy",
        str(out_path),
    ]
    print(f"concat → {out_path.name}")
    ffmpeg(cmd)
    concat_list.unlink(missing_ok=True)


# -------- Master SRT (Rule 5) ------------------------------------------------


PUNCT_BREAK = set(".,!?;:")


def _srt_timestamp(seconds: float) -> str:
    total_ms = int(round(seconds * 1000))
    h, rem = divmod(total_ms, 3600_000)
    m, rem = divmod(rem, 60_000)
    s, ms = divmod(rem, 1000)
    return f"{h:02d}:{m:02d}:{s:02d},{ms:03d}"


def _words_in_range(transcript: dict, t_start: float, t_end: float) -> list[dict]:
    out: list[dict] = []
    for w in transcript.get("words", []):
        if w.get("type") != "word":
            continue
        ws = w.get("start")
        we = w.get("end")
        if ws is None or we is None:
            continue
        if we <= t_start or ws >= t_end:
            continue
        out.append(w)
    return out


CHUNK_WORDS = 2         # target words per cue
CHUNK_MAX_WORDS = 3     # a too-short chunk may grow to this many words
CHUNK_MIN_S = 0.35      # a cue shorter than this reads as a flash
CHUNK_PAUSE_S = 0.3     # a gap this long between words ends the cue


def chunk_words(words: list[dict]) -> list[list[dict]]:
    """Group transcript words into caption cues.

    A cue closes on trailing punctuation or on a pause before the next word.
    Otherwise it closes at CHUNK_WORDS words, unless it would be on screen for
    less than CHUNK_MIN_S; then it takes up to CHUNK_MAX_WORDS words. So
    "does | is" across a pause stays split, and fast "what a" does not flash.
    """
    words = [w for w in words if (w.get("text") or "").strip()]
    chunks: list[list[dict]] = []
    current: list[dict] = []
    for i, w in enumerate(words):
        current.append(w)
        text = w["text"].strip()
        nxt = words[i + 1] if i + 1 < len(words) else None
        gap = (nxt["start"] - w["end"]) if nxt else 0.0
        dur = w["end"] - current[0]["start"]
        if (
            nxt is None
            or text[-1] in PUNCT_BREAK
            or gap >= CHUNK_PAUSE_S
            or len(current) >= CHUNK_MAX_WORDS
            or (len(current) >= CHUNK_WORDS and dur >= CHUNK_MIN_S)
        ):
            chunks.append(current)
            current = []
    return chunks


def build_master_srt(edl: dict, edit_dir: Path, out_path: Path, rate: str) -> None:
    """Build an output-timeline SRT from per-source transcripts.

    - phrase-aware ~2-word chunks (see chunk_words)
    - UPPERCASE text
    - Output times computed as word.start - segment_start + segment_offset,
      offsets from the frame-quantized segment durations the render produces
    """
    transcripts_dir = edit_dir / "transcripts"

    entries: list[tuple[float, float, str]] = []
    seg_offset = 0.0

    for r in edl["ranges"]:
        src_name = r["source"]
        seg_start = float(r["start"])
        seg_duration = segment_duration(seg_start, float(r["end"]), rate)
        seg_end = seg_start + seg_duration

        tr_path = transcripts_dir / f"{src_name}.json"
        if not tr_path.exists():
            print(f"  no transcript for {src_name}, skipping captions for this segment")
            seg_offset += seg_duration
            continue

        transcript = json.loads(tr_path.read_text())
        words_in_seg = _words_in_range(transcript, seg_start, seg_end)

        for chunk in chunk_words(words_in_seg):
            local_start = max(seg_start, chunk[0].get("start", seg_start))
            local_end = min(seg_end, chunk[-1].get("end", seg_end))
            out_start = max(0.0, local_start - seg_start) + seg_offset
            out_end = max(0.0, local_end - seg_start) + seg_offset
            if out_end <= out_start:
                out_end = out_start + 0.4
            text = " ".join((w.get("text") or "").strip() for w in chunk)
            text = re.sub(r"\s+", " ", text).strip()
            # Strip trailing punctuation for cleaner uppercase look
            text = text.rstrip(",;:")
            text = text.upper()
            entries.append((out_start, out_end, text))

        seg_offset += seg_duration

    # Sort and write as SRT
    entries.sort(key=lambda e: e[0])
    lines: list[str] = []
    for i, (a, b, t) in enumerate(entries, start=1):
        lines.append(str(i))
        lines.append(f"{_srt_timestamp(a)} --> {_srt_timestamp(b)}")
        lines.append(t)
        lines.append("")
    out_path.write_text("\n".join(lines))
    print(f"master SRT → {out_path.name} ({len(entries)} cues)")


# -------- Loudness normalization (social-ready audio) -----------------------


# Social-media standard: -14 LUFS integrated, -1 dBTP peak, LRA 11 LU.
# Matches YouTube / Instagram / TikTok / X / LinkedIn normalization targets.
LOUDNORM_I = -14.0
LOUDNORM_TP = -1.0
LOUDNORM_LRA = 11.0
# The AAC encode after loudnorm raises true peak by 0.3–0.9 dB depending on the
# material (measured on one speech track rendered twice: limiter -1.5 gave -1.1
# and -0.7 dBTP after AAC 192k; limiter -2.0 gave -1.0 and -1.1). Limit lower so
# the delivered file meets LOUDNORM_TP.
AAC_TP_HEADROOM = 1.0
LIMITER_TP = LOUDNORM_TP - AAC_TP_HEADROOM
# loudnorm's own limiter does not hold its TP target when it falls back to
# dynamic mode on a short file (a 54 s part needing +6.4 dB came out at
# -0.3 dBTP). loudnorm emits 192 kHz, where sample peaks approximate true
# peaks, so a brickwall there, before the drop to 48 kHz, holds it: the same
# part then measured -1.5 dBTP after AAC, loudness unchanged.
TP_GUARD = (
    f",alimiter=limit={10 ** (LIMITER_TP / 20):.4f}:level=false:attack=1:release=50"
    ",aresample=48000"
)


def measure_loudness(video_path: Path) -> dict[str, str] | None:
    """Run ffmpeg loudnorm first pass and parse the JSON measurement.

    Returns a dict with measured_i, measured_tp, measured_lra, measured_thresh,
    target_offset, or None if measurement failed.
    """
    filter_str = (
        f"loudnorm=I={LOUDNORM_I}:TP={LIMITER_TP}:LRA={LOUDNORM_LRA}:print_format=json"
    )
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-nostats",
        "-i", str(video_path),
        "-af", filter_str,
        "-vn", "-f", "null", "-",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    # loudnorm prints the JSON to stderr at the end of the run
    stderr = proc.stderr

    # Find the JSON block — loudnorm output contains a `{ ... }` block
    start = stderr.rfind("{")
    end = stderr.rfind("}")
    if start == -1 or end == -1 or end <= start:
        return None
    try:
        data = json.loads(stderr[start : end + 1])
    except json.JSONDecodeError:
        return None
    needed = {"input_i", "input_tp", "input_lra", "input_thresh", "target_offset"}
    if not needed.issubset(data.keys()):
        return None
    return data


def apply_loudnorm_two_pass(
    input_path: Path,
    output_path: Path,
    preview: bool = False,
) -> bool:
    """Run two-pass loudnorm on input_path, write normalized copy to output_path.

    Returns True on success, False if measurement failed (caller should fall
    back to copying the input unchanged).

    In preview mode, skips the measurement pass and uses a one-pass approximation
    for speed. Final mode always does the proper two-pass.
    """
    if preview:
        # One-pass approximation — faster, slightly less accurate.
        filter_str = f"loudnorm=I={LOUDNORM_I}:TP={LIMITER_TP}:LRA={LOUDNORM_LRA}" + TP_GUARD
        cmd = [
            "ffmpeg", "-y", "-hide_banner", "-nostats",
            "-i", str(input_path),
            "-c:v", "copy",
            "-af", filter_str,
            "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
            "-movflags", "+faststart",
            str(output_path),
        ]
        print(f"  loudnorm (1-pass preview) → {output_path.name}")
        ffmpeg(cmd)
        return True

    # Full two-pass
    print(f"  loudnorm pass 1: measuring {input_path.name}")
    measurement = measure_loudness(input_path)
    if measurement is None:
        print("  loudnorm measurement failed — falling back to 1-pass")
        return apply_loudnorm_two_pass(input_path, output_path, preview=True)

    print(f"    measured: I={measurement['input_i']} LUFS  "
          f"TP={measurement['input_tp']}  LRA={measurement['input_lra']}")

    filter_str = (
        f"loudnorm=I={LOUDNORM_I}:TP={LIMITER_TP}:LRA={LOUDNORM_LRA}"
        f":measured_I={measurement['input_i']}"
        f":measured_TP={measurement['input_tp']}"
        f":measured_LRA={measurement['input_lra']}"
        f":measured_thresh={measurement['input_thresh']}"
        f":offset={measurement['target_offset']}"
        f":linear=true"
        + TP_GUARD
    )
    cmd = [
        "ffmpeg", "-y", "-hide_banner", "-nostats",
        "-i", str(input_path),
        "-c:v", "copy",
        "-af", filter_str,
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart",
        str(output_path),
    ]
    print(f"  loudnorm pass 2: normalizing → {output_path.name}")
    ffmpeg(cmd)
    return True


def alpha_decoder(overlay: Path) -> list[str]:
    """Input options that keep a WebM overlay's alpha. ffmpeg's native VP8/VP9
    decoders drop it, and a transparent overlay then covers the whole frame."""
    out = subprocess.run(
        ["ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries", "stream=codec_name",
         "-of", "default=noprint_wrappers=1:nokey=1", str(overlay)],
        capture_output=True, text=True, check=True,
    )
    codec = out.stdout.strip()
    if codec == "vp9":
        return ["-c:v", "libvpx-vp9"]
    if codec == "vp8":
        return ["-c:v", "libvpx"]
    return []


def filter_path(path: Path) -> str:
    """A path as a filter option value, escaped once for the option parser and once
    for the filtergraph (ffmpeg-filters, "Notes on filtergraph escaping"). Quoting
    alone breaks on a path with an apostrophe in it."""
    option_level = re.sub(r"([\\':])", r"\\\1", str(path.resolve()))
    return re.sub(r"([\\'\[\],;])", r"\\\1", option_level)


def encode_audio(input_path: Path, output_path: Path) -> None:
    """Copy the video and encode the PCM mix to AAC (the --no-loudnorm path)."""
    ffmpeg([
        "ffmpeg", "-y", "-i", str(input_path),
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", "192k", "-ar", "48000",
        "-movflags", "+faststart",
        str(output_path),
    ])


# -------- Final compositing (Rule 1 + Rule 4) -------------------------------


def build_final_composite(
    base_path: Path,
    overlays: list[dict],
    ass_path: Path | None,
    subtitles_path: Path | None,
    out_path: Path,
    edit_dir: Path,
    canvas: tuple[int, int],
) -> None:
    """Final pass: base → overlays (PTS-shifted) → designed ASS titles → subtitles LAST → out.

    Overlays are full-frame animations: each is scaled to the canvas, so one rendered
    at 1080p still lines up on a 720p draft.

    The EDL's `ass` file is burned as written: unlike subtitles it gets no
    force_style, so its own fonts, positions and animation tags survive.
    `out_path` is an intermediate MOV: the audio is copied as PCM and encoded once, later.
    """
    has_overlays = bool(overlays)
    has_ass = ass_path is not None
    has_subs = subtitles_path is not None and subtitles_path.exists()

    inputs: list[str] = ["-i", str(base_path)]
    for ov in overlays:
        ov_path = resolve_path(ov["file"], edit_dir)
        inputs += [*alpha_decoder(ov_path), "-i", str(ov_path)]

    filter_parts: list[str] = []
    # PTS-shift every overlay so its frame 0 lands at start_in_output
    w, h = canvas
    for idx, ov in enumerate(overlays, start=1):
        t = float(ov["start_in_output"])
        filter_parts.append(f"[{idx}:v]scale={w}:{h},setpts=PTS-STARTPTS+{t}/TB[a{idx}]")

    # Chain overlays on top of base
    current = "[0:v]"
    for idx, ov in enumerate(overlays, start=1):
        t = float(ov["start_in_output"])
        dur = float(ov["duration"])
        end = t + dur
        next_label = f"[v{idx}]"
        filter_parts.append(
            f"{current}[a{idx}]overlay=enable='between(t,{t:.3f},{end:.3f})'{next_label}"
        )
        current = next_label

    if ass_path is not None:
        filter_parts.append(f"{current}ass={filter_path(ass_path)}[vass]")
        current = "[vass]"

    # Subtitles LAST — Rule 1
    if subtitles_path is not None and has_subs:
        filter_parts.append(
            f"{current}subtitles={filter_path(subtitles_path)}:force_style='{SUB_FORCE_STYLE}'[outv]"
        )
        out_label = "[outv]"
    else:
        # Rename the last overlay output to [outv] for consistency
        if has_overlays or has_ass:
            filter_parts.append(f"{current}null[outv]")
            out_label = "[outv]"
        else:
            out_label = "[0:v]"

    filter_complex = ";".join(filter_parts)

    cmd = [
        "ffmpeg", "-y",
        *inputs,
        "-filter_complex", filter_complex,
        "-map", out_label,
        "-map", "0:a",
        "-c:v", "libx264", "-preset", "fast", "-crf", "18",
        "-pix_fmt", "yuv420p",
        "-c:a", "copy",
        str(out_path),
    ]
    print(f"compositing → {out_path.name}")
    print(f"  overlays: {len(overlays)}, ass titles: {'yes' if has_ass else 'no'}, "
          f"subtitles: {'yes' if has_subs else 'no'}")
    ffmpeg(cmd)


# -------- Main ---------------------------------------------------------------


def main() -> None:
    ap = argparse.ArgumentParser(description="Render a video from an EDL")
    ap.add_argument("edl", type=Path, help="Path to edl.json")
    ap.add_argument("-o", "--output", type=Path, required=True, help="Output video path")
    ap.add_argument(
        "--preview",
        action="store_true",
        help="Preview mode: 1080p, medium, CRF 22 — evaluable for QC, faster than final.",
    )
    ap.add_argument(
        "--draft",
        action="store_true",
        help="Draft mode: 720p, ultrafast, CRF 28 — cut-point verification only.",
    )
    ap.add_argument(
        "--build-subtitles",
        action="store_true",
        help="Build <output>.srt from transcripts + EDL offsets before compositing",
    )
    ap.add_argument(
        "--no-subtitles",
        action="store_true",
        help="Skip subtitles even if the EDL references one",
    )
    ap.add_argument(
        "--no-loudnorm",
        action="store_true",
        help="Skip audio loudness normalization. Default is on (-14 LUFS, -1 dBTP, LRA 11).",
    )
    ap.add_argument(
        "--size",
        type=parse_size,
        default=None,
        help="Output frame WIDTHxHEIGHT, e.g. 3840x2160 or 1080x1080. Default: the first "
             "source's shape at 1080 on the short side (720 with --draft). Sources of "
             "another shape are fitted inside with bars.",
    )
    ap.add_argument(
        "--fps",
        type=parse_fps,
        default=None,
        help="Output frame rate. Default: preserve the source's frame rate "
             "(falls back to 24 if it can't be probed). Pass e.g. --fps 30 or "
             "--fps 30000/1001 to force.",
    )
    args = ap.parse_args()

    edl_path = args.edl.resolve()
    if not edl_path.exists():
        sys.exit(f"edl not found: {edl_path}")

    edl = json.loads(edl_path.read_text())
    edit_dir = edl_path.parent
    out_path = args.output.resolve()

    rate = resolve_output_rate(edl, edit_dir, args.fps)
    first_source = resolve_path(edl["sources"][edl["ranges"][0]["source"]], edit_dir)
    canvas = canvas_size(display_size(first_source), args.size, args.draft)

    # Every file the EDL names is checked before minutes of extraction, not after.
    subs_path: Path | None = None
    if not args.no_subtitles:
        if args.build_subtitles:
            srt_path = out_path.with_suffix(".srt")
            build_master_srt(edl, edit_dir, srt_path, rate)
            subs_path = srt_path
        elif edl.get("subtitles"):
            subs_path = resolve_edl_file(edl["subtitles"], edit_dir, "subtitles")
    ass_path = resolve_edl_file(edl["ass"], edit_dir, "ass") if edl.get("ass") else None
    overlays = [
        {**ov, "file": str(resolve_edl_file(ov["file"], edit_dir, "overlay"))}
        for ov in edl.get("overlays") or []
    ]

    # 1. Extract per-segment (auto-grade per range if EDL grade is "auto")
    segment_paths = extract_all_segments(
        edl, edit_dir, preview=args.preview, draft=args.draft, fps=args.fps, canvas=canvas
    )

    # 2. Concat → base. Intermediates are named after the output, so renders of
    # several EDLs from one edit dir (parts, a teaser) can run at the same time.
    base_path = out_path.with_suffix(".base.mov")
    concat_segments(segment_paths, base_path)

    # 3. Composite (overlays + ASS titles + subtitles LAST), audio still PCM
    if overlays or ass_path or subs_path:
        composite_path = out_path.with_suffix(".composite.mov")
        build_final_composite(base_path, overlays, ass_path, subs_path, composite_path, edit_dir, canvas)
    else:
        composite_path = base_path

    # 4. The one AAC encode of the audio
    if args.no_loudnorm:
        encode_audio(composite_path, out_path)
    else:
        print(f"loudness normalization → social-ready ({LOUDNORM_I:g} LUFS / {LOUDNORM_TP:g} dBTP "
              f"delivered, limiter at {LIMITER_TP:g} / LRA {LOUDNORM_LRA:g})")
        apply_loudnorm_two_pass(composite_path, out_path, preview=args.draft)
    composite_path.unlink(missing_ok=True)
    base_path.unlink(missing_ok=True)

    size_mb = out_path.stat().st_size / (1024 * 1024)
    print(f"\ndone: {out_path} ({size_mb:.1f} MB)")


if __name__ == "__main__":
    main()

---
name: cutroom
description: Edit any video by conversation. Transcribe, cut pauses and slips, reframe with smooth punch-ins, color grade, generate overlay animations, burn subtitles and designed titles - for talking heads, vlogs, montages, tutorials, travel, interviews. No presets, no menus. Ask questions, confirm the plan, execute, iterate, persist. Production-correctness rules are hard; everything else is artistic freedom. Russian triggers - «смонтируй видео», «нарежь видео», «вырежи паузы», «собери ролик из дублей», «склей дубли», «вшей субтитры», «сделай цветокор», «наложи анимацию на видео», «добавь подписи». NOT for merely watching a video or pulling its transcript to answer questions about it - that is a video-watching skill's job; transcription here is a paid ElevenLabs call, or free and local via mlx-whisper.
---

# cutroom

Conversational video editing, based on [browser-use/video-use](https://github.com/browser-use/video-use).

## Principle

1. **LLM reasons from raw transcript + on-demand visuals.** The only derived artifact that earns its keep is a packed phrase-level transcript (`takes_packed.md`). Everything else — filler tagging, retake detection, shot classification, emphasis scoring — you derive at decision time.
2. **Audio is primary, visuals follow.** Cut candidates come from speech boundaries and silence gaps. Drill into visuals only at decision points.
3. **Ask → confirm → execute → iterate → persist.** Never touch the cut until the user has confirmed the strategy in plain English.
4. **Generalize.** Do not assume what kind of video this is. Look at the material, ask the user, then edit.
5. **Artistic freedom is the default.** Every specific value, preset, font, color, duration, pitch structure, and technique in this document is a *worked example* from one proven video — not a mandate. Read them to understand what's possible and why each worked. Then make your own taste calls based on what the material actually is and what the user actually wants. **The only things you MUST do are in the Hard Rules section below.** Everything else is yours.
6. **Invent freely.** If the material calls for a technique not described here — split-screen, picture-in-picture, lower-third identity cards, reaction cuts, speed ramps, freeze frames, crossfades, match cuts, L-cuts, J-cuts, speed ramps over breath, whatever — build it. The helpers are ffmpeg and PIL. They can do anything the format supports. Do not wait for permission.
7. **Verify your own output before showing it to the user.** If you wouldn't ship it, don't present it.

## Hard Rules (production correctness — non-negotiable)

These are the things where deviation produces silent failures or broken output. They are not taste, they are correctness. Memorize them.

1. **Subtitles are applied LAST in the filter chain**, after every overlay. Otherwise overlays hide captions. Silent failure.
2. **Per-segment extract → lossless `-c copy` concat**, not single-pass filtergraph. Otherwise you double-encode every segment when overlays are added. Segment audio stays PCM and is encoded to AAC once, on the finished mix: an AAC segment carries encoder delay that the concat stacks up, and speech fell 20–35 ms further behind the picture at every cut.
3. **30ms audio fades at every segment boundary where time was cut** (`afade=t=in:st=0:d=0.03,afade=t=out:st={dur-0.03}:d=0.03`). Otherwise audible pops at every cut. A range that continues the previous one with no time removed (`tighten.py` splits long ranges on whole frames to change the shot) joins seamlessly with no fade: `render.py` detects it.
4. **Overlays use `setpts=PTS-STARTPTS+T/TB`** to shift the overlay's frame 0 to its window start. Otherwise you see the middle of the animation during the overlay window.
5. **Master SRT uses output-timeline offsets**: `output_time = word.start - segment_start + segment_offset`. Otherwise captions misalign after segment concat. Offsets come from the rendered, whole-frame segment durations, not from EDL `end - start`: `render.py` cuts every range to `round(duration × fps)` frames, and summing raw EDL durations drifts about a frame per cut (1.2 s after 62 ranges). Anything else timed to the output (titles, overlays) goes through `render.source_to_output(edl, rate, source, t)`.
6. **Never cut inside a word.** Snap every cut edge to a word boundary from the Scribe transcript.
7. **Pad every cut edge.** Working window: 30–200ms. Scribe timestamps drift 50–100ms — padding absorbs the drift. Tighter for fast-paced, looser for cinematic.
8. **Word-level verbatim ASR only.** Never SRT/phrase mode (loses sub-second gap data). Never normalized fillers (loses editorial signal).
9. **Cache transcripts per source.** Never re-transcribe unless the source file itself changed.
10. **Parallel sub-agents for multiple animations when available and permitted.** In Claude Code use `Agent`; in Codex use the available collaboration or `spawn_agent` tool. Give each worker a separate slot directory. If the host has no delegation tool or disallows it, do the work in the main session and say that it will run sequentially.
11. **Strategy confirmation before execution.** Never touch the cut until the user has approved the plain-English plan.
12. **All session outputs in `<videos_dir>/edit/`.** Never write inside the skill directory: it is the plugin's install cache and is replaced on every update.
13. **Cost confirmation before `transcribe.py` or `transcribe_batch.py`.** Scribe bills per minute, one file or many. Name the number of files and the total minutes that are not already cached in `transcripts/`, then wait for the user's go-ahead. This applies to the Inventory step too. `transcribe_local.py` is free and needs none.

Everything else in this document is a worked example. Deviate whenever the material calls for it.

## Directory layout

The skill lives in the plugin's `skills/cutroom/`. User footage lives wherever they put it. All session outputs go into `<videos_dir>/edit/`.

```
<videos_dir>/
├── <source files, untouched>
└── edit/
    ├── project.md               ← memory; appended every session
    ├── takes_packed.md          ← phrase-level transcripts, the LLM's primary reading view
    ├── edl.json                 ← cut decisions
    ├── transcripts/<name>.json  ← cached raw Scribe JSON
    ├── animations/slot_<id>/    ← per-animation source + render + reasoning
    ├── clips_graded/            ← segment cache of final renders (clips_preview/, clips_draft/ for the other modes)
    ├── master.srt               ← output-timeline subtitles (`--build-subtitles` writes `<output>.srt`)
    ├── downloads/               ← yt-dlp outputs
    ├── verify/                  ← debug frames / timeline PNGs
    ├── preview.mp4
    └── final.mp4
```

## Setup

The plugin brings the skill and its helpers; the tools below come from the system. On cold start verify:

- A transcription path. Free and local: the `mlx_whisper` CLI (`uv tool install mlx-whisper`, Apple Silicon only; the model downloads on first use). Paid and better at fillers and timing: `ELEVENLABS_API_KEY` for Scribe, from the environment or, failing that, from `.env` in the current directory (a `.env` inside the skill directory is not read: plugin updates replace it). Never ask the user to paste a key into the chat: the transcript of this session is stored, so ask them to export it from their password manager or write the `.env` themselves (`$EDITOR .env`, then `chmod 600`), and wait. Without a key, use `transcribe_local.py` and say what it loses.
- `uv` on PATH: it builds the helpers' environment (see Helpers).
- `ffmpeg` + `ffprobe` on PATH.
- `ffmpeg -h filter=subtitles` and `ffmpeg -h filter=zscale` both print a filter description rather than `Unknown filter` (the exit code is 0 either way, read the text). Homebrew's default `ffmpeg` bottle is built without libass and libzimg, and then burning subtitles and tone-mapping HDR (iPhone HLG) sources fail at render time, not at plan time. If either filter is missing, stop and tell the user to reinstall ffmpeg with those libraries.
- Node.js + npm available if the session needs HyperFrames or Remotion slots. HyperFrames currently requires Node.js 22+.
- `yt-dlp`, HyperFrames, Remotion, Manim installed only on first use.
- First-use animation setup happens inside the slot directory, never in the skill directory. HyperFrames can be invoked with `npx --yes hyperframes ...`; Remotion can be scaffolded with `npx create-video@latest` or installed as a project-local dependency before using its `remotion render` command.

Helpers (`helpers/transcribe.py`, `helpers/render.py`, etc.) live alongside this SKILL.md. Resolve their paths relative to the directory containing this file (`<skill dir>` below).

## Helpers

Run every helper through uv against the skill's own project: `uv run --frozen --project <skill dir> python <skill dir>/helpers/<script>.py`.
uv builds the environment exactly from `uv.lock` (`--frozen`: the lock is never re-resolved or rewritten) on first use and again after a plugin update replaces the directory; nothing is installed globally.
The system `python3` has none of the deps (numpy, requests), and `render.py` / `grade.py`
print `--help` under it anyway, which makes a broken environment look healthy.

- **`transcribe.py <video>`** — single-file Scribe call. `--num-speakers N` optional. Cached.
- **`transcribe_batch.py <videos_dir>`** — 4-worker parallel transcription. Use for multi-take.
- **`transcribe_local.py <video_or_dir> --language <code>`** — free local alternative to Scribe via the `mlx_whisper` CLI (whisper-large-v3-turbo on the Apple GPU), any language Whisper knows, writes the same Scribe-shaped JSON into the same cache, no cost confirmation. No diarization and no audio events; for `ru` and `en` a filler prompt keeps most "эм"/"э-э" in the text (other languages lose more fillers), but mumbled ones fall into the gaps between words: `tighten.py` flags gaps with speech-level audio. Word edges are looser than Scribe's (a word onset measured 40 ms before Whisper's timestamp): pad cuts toward the top of the window.
- **`pack_transcripts.py --edit-dir <dir>`** — `transcripts/*.json` → `takes_packed.md` (phrase-level, break on silence ≥ 0.5s).
- **`tighten.py <video> -o <edit>/edl.json`** — EDL skeleton for one source: every pause ≥ `--gap` (0.45) cut, ranges padded `--pad-before`/`--pad-after` (0.10/0.14), `--remove START-END` for retakes and slips, `--topic T` for subject changes (split there, range marked `"topic": true`). Ranges under 0.8 s merge into their predecessor; ranges over 9 s split at sentence ends without removing time. Audits every cut pause for speech-level audio and lists the loud ones: those are words the ASR dropped.
- **`timeline_view.py <video> <start> <end>`** — filmstrip + waveform PNG. On-demand visual drill-down. **Not a scan tool** — use it at decision points, not constantly. `--edl <edl.json>` draws every range as first/middle/last source frame on one sheet (`verify/edl_ranges.png`): the view for deciding shot sizes before rendering.
- **Reframing**: give every range a `frame` target (`{"z", "ax", "ay"}`) and `render.py` zooms from it while extracting (logic in `reframe.py`, not a CLI): continuous zoom across cuts, eased settle, slow drift, push-in before a `topic` range, timed to the render's own rate. See Cut craft → Reframing.
- **`render.py <edl.json> -o <out>`** — per-segment extract → concat → overlays (PTS-shifted) → EDL `ass` titles → subtitles LAST. `--preview` is 1080p / medium / CRF 22 (QC-grade), `--draft` is 720p / ultrafast / CRF 28 (cut-point check only). `--build-subtitles` builds `<output>.srt` inline, next to the output. Intermediates are named after the output too, so several EDLs from one edit dir can render at the same time. Loudness normalization to -14 LUFS / -1 dBTP delivered is ON by default (limiter at -2, then the delivered true peak is measured and the AAC encode redone lower if it went over), `--no-loudnorm` turns it off. One canvas for the whole cut, `--size WxH` to set it (see Output spec). The source frame rate is preserved unless `--fps` overrides it. `"grade": "auto"` in the EDL grades every range from its own frames. Segments are cut to whole frames and cached by what shapes them, in a clips dir shared by every EDL in the edit dir: re-rendering after changing one range re-extracts one range, and a sub-EDL (parts of a long cut, a teaser) renders from the full cut's segments. The cache is never pruned automatically; `clips_*` can be deleted any time. Audio comes from EDL `audio_track` (default 0), the same track the transcript was made from.
- **`verify_render.py <out.mp4> <edl.json> [--retranscribe ru]`** — the self-eval in one pass: duration vs the quantized EDL, a seams sheet (frame before / after every cut), dark-border check after cuts, loudness and true peak, and with `--retranscribe` a local transcription of the render diffed against the words the EDL keeps, mismatches next to a cut flagged.
- **`parts.py <edl.json> --max 60`**: splits a long cut into part EDLs of at most `--max` seconds (`edl_part1.json`, … next to the EDL), each starting at a cut or a pause between words, never while a title or overlay is on screen; the EDL's `ass`, `subtitles` and overlays are retimed per part. Render each with `render.py`, in parallel if you like: they share the full cut's segments.
- **`grade.py <in> -o <out>`** — ffmpeg filter chain grade. Presets + `--filter '<raw>'` for custom.

To time anything to the output (titles, overlays), write a script into the edit dir and import the helpers from the skill, under the same project:

```python
# <edit>/titles.py, run as: uv run --frozen --project <skill dir> python <edit>/titles.py
import json, sys
from pathlib import Path
sys.path.insert(0, "<skill dir>/helpers")
import render

edit = Path("<edit>")
edl = json.loads((edit / "edl.json").read_text())
rate = render.resolve_output_rate(edl, edit, None)       # or the --fps you render with
t = render.source_to_output(edl, rate, "C0103", 40.10)   # EDL source name, seconds in that source
```

For animations, create `<edit>/animations/slot_<id>/` with the host's shell tool (`Bash` in Claude Code, `exec_command` in Codex). Use the host's delegation tool when available and permitted; otherwise complete the slot in the main session. Worker prompts must name their owned slot directory and tell workers not to revert others' edits or bypass denied tools and commands.

## The process

1. **Inventory.** `ffprobe` every source. `transcribe_batch.py` on the directory. `pack_transcripts.py` to produce `takes_packed.md`. Sample one or two `timeline_view`s for a visual first impression.
2. **Pre-scan for problems.** One pass over `takes_packed.md` to note verbal slips, obvious mis-speaks, or phrasings to avoid. Plain list, feed into the editor brief (`references/multi-take.md`).
3. **Converse.** Describe what you see in plain English. Ask questions *shaped by the material*. Collect: content type, target length/aspect, aesthetic/brand direction, pacing feel, must-preserve moments, must-cut moments, animation and grade preferences, subtitle needs. Do not use a fixed checklist — the right questions are different every time.
4. **Propose strategy.** 4–8 sentences: shape, take choices, cut direction, animation plan, grade direction, subtitle style, length estimate. **Wait for confirmation.**
5. **Execute.** Produce `edl.json` via the editor sub-agent brief (multi-take, `references/multi-take.md`), or `tighten.py` for one take that needs its pauses and slips out. For reframing: `timeline_view.py --edl` to see what each range holds, a `frame` target per range; `render.py` does the zoom. Drill into `timeline_view` at ambiguous moments. Build animations in parallel sub-agents. Apply grade per-segment. Compose via `render.py`.
6. **Preview.** `render.py --preview`.
7. **Self-eval (before showing the user).** Run `verify_render.py <preview> <edl.json>` (add `--retranscribe <lang>` when cuts are tight or the ASR was local) and look at the seams sheet it writes. Check for:
   - Visual discontinuity / flash / scale jump at a cut
   - Dark border after a cut (a zoom sampling outside the frame; dark content at the edge is a false alarm)
   - A word mismatch flagged NEAR CUT: a clipped or leftover word
   - Subtitle hidden behind an overlay (Rule 1 violation)
   - Overlay misaligned or showing wrong frames (Rule 4 violation)

   Open `timeline_view` on the rendered output (not the sources) only for cuts that look wrong on the sheet; one image per cut does not scale past a dozen cuts. An RMS jump at a cut is not a click: it is nearly always speech starting right after a tightened pause.

   Also sample: first 2s, last 2s, and 2–3 mid-points — check grade consistency, subtitle readability, overall coherence.

   Measure the audio, don't assume it: `verify_render.py` reports integrated loudness and true peak; add RMS per section (dialogue, music-only, end card) when there is music. An end card 15 dB under the dialogue, or effects louder than speech, is a bug. You cannot listen: say so, and report the numbers.

   For anything the user will publish (launch, promo, ad), also use one **critic sub-agent** when delegation is available and permitted, with the rendered file, the EDL, and any reference videos the user gave. Brief it to roast, not to praise: a verdict, ranked problems with timecodes and evidence (frames, levels), and the 5 fixes to do first. Otherwise do that critic pass in the main session and disclose the lack of an independent review. Fresh eyes catch what the author stopped seeing - cut-off payoff lines, 0.5s memes, unreadable 28px text at phone size.

   If anything fails: fix → re-render → re-eval. **Cap at 3 self-eval passes** — if issues remain after 3, flag them to the user rather than looping forever. Only present the preview once the self-eval passes.
8. **Iterate + persist.** Natural-language feedback, re-plan, re-render. Never re-transcribe. Final render on confirmation. Append to `project.md`.

## Cut craft (techniques)

- **Audio-first.** Candidate cuts from word boundaries and silence gaps.
- **Preserve peaks.** Laughs, punchlines, emphasis beats. Extend past punchlines to include reactions — the laugh IS the beat.
- **Speaker handoffs** benefit from air between utterances. Common values: 400–600ms. Less for fast-paced, more for cinematic. Taste call.
- **Audio events as signals.** `(laughs)`, `(sighs)`, `(applause)` mark beats. Extend past them.
- **Silence gaps are cut candidates.** Silences ≥400ms are usually the cleanest. 150–400ms phrase boundaries are usable with a visual check. <150ms is unsafe (mid-phrase).
- **Example cut padding** (the launch video shipped with this): 50ms before the first kept word, 80ms after the last. Tighter for montage energy, looser for documentary. Stay in the 30–200ms working window (Hard Rule 7).
- **Never reason audio and video independently.** Every cut must work on both tracks.
- **A loud pause is not a pause.** Local Whisper drops fillers and mumbled words, and they land inside the "silence" between two transcribed words, not in the text. A gap with speech-level audio holds words: `tighten.py` lists them; re-transcribe the snippet with a second or so of context before deciding the cut.

### Reframing (punch-ins and slow zoom on one static camera)

Worked from a 6-minute vertical haul video, one iPhone on a stand, 62 ranges after tightening; the viewer's feedback shaped the rules.

- **No scale jumps at a cut.** The first version switched shot size instantly at every cut (1.00 / 1.12 / 1.25); the viewer called it harsh. What shipped keeps scale and anchor continuous: each range starts at the zoom its predecessor ended on and eases (in-out cubic) to its own size over ~1 s, capped at 70% of the range. `render.py` does this from the `frame` targets (`reframe.py`).
- **Time the motion to the last frame** (`duration − 1/fps`), not to `duration`: the last frame sits one frame early, and a push-in misses its end value by ~2% and jumps at the cut.
- **Topic changes get a push.** The outgoing range pushes in ~10% over its last 0.4 s, the next range carries on from there and eases to its size. Inside a topic, only the eased change of shot size.
- **Alternate sizes, keep a slow drift.** Neighbouring ranges never share a size; within a range, an unzoomed shot pushes in and a zoomed one pulls out, 0.8% per second up to 4%.
- **Look before choosing sizes.** No close-up on a range where the subject holds something at chest height, it gets cropped; `timeline_view.py --edl` shows which ranges those are. No shot change on a range under ~0.8 s; `tighten.py` merges those into their neighbour.
- **1.25× is the ceiling for a close-up from a 1080p source**; beyond it the upscale gets soft. Wider sources allow more.
- **`perspective` with `eval=frame` and cubic interpolation, not `zoompan`.** zoompan rounds the crop to whole pixels and shimmers on slow moves; perspective is subpixel and runs at ~2.4× real time on 1080×1920.

## The packed transcript (primary reading view)

`pack_transcripts.py` reads all `transcripts/*.json` and produces one markdown file where each take is a list of phrase-level lines, each prefixed with its `[start-end]` time range. Phrases break on any silence ≥ 0.5s OR speaker change. This is the artifact the editor sub-agent reads to pick cuts — it gives word-boundary precision from text alone at 1/10 the tokens of raw JSON.

Example line:
```
## C0103  (duration: 43.0s, 8 phrases)
  [002.52-005.36] S0 Ninety percent of what a web agent does is completely wasted.
  [006.08-006.74] S0 We fixed this.
```

## References

Read the one the task needs before doing that part of it:

- `<skill dir>/references/multi-take.md`: the editor sub-agent brief, for picking the best take of each beat across many clips.
- `<skill dir>/references/color-grade.md`: grading, when the user asks for a look or a correction.
- `<skill dir>/references/subtitles.md`: caption styles, when subtitles are wanted.
- `<skill dir>/references/titles.md`: names, places, prices and numbers on screen as designed ASS titles.
- `<skill dir>/references/animations.md`: overlay animations (HyperFrames, Remotion, Manim, PIL), their timing, easing, palette and the sub-agent brief.
- `<skill dir>/references/sound.md`: music and sound effects.

## Output spec

Match the source unless the user asked for something specific. Common targets: `1920×1080@24` cinematic, `1920×1080@30` screen content, `1080×1920@30` vertical social, `3840×2160@24` 4K cinema, `1080×1080@30` square. `render.py` renders every range onto one canvas: by default the first source's shape at 1080 on the short side (720 with `--draft`), `--size 3840x2160` or `--size 1080x1080` for another target, `--fps` for another rate. A source of another shape is fitted inside with bars; give its ranges a `frame` zoom if bars are not wanted. Worth asking the user which delivery format matters.

## EDL format

```json
{
  "version": 1,
  "sources": {"C0103": "/abs/path/C0103.MP4", "C0108": "/abs/path/C0108.MP4"},
  "ranges": [
    {"source": "C0103", "start": 2.42, "end": 6.85,
     "beat": "HOOK", "quote": "...", "reason": "Cleanest delivery, stops before slip at 38.46."},
    {"source": "C0108", "start": 14.30, "end": 28.90,
     "beat": "SOLUTION", "quote": "...", "reason": "Only take without the false start."}
  ],
  "grade": "warm_cinematic",
  "overlays": [
    {"file": "animations/slot_1/render.mp4", "start_in_output": 0.0, "duration": 5.0}
  ],
  "subtitles": "master.srt",
  "total_duration_s": 87.4
}
```

Relative paths (`overlays`, `ass`, `subtitles`) resolve against the EDL's own directory, then the current one. `grade` is a preset name or raw ffmpeg filter. A range's `frame` target (`{"z", "ax", "ay"}`, optional `drift`) and `topic` flag drive the zoom, computed at render time (tune with an optional top-level `"reframe": {"settle": 1.0, "push": 0.4, "push_amp": 0.10}`). A range may also carry `vf`, a raw ffmpeg video filter for that range only, applied after the canvas fit, grade and zoom. `audio_track` (default 0) picks the source audio stream. `ass` is a designed titles file burned after overlays, before subtitles. `overlays` are rendered animation clips. `subtitles` is optional and applied LAST.

## Memory — `project.md`

Append one section per session at `<edit>/project.md`:

```markdown
## Session N - YYYY-MM-DD

**Strategy:** one paragraph describing the approach
**Decisions:** take choices, cuts, grades, animations + why
**Reasoning log:** one-line rationale for non-obvious decisions
**Outstanding:** deferred items
```

On startup, read `project.md` if it exists and summarize the last session in one sentence before asking whether to continue.

## Anti-patterns

Things that consistently fail regardless of style:

- **Hierarchical pre-computed codec formats** with USABILITY / tone tags / shot layers. Over-engineering. Derive from the transcript at decision time.
- **Hand-tuned moment-scoring functions.** The LLM picks better than any heuristic you'll write.
- **Whisper SRT / phrase-level output.** Loses sub-second gap data. Always word-level verbatim.
- **Running Whisper locally on CPU.** Slow and it normalizes fillers. Use hosted Scribe, or `transcribe_local.py` (GPU, filler prompt) when Scribe is unreachable.
- **Burning subtitles into base before compositing overlays.** Overlays hide them. (Hard Rule 1.)
- **Single-pass filtergraph when you have overlays.** Double re-encodes. Use per-segment extract → concat.
- **Linear animation easing.** Looks robotic. Always cubic.
- **Unverified web fonts.** A failed load silently falls back to a system face. Assert the font loaded before rendering.
- **Stock SFX on every transition.** Tie each effect to a visible event; cap the count.
- **Hard audio cuts at segment boundaries.** Audible pops. (Hard Rule 3.)
- **Typing text centered on the partial string.** Text slides left as it grows.
- **Sequential sub-agents for multiple animations when parallel delegation is available and permitted.** Dispatch independent slots together.
- **Editing before confirming the strategy.** Never.
- **Re-transcribing cached sources.** Immutable outputs of immutable inputs.
- **Assuming what kind of video it is.** Look first, ask second, edit last.

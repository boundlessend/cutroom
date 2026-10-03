# cutroom

Conversational video editing for Claude Code and Codex. Point it at your footage and say what you want: cut the pauses and slips, punch in and out so a static shot does not sit still, put names and prices on screen, normalize the loudness. It transcribes, plans the cut with you, renders, checks its own output and iterates.

Based on [browser-use/video-use](https://github.com/browser-use/video-use) by Browser Use.

## What it does

- **Transcription.** Free and local with `mlx-whisper` on Apple Silicon, or ElevenLabs Scribe with your own key. Word-level timestamps drive every cut.
- **Tightening.** Every pause over a threshold is cut, retakes and slips are removed on request, and pauses that hold speech-level audio are flagged as words the transcriber missed.
- **Reframing.** Smooth punch-ins on a single static camera: shot size changes ease across cuts instead of jumping, with a slow drift inside each shot and a push-in at topic changes.
- **Titles.** Names and numbers on screen from a designed ASS file, timed to the rendered cut.
- **Rendering.** Per-segment extraction cut to whole frames, cached between renders, loudness normalized to -14 LUFS with true peak held at -1 dBTP after the AAC encode.
- **Self-check.** One pass over the render: timeline against the plan, a sheet of every cut, loudness and true peak, and optionally a re-transcription diffed against the words the plan keeps.
- From video-use: color grading, overlay animations (HyperFrames, Remotion, Manim, PIL), subtitles, multi-take selection.

## What changed from video-use

- Local transcription helper (`transcribe_local.py`) as an alternative to ElevenLabs.
- New helpers: `tighten.py`, `verify_render.py`, `parts.py` (split into parts of at most a minute), `timeline_view.py --edl`, and reframing from per-range `frame` targets.
- `render.py`: whole-frame segments so subtitles and titles no longer drift from the speech, segment audio kept PCM so the sound stays in sync across cuts, one canvas for sources of mixed shapes, explicit audio stream mapping, a segment cache shared by every EDL in an edit folder, an `ass` titles field, and the true peak measured after the AAC encode.
- Helpers run in a `uv` project environment with only the dependencies they import.
- The vendored `manim-video` skill and the Browser Use branding are not included.

## Requirements

- Claude Code with plugin support, or another agent that reads Agent Skills (see Install).
- [uv](https://docs.astral.sh/uv/) on `PATH`: it builds the helpers' Python environment on first use.
- `ffmpeg` and `ffprobe` built with libass and libzimg (the `subtitles`, `ass` and `zscale` filters). Homebrew's default `ffmpeg` lacks both; `ffmpeg-full` has them.
- A transcription path:
  - free: macOS on Apple Silicon and `uv tool install mlx-whisper`, any language Whisper knows (fillers are kept best in Russian and English);
  - paid: `ELEVENLABS_API_KEY` in the environment.
- Node.js 22+ only for HyperFrames or Remotion animations.

## Install

### Claude Code

```
claude plugin marketplace add boundlessend/cutroom
claude plugin install cutroom@cutroom
```

### Codex

```bash
codex plugin marketplace add boundlessend/cutroom
codex plugin add cutroom@cutroom
```

In the Codex desktop app, open the plugin directory, select **Cutroom**, and install **cutroom**. Start a new chat after installing.

Cutroom is also available from the shared **Senya Plugins** marketplace:

```bash
codex plugin marketplace add boundlessend/yougile-tracking
codex plugin add cutroom@senya-plugins
```

Choose one marketplace for Cutroom to avoid duplicate skills.

For a standalone skill, ask Codex:

```text
$skill-installer install https://github.com/boundlessend/cutroom/tree/main/skills/cutroom
```

### Other agents

The skill follows the [Agent Skills](https://agentskills.io) format. Choose an installer supported by your agent:

```
npx skills add boundlessend/cutroom
gh skill install boundlessend/cutroom cutroom
gemini extensions install https://github.com/boundlessend/cutroom
```

Animation workers and the critic pass use the host's delegation tools: `Agent` in Claude Code, collaboration or `spawn_agent` in Codex. If delegation is unavailable or disallowed, the main session does the work sequentially and reports that limitation.

## Updating

Claude Code:

```bash
claude plugin marketplace update cutroom
claude plugin update cutroom@cutroom
```

Codex:

```bash
codex plugin marketplace upgrade cutroom
codex plugin add cutroom@cutroom
```

If you installed from `senya-plugins`, use that marketplace name instead. Restart the host after updating.

## Use

Ask in plain words, with the path to your footage:

- "Cut the pauses and slips out of ~/Videos/IMG_1234.MOV, keep the aspect ratio."
- "Add smooth punch-ins so the shot is not static."
- "Put the product names and prices on screen above my head."
- "Split the result into parts of at most a minute."

All outputs go to an `edit/` folder next to the footage; the plugin directory is never written to.

## Tests

```
uv run --project skills/cutroom python -m unittest discover -s skills/cutroom/tests
```

The smoke test renders a synthetic clip with real ffmpeg and checks lip sync, duration and true peak.

## License

cutroom's own work is licensed under the BSD 3-Clause License. The portions derived from browser-use/video-use remain under the MIT License, © Browser Use. Both texts are in [LICENSE](LICENSE).

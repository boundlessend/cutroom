"""End to end: render.py and verify_render.py on a synthetic source, real ffmpeg.

The source flashes a white frame and beeps at the same moments, so every range
checks lip sync; the EDL also has a seamless join and a zoom target just above 1.
"""

import json
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path

import numpy as np

HELPERS = Path(__file__).parents[1] / "helpers"
sys.path.insert(0, str(HELPERS))
import render  # noqa: E402

FPS = 30
MOMENTS = [k + 0.5 for k in range(7)]


def sh(*cmd: str) -> str:
    return subprocess.run(cmd, check=True, capture_output=True, text=True).stdout


def build_source(path: Path) -> None:
    flash = "+".join(f"between(t,{m},{m}+0.0333)" for m in MOMENTS)
    beep = "+".join(f"between(t,{m},{m}+0.02)" for m in MOMENTS)
    sh("ffmpeg", "-y", "-f", "lavfi", "-i", f"color=black:s=720x1280:r={FPS}:d=8",
       "-f", "lavfi", "-i", f"aevalsrc='0.5*sin(2*PI*1000*t)*({beep})':s=48000:d=8",
       "-vf", f"drawbox=c=white:t=fill:enable='{flash}'",
       "-c:v", "libx264", "-preset", "ultrafast", "-pix_fmt", "yuv420p", "-c:a", "aac", str(path))


def flash_times(video: Path) -> list[float]:
    out = sh("ffprobe", "-v", "error", "-f", "lavfi", "-i", f"movie={video},signalstats",
             "-show_entries", "frame=pts_time:frame_tags=lavfi.signalstats.YAVG", "-of", "csv=p=0")
    return [float(t) for t, y in (line.split(",")[:2] for line in out.splitlines()) if float(y) > 120]


def beep_times(video: Path, wav: Path) -> list[float]:
    sh("ffmpeg", "-y", "-i", str(video), "-map", "0:a:0", "-ac", "1", "-ar", "48000", str(wav))
    with wave.open(str(wav), "rb") as w:
        pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16).astype(np.float32)
    env = np.sqrt(np.mean(pcm[: pcm.size // 48 * 48].reshape(-1, 48) ** 2, axis=1))  # 1 ms windows
    on = env > 0.25 * env.max()
    return [i / 1000 for i in range(1, on.size) if on[i] and not on[i - 1]]


class RenderSmokeTest(unittest.TestCase):
    def test_draft_render_keeps_sync_duration_and_true_peak(self):
        with tempfile.TemporaryDirectory() as d:
            edit = Path(d)
            source = edit / "flash.mp4"
            build_source(source)
            ranges = [{"source": "flash", "start": m - 0.3, "end": m + 0.45, "frame": {"z": z}}
                      for m, z in zip(MOMENTS[:6], (1.0, 1.12, 1.0, 1.01, 1.0, 1.12))]
            # 6.2 + 12 frames = 6.6: a seamless join, rendered with no fade
            ranges += [{"source": "flash", "start": 6.2, "end": 6.6, "frame": {"z": 1.0}},
                       {"source": "flash", "start": 6.6, "end": 6.95, "frame": {"z": 1.12}}]
            edl = {"sources": {"flash": str(source)}, "ranges": ranges}
            edl_path = edit / "edl.json"
            edl_path.write_text(json.dumps(edl))
            out = edit / "out.mp4"
            sh(sys.executable, str(HELPERS / "render.py"), str(edl_path), "-o", str(out), "--draft")

            flashes, beeps = flash_times(out), beep_times(out, edit / "a.wav")
            self.assertEqual(len(flashes), len(MOMENTS))
            self.assertEqual(len(beeps), len(MOMENTS))
            for f, b in zip(flashes, beeps):
                self.assertLess(abs(b - f), 1 / FPS, f"sound {1000 * (b - f):+.1f} ms off the picture")

            rate = f"{FPS}/1"
            expected = sum(render.segment_duration(r["start"], r["end"], rate) for r in ranges)
            video_s = float(sh("ffprobe", "-v", "error", "-select_streams", "v:0", "-show_entries",
                               "stream=duration", "-of", "default=noprint_wrappers=1:nokey=1", str(out)))
            self.assertAlmostEqual(video_s, expected, delta=1 / FPS)
            self.assertLessEqual(render.loudness(out)[1], render.LOUDNORM_TP)

            report = sh(sys.executable, str(HELPERS / "verify_render.py"), str(out), str(edl_path))
            self.assertNotIn("CHECK", report)


if __name__ == "__main__":
    unittest.main()

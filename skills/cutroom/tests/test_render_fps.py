import argparse
import contextlib
import io
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).parents[1] / "helpers"))
import render  # noqa: E402


class ParseFpsTests(unittest.TestCase):
    def test_accepts_integer_decimal_and_rational_rates(self):
        expected = {
            "60": "60/1",
            "29.97": "2997/100",
            "30000/1001": "30000/1001",
        }
        for value, canonical in expected.items():
            with self.subTest(value=value):
                self.assertEqual(render.parse_fps(value), canonical)

    def test_canonical_rates_are_idempotent(self):
        for value in ("60", "29.97", "30000/1001"):
            with self.subTest(value=value):
                canonical = render.parse_fps(value)
                self.assertEqual(render.parse_fps(canonical), canonical)

    def test_rejects_invalid_or_non_positive_rates(self):
        for value in (
            "",
            "nope",
            "0",
            "-24",
            "1/0",
            "1e3",
            "1_000",
            "0.12345678901234567890",
            "1" * 33,
        ):
            with self.subTest(value=value):
                with self.assertRaises(argparse.ArgumentTypeError):
                    render.parse_fps(value)


class ProbeSourceFpsTests(unittest.TestCase):
    @staticmethod
    def _probe_result(avg: str, nominal: str) -> subprocess.CompletedProcess:
        stdout = json.dumps({
            "streams": [{"avg_frame_rate": avg, "r_frame_rate": nominal}]
        })
        return subprocess.CompletedProcess([], 0, stdout=stdout, stderr="")

    def test_prefers_average_rate(self):
        result = self._probe_result("30000/1001", "30/1")
        with patch.object(render.subprocess, "run", return_value=result):
            self.assertEqual(render.probe_source_fps(Path("source.mp4")), "30000/1001")

    def test_falls_back_to_nominal_rate(self):
        result = self._probe_result("0/0", "60/1")
        with patch.object(render.subprocess, "run", return_value=result):
            self.assertEqual(render.probe_source_fps(Path("source.mp4")), "60/1")

    def test_returns_none_for_unusable_probe_output(self):
        result = subprocess.CompletedProcess([], 0, stdout='{"streams": []}', stderr="")
        with patch.object(render.subprocess, "run", return_value=result):
            self.assertIsNone(render.probe_source_fps(Path("source.mp4")))

    def test_returns_none_when_ffprobe_fails(self):
        error = subprocess.CalledProcessError(1, ["ffprobe"])
        with patch.object(render.subprocess, "run", side_effect=error):
            self.assertIsNone(render.probe_source_fps(Path("source.mp4")))


class RenderRateTests(unittest.TestCase):
    @staticmethod
    def _edl() -> dict:
        return {
            "sources": {"first": "first.mp4", "second": "second.mp4"},
            "ranges": [
                {"source": "first", "start": 0, "end": 1},
                {"source": "second", "start": 0, "end": 1},
            ],
        }

    def setUp(self):
        # the segment cache key reads each source's size and mtime
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.edit_dir = Path(tmp.name)
        for name in ("first.mp4", "second.mp4"):
            (self.edit_dir / name).write_bytes(b"")

    def test_multi_source_render_resolves_one_rate_from_first_source(self):
        with (
            patch.object(render, "probe_source_fps", return_value="60/1") as probe,
            patch.object(render, "extract_segment") as extract,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            render.extract_all_segments(self._edl(), self.edit_dir, preview=False, canvas=(1920, 1080))

        probe.assert_called_once_with((self.edit_dir / "first.mp4").resolve())
        self.assertEqual([call.kwargs["rate"] for call in extract.call_args_list], ["60/1", "60/1"])

    def test_explicit_rate_skips_probe_and_applies_to_every_segment(self):
        with (
            patch.object(render, "probe_source_fps") as probe,
            patch.object(render, "extract_segment") as extract,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            render.extract_all_segments(
                self._edl(), self.edit_dir, preview=False, fps="30", canvas=(1920, 1080)
            )

        probe.assert_not_called()
        self.assertEqual(
            [call.kwargs["rate"] for call in extract.call_args_list],
            ["30/1", "30/1"],
        )

    def test_failed_probe_falls_back_to_24_for_every_segment(self):
        with (
            patch.object(render, "probe_source_fps", return_value=None),
            patch.object(render, "extract_segment") as extract,
            contextlib.redirect_stdout(io.StringIO()),
        ):
            render.extract_all_segments(
                self._edl(), self.edit_dir, preview=False, canvas=(1920, 1080)
            )

        self.assertEqual(
            [call.kwargs["rate"] for call in extract.call_args_list],
            ["24", "24"],
        )


if __name__ == "__main__":
    unittest.main()

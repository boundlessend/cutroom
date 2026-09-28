import json
import subprocess
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).parents[1] / "helpers"))
import render  # noqa: E402


class DisplaySizeTests(unittest.TestCase):
    def _size(self, stream: dict) -> tuple[int, int]:
        result = subprocess.CompletedProcess(
            [], 0, stdout=json.dumps({"streams": [stream]}), stderr=""
        )
        with patch.object(render.subprocess, "run", return_value=result) as run:
            size = render.display_size(Path("source.mp4"))
        cmd = run.call_args.args[0]
        show_entries = cmd[cmd.index("-show_entries") + 1]
        self.assertIn("stream_side_data=rotation", show_entries)
        self.assertNotIn("stream_tags=rotate", show_entries)
        return size

    def test_native_portrait_dimensions(self):
        self.assertEqual(self._size({"width": 1080, "height": 1920}), (1080, 1920))

    def test_native_landscape_dimensions(self):
        self.assertEqual(self._size({"width": 1920, "height": 1080}), (1920, 1080))

    def test_side_data_rotation_turns_coded_landscape_into_portrait(self):
        stream = {
            "width": 1920,
            "height": 1080,
            "side_data_list": [{"rotation": -90}],
        }
        self.assertEqual(self._size(stream), (1080, 1920))

    def test_plain_rotation_tag_without_side_data_is_ignored(self):
        stream = {"width": 1920, "height": 1080, "tags": {"rotate": "270"}}
        self.assertEqual(self._size(stream), (1920, 1080))

    def test_rotation_can_turn_coded_portrait_into_landscape(self):
        stream = {
            "width": 1080,
            "height": 1920,
            "side_data_list": [{"rotation": 90}],
        }
        self.assertEqual(self._size(stream), (1920, 1080))

    def test_failed_probe_is_an_error(self):
        result = subprocess.CompletedProcess([], 1, stdout="", stderr="No such file")
        with patch.object(render.subprocess, "run", return_value=result):
            with self.assertRaises(RuntimeError):
                render.display_size(Path("source.mp4"))


class CanvasSizeTests(unittest.TestCase):
    def test_default_is_the_first_source_at_1080_on_the_short_side(self):
        self.assertEqual(render.canvas_size((1080, 1920), None, draft=False), (1080, 1920))
        self.assertEqual(render.canvas_size((3840, 2160), None, draft=False), (1920, 1080))
        self.assertEqual(render.canvas_size((1080, 1080), None, draft=False), (1080, 1080))

    def test_draft_is_720_on_the_short_side(self):
        self.assertEqual(render.canvas_size((1080, 1920), None, draft=True), (720, 1280))
        self.assertEqual(render.canvas_size((3840, 2160), (3840, 2160), draft=True), (1280, 720))

    def test_explicit_size_wins(self):
        self.assertEqual(render.canvas_size((1080, 1920), (1080, 1080), draft=False), (1080, 1080))
        self.assertEqual(render.canvas_size((1920, 1080), (3840, 2160), draft=False), (3840, 2160))


if __name__ == "__main__":
    unittest.main()

"""The retired Phase recolouring option must stay out of every user path."""

import inspect
import io
import unittest
from contextlib import redirect_stderr

from pyincucyte import ExportOptions
from pyincucyte import engine
from pyincucyte.cli import parse_args
from pyincucyte.gui.app import App


class RemovedGreenLutTests(unittest.TestCase):
    def test_export_recipes_ignore_old_keys_and_cannot_enable_the_option(self):
        options = ExportOptions.from_dict({
            "output": "out", "vessels": [1],
            "green_lut": True, "green_phase": True,
        })

        self.assertNotIn("green_lut", options.to_dict())
        self.assertNotIn("green_phase", options.to_dict())
        with self.assertRaises(TypeError):
            ExportOptions(green_lut=True)

    def test_command_line_flags_are_no_longer_accepted(self):
        for flag in ("--green-lut", "--no-green-lut"):
            with self.subTest(flag=flag), redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit):
                    parse_args(["download", "-v", "1", "-o", "out", flag])

    def test_gui_and_engine_no_longer_expose_phase_recolouring(self):
        interface_source = inspect.getsource(App)
        self.assertNotIn("green_lut", interface_source)
        self.assertNotIn("Green LUT", interface_source)
        self.assertFalse(hasattr(engine, "apply_green_lut"))
        for operation in (
            engine.download_scan_images,
            engine.download_collected_scan_items,
        ):
            self.assertNotIn(
                "green_phase", inspect.signature(operation).parameters)

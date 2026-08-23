"""One package, one name.

Everything ships under ``pyincucyte``: the library, the command line, and the
desktop app as ``pyincucyte gui``.  These tests pin the things that a rename
quietly breaks - retired import names, the console entry points, and the folder
holding somebody's saved login.
"""

import importlib
import importlib.util
import os
import sys
import tomllib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import pyincucyte
from pyincucyte import cli, compat, engine

ROOT = Path(__file__).resolve().parent.parent


class LayoutTests(unittest.TestCase):
    def test_the_distribution_and_the_package_agree_on_the_version(self):
        with open(ROOT / "pyproject.toml", "rb") as handle:
            project = tomllib.load(handle)["project"]
        self.assertEqual(project["version"], pyincucyte.__version__)
        self.assertEqual(project["name"].lower(), "pyincucyte")

    def test_nothing_ships_outside_the_package(self):
        stray = {"py_incucyte_gui", "incucyte_downloader.py", "incucyte_gui.py"}
        self.assertEqual(stray & {p.name for p in ROOT.iterdir()}, set())

    def test_both_console_scripts_point_into_the_package(self):
        with open(ROOT / "pyproject.toml", "rb") as handle:
            scripts = tomllib.load(handle)["project"]["scripts"]
        self.assertEqual(scripts["pyincucyte"], "pyincucyte.cli:main")
        self.assertEqual(scripts["pyincucyte-gui"], "pyincucyte.gui:main")


class GuiFromTheCommandLineTests(unittest.TestCase):
    def test_gui_is_a_subcommand(self):
        args = cli.parse_args(["gui"])
        self.assertEqual(args.command, "gui")
        self.assertIs(cli.COMMANDS["gui"], cli.cmd_gui)

    def test_the_command_line_does_not_drag_in_tk(self):
        """Planning a download must not require a display."""
        source = (ROOT / "pyincucyte" / "cli.py").read_text(encoding="utf-8")
        for line in source.splitlines():
            if line.startswith(("import ", "from ")):
                self.assertNotIn("tkinter", line)
                self.assertNotIn(".gui", line)

    def test_gui_launches_the_app(self):
        with mock.patch("pyincucyte.gui.app.main", return_value=0) as launched:
            self.assertEqual(cli.cmd_gui(cli.parse_args(["gui"])), 0)
        launched.assert_called_once_with()


class RetiredImportNameTests(unittest.TestCase):
    def test_the_old_name_is_the_same_module_object(self):
        downloader = importlib.import_module("incucyte_downloader")
        self.assertIs(downloader, engine)

    def test_the_old_name_still_exposes_the_command_line(self):
        downloader = importlib.import_module("incucyte_downloader")
        self.assertIs(downloader.main, cli.main)

    def test_the_old_distribution_package_resolves_too(self):
        self.assertIs(importlib.import_module("py_incucyte_gui"), pyincucyte)

    def test_the_gui_alias_resolves_without_importing_tk(self):
        # find_spec locates it; nothing is executed, so Tk stays unloaded.
        self.assertIsNotNone(importlib.util.find_spec("incucyte_gui"))
        self.assertEqual(compat.ALIASES["incucyte_gui"], "pyincucyte.gui")

    def test_installing_twice_adds_one_finder(self):
        compat.install()
        compat.install()
        finders = [f for f in sys.meta_path if isinstance(f, compat._AliasFinder)]
        self.assertEqual(len(finders), 1)

    def test_an_unrelated_import_is_left_alone(self):
        self.assertIsNone(compat._AliasFinder().find_spec("json"))


class SettingsFolderTests(unittest.TestCase):
    """Renaming the project must not lose anybody's saved login."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    #: What the settings folder was called before 0.3, on this platform.
    OLD_NAME = "PyIncucyteGUI" if os.name == "nt" else "pyincucytegui"
    NEW_NAME = "PyIncucyte" if os.name == "nt" else "pyincucyte"

    def app_dir(self, **environment):
        """Resolve the settings folder in an environment of our own making."""
        base = str(self.tmp / "config")
        environment.setdefault("APPDATA", base)
        environment.setdefault("XDG_CONFIG_HOME", base)
        # Path.home() is evaluated as the fallback whether or not it is used.
        environment.setdefault("USERPROFILE", str(self.tmp))
        environment.setdefault("HOME", str(self.tmp))
        # clear=True also drops the PYINCUCYTE_HOME that conftest sets.
        with mock.patch.dict("os.environ", environment, clear=True), \
                mock.patch.object(engine, "LEGACY_APP_DIR", self.tmp / "absent"):
            return engine.default_app_dir()

    def test_the_new_variable_wins(self):
        self.assertEqual(self.app_dir(PYINCUCYTE_HOME=str(self.tmp / "new")),
                         self.tmp / "new")

    def test_the_old_variable_still_works(self):
        self.assertEqual(self.app_dir(PYINCUCYTEGUI_HOME=str(self.tmp / "old")),
                         self.tmp / "old")

    def test_a_fresh_machine_gets_the_new_folder(self):
        self.assertEqual(self.app_dir().name, self.NEW_NAME)

    def test_an_existing_settings_folder_keeps_being_used(self):
        old = self.tmp / "config" / self.OLD_NAME
        old.mkdir(parents=True)
        (old / "incucyte_config.json").write_text("{}", encoding="utf-8")
        self.assertEqual(self.app_dir(), old)

    def test_an_empty_old_folder_does_not_count(self):
        (self.tmp / "config" / self.OLD_NAME).mkdir(parents=True)
        self.assertEqual(self.app_dir().name, self.NEW_NAME)


if __name__ == "__main__":
    unittest.main()

"""One package, one name.

Everything ships under ``pyincucyte``: the library, the command line, and the
desktop app as ``pyincucyte gui``.  These tests pin the things that a rename
quietly breaks - retired import names, the console entry points, and the folder
holding somebody's saved login.
"""

import importlib
import importlib.util
import os
import re
import sys
import tomllib
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import mock

import pyincucyte
from pyincucyte import IncucyteClient, cli, compat, engine
from pyincucyte.config import ConfigStore, Credentials
from pyincucyte.errors import HostNotSetError

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


class NothingPrivateShipsTests(unittest.TestCase):
    """Whatever is in a release is on PyPI and GitHub, and stays there.

    An instrument's address is site-specific. One written into the source is
    published with every wheel, rendered on the project page if it reaches the
    README, and preserved in every clone of the history. It has happened once;
    this is the test that stops it happening again.
    """

    #: Four dot-separated numbers - an address, wherever it turns up.
    ADDRESS = re.compile(r"\b\d{1,3}\.\d{1,3}\.\d{1,3}\.\d{1,3}\b")

    def shipped(self):
        """Every file that goes into the distribution."""
        return sorted((ROOT / "pyincucyte").rglob("*.py")) + [ROOT / "README.md",
                                                              ROOT / "pyproject.toml"]

    def test_no_address_is_written_into_anything_that_ships(self):
        found = []
        for path in self.shipped():
            if not path.exists():
                continue
            for number, line in enumerate(
                    path.read_text(encoding="utf-8").splitlines(), 1):
                if self.ADDRESS.search(line):
                    found.append(f"{path.relative_to(ROOT)}:{number}: {line.strip()}")
        self.assertEqual(found, [], "an address here is published for good")

    def test_there_is_no_address_to_fall_back_on(self):
        with mock.patch.dict("os.environ", {"PYINCUCYTE_HOST": ""}):
            self.assertEqual(engine.default_host(), "")

    def test_the_environment_can_supply_one(self):
        with mock.patch.dict("os.environ", {"PYINCUCYTE_HOST": "10.0.0.7"}):
            self.assertEqual(engine.default_host(), "10.0.0.7")


class HostIsRequiredTests(unittest.TestCase):
    """Nothing reaches the network until somebody says where the device is."""

    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.store = ConfigStore(Path(self.tmp) / "credentials.json")

    def tearDown(self):
        self._tmp.cleanup()

    def client(self, host=None):
        with mock.patch.object(engine, "DEFAULT_HOST", ""):
            return IncucyteClient(host, store=self.store)

    def test_asking_without_an_address_says_how_to_set_one(self):
        client = self.client()
        with self.assertRaises(HostNotSetError) as caught:
            client.require_host()
        message = str(caught.exception)
        for how in ("--host", "PYINCUCYTE_HOST", "login"):
            self.assertIn(how, message)

    def test_a_call_refuses_before_it_reaches_the_network(self):
        client = self.client()
        with mock.patch.object(engine, "api_post",
                               side_effect=AssertionError("should not be sent")):
            with self.assertRaises(HostNotSetError):
                client.call("Vessels/GetAllSearchVessels")

    def test_a_probe_refuses_too(self):
        with self.assertRaises(HostNotSetError):
            self.client().probe()

    def test_an_address_passed_in_is_enough(self):
        self.assertEqual(self.client("10.0.0.7").require_host(), "10.0.0.7")

    def test_the_environment_reaches_the_client(self):
        with mock.patch.dict("os.environ", {"PYINCUCYTE_HOST": "10.0.0.9"}):
            self.assertEqual(self.client().require_host(), "10.0.0.9")

    def test_a_saved_login_carries_its_own_address(self):
        self.store.save(Credentials(host="10.0.0.8", username="tester",
                                    encrypted_password="hashed"))
        self.assertEqual(self.client().require_host(), "10.0.0.8")


if __name__ == "__main__":
    unittest.main()

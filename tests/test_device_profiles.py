"""Saved device profiles keep addresses, names, and credentials isolated."""

import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

from pyincucyte.client import IncucyteClient
from pyincucyte.config import CONFIG_VERSION, ConfigStore, Credentials
from pyincucyte.errors import NotLoggedInError


class DeviceProfileTests(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.path = Path(self._tmp.name) / "credentials.json"
        self.store = ConfigStore(self.path)

    def tearDown(self):
        self._tmp.cleanup()

    @staticmethod
    def credentials(host, name, token):
        return Credentials(
            host=host, device_name=name, username=f"user-{name}",
            encrypted_password=f"password-{name}", token=token,
            token_expires_at="2999-01-01T00:00:00")

    def test_saving_a_second_device_preserves_the_first_login(self):
        first = self.credentials("10.0.0.1", "Upstairs", "token-one")
        second = self.credentials("10.0.0.2", "Downstairs", "token-two")
        self.store.save(first)
        self.store.save(second)

        self.assertEqual(self.store.load("10.0.0.1").token, "token-one")
        self.assertEqual(self.store.load("10.0.0.2").token, "token-two")
        self.assertEqual(self.store.active_host(), "10.0.0.2")
        payload = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(payload["version"], CONFIG_VERSION)
        self.assertEqual(set(payload["devices"]), {"10.0.0.1", "10.0.0.2"})

    def test_legacy_single_login_migrates_when_another_device_is_added(self):
        first = self.credentials("10.0.0.1", "", "legacy-token")
        self.path.write_text(
            json.dumps(first.to_dict()), encoding="utf-8")

        self.assertEqual(self.store.load().token, "legacy-token")
        self.store.save(
            self.credentials("10.0.0.2", "New device", "new-token"))

        self.assertEqual(self.store.load("10.0.0.1").token, "legacy-token")
        self.assertEqual(self.store.load("10.0.0.2").device_name, "New device")

    def test_switching_or_forgetting_one_device_never_uses_anothers_token(self):
        self.store.save(
            self.credentials("10.0.0.1", "One", "token-one"))
        self.store.save(
            self.credentials("10.0.0.2", "Two", "token-two"))

        self.store.select("10.0.0.1")
        self.assertEqual(self.store.load().token, "token-one")
        unknown = IncucyteClient("10.0.0.3", store=self.store)
        self.assertEqual(unknown.credentials.token, "")
        with self.assertRaises(NotLoggedInError):
            self.store.require("10.0.0.3")

        self.store.clear("10.0.0.1")
        self.assertEqual(self.store.load().token, "token-two")
        self.assertEqual(self.store.load("10.0.0.1").token, "")

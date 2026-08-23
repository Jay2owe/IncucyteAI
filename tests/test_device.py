"""Reading the instrument's state, and the two calls that write back to it.

The writes are the only thing in PyIncucyte that changes the Incucyte, so most
of what is tested here is the *refusal*: an unconfirmed call must send nothing
at all, not send-then-apologise.
"""

import unittest
from datetime import datetime, timedelta
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch

from pyincucyte import IncucyteClient, device
from pyincucyte.cli import main
from pyincucyte.errors import ConfirmationRequiredError, DeviceBusyError
from pyincucyte.processing import Unmixing, unmix_pairs_from_scan

from fakes import FakeDevice, logged_in_store, patched


class DeviceTestCase(unittest.TestCase):
    def setUp(self):
        self._tmp = TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.store = logged_in_store(self.tmp)
        self.device = FakeDevice()
        self.client = IncucyteClient("10.0.0.1", store=self.store)

    def tearDown(self):
        self._tmp.cleanup()

    def routes(self):
        return [route for route, _ in self.device.calls]

    def wrote_anything(self):
        return bool(self.device.scans_requested or self.device.saved_unmixes)


# ---------------------------------------------------------------------------
# reading the status payload
# ---------------------------------------------------------------------------

class StatusPayloadTests(unittest.TestCase):
    """A .NET enum arrives as its index; a missing field must not raise."""

    def test_enum_indexes_become_names(self):
        state = device.DeviceState.from_payload(
            {"DeviceStatus": {"DeviceActivity": 1, "DrawerStatus": 2}})
        self.assertEqual(state.activity, "Scanning")
        self.assertEqual(state.drawer, "Closed")

    def test_enum_names_are_accepted_too(self):
        state = device.DeviceState.from_payload(
            {"DeviceStatus": {"DeviceActivity": "DiskFull"}})
        self.assertEqual(state.activity, "DiskFull")
        self.assertTrue(state.has_problem)

    def test_an_unknown_enum_value_does_not_raise(self):
        state = device.DeviceState.from_payload(
            {"DeviceStatus": {"DeviceActivity": 998}})
        self.assertEqual(state.activity, "Unknown")
        self.assertFalse(state.has_problem)

    def test_a_bare_status_block_works_without_the_wrapper(self):
        state = device.DeviceState.from_payload({"DeviceActivity": 0})
        self.assertTrue(state.is_idle)

    def test_an_empty_payload_gives_an_unknown_state_not_an_error(self):
        state = device.DeviceState.from_payload({})
        self.assertEqual(state.activity, "Unknown")
        self.assertEqual(state.describe()[0], "Activity:     Unknown")

    def test_the_raw_payload_is_kept_for_whatever_we_did_not_parse(self):
        payload = {"DeviceStatus": {"DeviceActivity": 0}, "SomethingNew": 42}
        state = device.DeviceState.from_payload(payload)
        self.assertEqual(state.raw["SomethingNew"], 42)

    def test_a_dotnet_timespan_becomes_a_timedelta(self):
        state = device.DeviceState.from_payload({"DeviceStatus": {
            "DeviceActivity": 1, "TimeToComplete": "01:12:00"}})
        self.assertEqual(state.time_to_complete, timedelta(hours=1, minutes=12))

    def test_a_timespan_with_days_becomes_a_timedelta(self):
        state = device.DeviceState.from_payload({"DeviceStatus": {
            "TimeToComplete": "1.02:03:04"}})
        self.assertEqual(state.time_to_complete,
                         timedelta(days=1, hours=2, minutes=3, seconds=4))

    def test_progress_is_quoted_while_working_and_not_while_idle(self):
        working = device.DeviceState.from_payload({"DeviceStatus": {
            "DeviceActivity": 1, "PercentageComplete": 42.0}})
        self.assertIn("42% done", working.summary())
        idle = device.DeviceState.from_payload({"DeviceStatus": {
            "DeviceActivity": 0, "PercentageComplete": 42.0}})
        self.assertNotIn("42%", idle.summary())

    def test_a_fault_shows_up_as_a_warning_line(self):
        state = device.DeviceState.from_payload(
            {"DeviceStatus": {"DeviceActivity": "DiskNearlyFull"}})
        self.assertTrue(any("WARNING" in line for line in state.describe()))
        self.assertIn("Disk nearly full", state.summary())

    def test_to_dict_is_json_shaped(self):
        state = device.DeviceState.from_payload({"DeviceStatus": {
            "DeviceActivity": 0, "LastScan": "2026-03-01T09:00:00"}})
        payload = state.to_dict()
        self.assertEqual(payload["activity"], "Idle")
        self.assertTrue(payload["healthy"])
        self.assertEqual(payload["last_scan"], "2026-03-01T09:00:00")


# ---------------------------------------------------------------------------
# reading, through the client
# ---------------------------------------------------------------------------

class ClientReadTests(DeviceTestCase):
    def test_device_state_reads_the_instrument(self):
        self.device.activity = "Scanning"
        with patched(self.device):
            state = self.client.device_state()
        self.assertTrue(state.is_scanning)
        self.assertEqual(state.gantry_c, 37.4)
        self.assertIn(device.ROUTE_STATUS, self.routes())

    def test_the_next_scan_is_reported_with_whoever_scheduled_it(self):
        self.device.next_scan = "2026-03-04T09:00:00"
        with patched(self.device):
            state = self.client.device_state()
        self.assertEqual(state.next_scan, datetime(2026, 3, 4, 9, 0))
        self.assertEqual(state.next_scan_user, "tester")

    def test_temperatures_come_back_as_a_list(self):
        with patched(self.device):
            readings = self.client.temperatures()
        self.assertEqual(len(readings), 1)
        self.assertEqual(readings[0]["GantryBoardDegreesCelcius"], 37.4)

    def test_the_scan_pattern_says_how_many_wells_and_sites(self):
        with patched(self.device):
            pattern = self.client.scan_pattern(38)
        self.assertEqual(pattern.well_count, 2)
        self.assertEqual(pattern.images_per_well, 2)

    def test_the_user_id_is_fetched_once_and_then_remembered(self):
        with patched(self.device):
            self.assertEqual(self.client.user_id(), 7)
            self.assertEqual(self.client.user_id(), 7)
        logins = [r for r in self.routes() if r == device.ROUTE_VALIDATE_LOGIN]
        self.assertEqual(len(logins), 1, "the user id should be cached")

    def test_reading_never_writes(self):
        with patched(self.device):
            self.client.device_state()
            self.client.temperatures()
            self.client.scan_pattern(38)
        self.assertFalse(self.wrote_anything())


# ---------------------------------------------------------------------------
# the guard
# ---------------------------------------------------------------------------

class ConfirmationTests(DeviceTestCase):
    def test_begin_scan_without_confirmation_sends_nothing(self):
        with patched(self.device):
            with self.assertRaises(ConfirmationRequiredError):
                self.client.begin_scan(38)
        self.assertEqual(self.device.scans_requested, [])
        self.assertNotIn(device.ROUTE_BEGIN_SCAN, self.routes())

    def test_the_refusal_says_how_to_confirm(self):
        with patched(self.device):
            with self.assertRaises(ConfirmationRequiredError) as caught:
                self.client.begin_scan(38)
        message = str(caught.exception)
        self.assertIn("confirm=True", message)
        self.assertIn("--yes", message)
        self.assertIn("vessel 38", message)

    def test_save_unmix_without_confirmation_sends_nothing(self):
        with patched(self.device):
            with self.assertRaises(ConfirmationRequiredError):
                self.client.save_unmix(38, "green:8%red")
        self.assertEqual(self.device.saved_unmixes, [])

    def test_a_faulty_instrument_refuses_the_write(self):
        self.device.activity = "DiskFull"
        with patched(self.device):
            with self.assertRaises(DeviceBusyError):
                self.client.begin_scan(38, confirm=True)
        self.assertEqual(self.device.scans_requested, [])

    def test_force_sends_it_anyway(self):
        self.device.activity = "DiskFull"
        with patched(self.device):
            self.client.begin_scan(38, confirm=True, force=True)
        self.assertEqual(len(self.device.scans_requested), 1)

    def test_a_busy_but_healthy_instrument_is_not_a_refusal(self):
        self.device.activity = "Scanning"
        with patched(self.device):
            self.client.begin_scan(38, confirm=True)
        self.assertEqual(len(self.device.scans_requested), 1)


# ---------------------------------------------------------------------------
# the writes themselves
# ---------------------------------------------------------------------------

class BeginScanTests(DeviceTestCase):
    def test_the_payload_carries_the_vessel_and_the_user_id(self):
        with patched(self.device):
            self.client.begin_scan(38, confirm=True)
        self.assertEqual(self.device.scans_requested,
                         [{"VesselID": 38, "AutomationUserID": 7}])

    def test_an_explicit_user_id_skips_the_lookup(self):
        with patched(self.device):
            self.client.begin_scan(38, confirm=True, user_id=99)
        self.assertEqual(self.device.scans_requested[0]["AutomationUserID"], 99)
        self.assertNotIn(device.ROUTE_VALIDATE_LOGIN, self.routes())

    def test_a_state_already_in_hand_is_not_fetched_twice(self):
        with patched(self.device):
            state = self.client.device_state()
            self.client.begin_scan(38, confirm=True, state=state)
        reads = [r for r in self.routes() if r == device.ROUTE_STATUS]
        self.assertEqual(len(reads), 2, "one explicit read, one for the result")

    def test_a_vessel_object_works_as_well_as_an_id(self):
        with patched(self.device):
            vessel = self.client.vessel(38)
            self.client.begin_scan(vessel, confirm=True)
        self.assertEqual(self.device.scans_requested[0]["VesselID"], 38)


class SaveUnmixTests(DeviceTestCase):
    def test_our_channel_numbers_become_the_devices(self):
        with patched(self.device):
            self.client.save_unmix(38, "green:8%red", confirm=True)
        pairs = self.device.saved_unmixes[0]["UnmixPairs"]
        self.assertEqual(pairs, [{"Recipient": 1, "Contributor": 2,
                                  "ValueRatio": 0.08, "BlurringSigma": None}])

    def test_what_we_write_is_what_we_read_back(self):
        """The offset has to survive a round trip, or a saved 8% reads as 8% of
        the wrong channel."""
        with patched(self.device):
            self.client.save_unmix(38, "green:8%red@2", confirm=True)
        written = self.device.saved_unmixes[0]["UnmixPairs"]
        recovered = Unmixing(unmix_pairs_from_scan({"ColorUnmixes": written}))
        self.assertEqual(recovered.to_spec(), "green:8%red@2")

    def test_an_unmixing_object_is_accepted(self):
        with patched(self.device):
            self.client.save_unmix(38, Unmixing.parse("red:5%green"),
                                   confirm=True)
        pairs = self.device.saved_unmixes[0]["UnmixPairs"]
        self.assertEqual(pairs[0]["Recipient"], 2)

    def test_saving_an_empty_unmixing_clears_it(self):
        with patched(self.device):
            self.client.save_unmix(38, "", confirm=True)
        self.assertEqual(self.device.saved_unmixes[0]["UnmixPairs"], [])


# ---------------------------------------------------------------------------
# the command line
# ---------------------------------------------------------------------------

class CommandLineTests(DeviceTestCase):
    def run_cli(self, *argv, json_out=False):
        """--json is a global flag, so it goes before the subcommand."""
        args = ["--host", "10.0.0.1", "--config-file", str(self.store.path)]
        if json_out:
            args.append("--json")
        with patched(self.device):
            return main(args + list(argv))

    def test_status_reports_and_exits_clean(self):
        self.assertEqual(self.run_cli("status"), 0)

    def test_status_exits_non_zero_when_the_instrument_is_unwell(self):
        self.device.activity = "RaidDegraded"
        self.assertEqual(self.run_cli("status"), 1)

    def test_status_json_is_machine_readable(self):
        import io
        from contextlib import redirect_stdout
        import json as json_mod

        buffer = io.StringIO()
        with redirect_stdout(buffer):
            self.run_cli("status", json_out=True)
        payload = json_mod.loads(buffer.getvalue())
        self.assertEqual(payload["activity"], "Idle")
        self.assertTrue(payload["healthy"])

    def test_scan_now_without_yes_writes_nothing(self):
        with patch("sys.stdin.isatty", return_value=False):
            code = self.run_cli("scan-now", "-v", "38")
        self.assertEqual(code, 2)
        self.assertEqual(self.device.scans_requested, [])

    def test_scan_now_with_yes_sends_it(self):
        self.assertEqual(self.run_cli("scan-now", "-v", "38", "--yes"), 0)
        self.assertEqual(len(self.device.scans_requested), 1)

    def test_unmix_shows_without_changing_anything(self):
        self.assertEqual(self.run_cli("unmix", "-v", "38"), 0)
        self.assertEqual(self.device.saved_unmixes, [])

    def test_unmix_set_needs_yes(self):
        with patch("sys.stdin.isatty", return_value=False):
            code = self.run_cli("unmix", "-v", "38", "--set", "green:8%red")
        self.assertEqual(code, 2)
        self.assertEqual(self.device.saved_unmixes, [])

    def test_unmix_set_with_yes_writes(self):
        code = self.run_cli("unmix", "-v", "38", "--set", "green:8%red", "--yes")
        self.assertEqual(code, 0)
        self.assertEqual(len(self.device.saved_unmixes), 1)


if __name__ == "__main__":
    unittest.main()

"""Active-timeout checks using fake roots; no daemon, share, or experiment.

Run with unittest or directly. Set TEMP/TMP to a project scratch directory to
keep fixtures accessible. Fixtures are retained for inspection, not deleted.
"""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

import datasync.data_paths as dp
import datasync.data_sync as ds
import datasync.data_sync_ui as ui


class FakeCampaign(unittest.TestCase):
    def setUp(self):
        self.root = Path(tempfile.mkdtemp(prefix="active_timeout_"))
        self.campaign = "timeout-test"
        roots = patch.multiple(dp, ROOT_C=str(self.root / "C"),
                               ROOT_S=str(self.root / "S"))
        roots.start()
        self.addCleanup(roots.stop)
        self.now = int(time.time())
        clock = patch.object(ds.time, "time", return_value=self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def marker(self, age):
        path = str(self.root / "C" / self.campaign / "data" / "run.h5")
        marker = ds.mark_active(path, self.campaign)
        self.assertIsNotNone(marker)
        os.utime(marker, (self.now - age, self.now - age))
        return path, Path(marker)

    def settings_path(self):
        return Path(ds._state("settings.json", self.campaign))


class ActiveTimeoutTests(FakeCampaign):
    def test_default_does_not_create_state(self):
        self.assertEqual(ds.get_active_stale_s(self.campaign), 900.0)
        self.assertFalse(self.settings_path().exists())

    def test_default_boundary_and_read_only_claims(self):
        path, marker = self.marker(900)
        self.assertEqual(ds.active_paths(self.campaign), [path])
        self.assertEqual(ds.active_claims(self.campaign)[0]["stale_s"], 900.0)
        os.utime(marker, (self.now - 901, self.now - 901))
        self.assertEqual(ds.active_claims(self.campaign), [])
        self.assertTrue(marker.exists())
        self.assertEqual(ds.active_paths(self.campaign), [])
        self.assertFalse(marker.exists())

    def test_longer_hold_protects_existing_marker(self):
        path, marker = self.marker(1200)
        ds.set_active_stale_s(1800, self.campaign)
        self.assertEqual(ds.active_paths(self.campaign), [path])
        claims = ds.active_claims(self.campaign)
        self.assertEqual(claims[0]["path"], path)
        self.assertEqual(claims[0]["stale_s"] - claims[0]["age"], 600)
        self.assertTrue(marker.exists())

    def test_shorter_hold_expires_existing_marker(self):
        path, marker = self.marker(700)
        ds.set_active_stale_s(600, self.campaign)
        self.assertEqual(ds.active_claims(self.campaign), [])
        self.assertTrue(marker.exists())
        self.assertEqual(ds.active_paths(self.campaign), [])
        self.assertFalse(marker.exists())

    def test_fractional_seconds_round_trip_and_campaign_isolation(self):
        self.assertEqual(ds.set_active_stale_s(90.5, self.campaign), 90.5)
        self.assertEqual(ds.get_active_stale_s(self.campaign), 90.5)
        self.assertEqual(json.loads(self.settings_path().read_text()),
                         {"active_stale_s": 90.5})
        self.assertEqual(ds.get_active_stale_s("other-campaign"), 900.0)

    def test_separate_process_edits_are_seen_without_reimport(self):
        ds.set_active_stale_s(1200, self.campaign)
        self.assertEqual(ds.get_active_stale_s(self.campaign), 1200)
        env = dict(os.environ, TPROCV2_DATA_ROOT_C=dp.ROOT_C,
                   TPROCV2_DATA_ROOT_S=dp.ROOT_S,
                   PYTHONPATH=str(Path(ds.__file__).resolve().parents[1]))
        script = ("import sys; import datasync.data_sync as ds; "
                  "ds.set_active_stale_s(2400, sys.argv[1]); "
                  "assert ds.get_active_stale_s(sys.argv[1]) == 2400")
        subprocess.run([sys.executable, "-B", "-c", script, self.campaign],
                       env=env, check=True, capture_output=True, text=True,
                       timeout=15)
        self.assertEqual(ds.get_active_stale_s(self.campaign), 2400)

    def test_explicit_override_bypasses_saved_setting(self):
        path, marker = self.marker(1200)
        ds.set_active_stale_s(600, self.campaign)
        self.assertEqual(ds.active_paths(self.campaign, stale_s=1500), [path])
        self.assertEqual(ds.active_claims(self.campaign, stale_s=1500)[0]
                         ["stale_s"], 1500)
        self.assertTrue(marker.exists())

    def test_invalid_input_preserves_previous_setting(self):
        ds.set_active_stale_s(1200, self.campaign)
        before = self.settings_path().read_bytes()
        for value in (0, -1, float("nan"), float("inf"), -float("inf"),
                      "", "oops", None, True, False, [], {}):
            with self.subTest(value=value):
                with self.assertRaises(ValueError):
                    ds.set_active_stale_s(value, self.campaign)
                self.assertEqual(self.settings_path().read_bytes(), before)

    def test_missing_field_uses_default(self):
        ds.set_active_stale_s(1200, self.campaign)
        self.settings_path().write_text("{}")
        self.assertEqual(ds.get_active_stale_s(self.campaign), 900.0)

    def test_bad_settings_warn_and_use_default(self):
        ds.set_active_stale_s(1200, self.campaign)
        for text in ("{", "[]", "null", '{"active_stale_s": -1}',
                     '{"active_stale_s": true}', '{"active_stale_s": "NaN"}'):
            with self.subTest(text=text):
                self.settings_path().write_text(text)
                with self.assertLogs(ds.__name__, level="WARNING"):
                    self.assertEqual(ds.get_active_stale_s(self.campaign), 900.0)

    def test_unreadable_settings_warn_and_use_default(self):
        with patch("builtins.open", side_effect=PermissionError("test")):
            with self.assertLogs(ds.__name__, level="WARNING"):
                self.assertEqual(ds.get_active_stale_s(self.campaign), 900.0)

    def test_failed_replace_preserves_previous_setting(self):
        ds.set_active_stale_s(1200, self.campaign)
        with patch.object(ds.os, "replace", side_effect=PermissionError("test")):
            with self.assertRaises(PermissionError):
                ds.set_active_stale_s(600, self.campaign)
        self.assertEqual(ds.get_active_stale_s(self.campaign), 1200)


class WidgetTimeoutTests(FakeCampaign):
    def widget(self, text):
        variable = Mock()
        variable.get.return_value = text
        return SimpleNamespace(campaign=self.campaign, timeout_var=variable,
                               _flash=Mock())

    def test_apply_converts_fractional_minutes_to_saved_seconds(self):
        widget = self.widget("2.25")
        ui.MirrorWidget._apply_active_timeout(widget)
        self.assertEqual(ds.get_active_stale_s(self.campaign), 135)
        widget.timeout_var.set.assert_called_once_with("2.25")
        widget._flash.assert_called_once_with("saved")

    def test_invalid_entry_preserves_setting_and_reports_error(self):
        ds.set_active_stale_s(1200, self.campaign)
        for text in ("0", "-2", "NaN", "inf", "oops", ""):
            with self.subTest(text=text):
                widget = self.widget(text)
                ui.MirrorWidget._apply_active_timeout(widget)
                self.assertEqual(ds.get_active_stale_s(self.campaign), 1200)
                widget._flash.assert_called_once_with("enter positive minutes")

    def test_save_failure_reports_error(self):
        widget = self.widget("20")
        with patch.object(ds, "set_active_stale_s", side_effect=PermissionError):
            ui.MirrorWidget._apply_active_timeout(widget)
        widget._flash.assert_called_once_with("save failed: PermissionError")

    def test_snapshot_countdown_agrees_with_copy_exclusions(self):
        path, marker = self.marker(1200)
        ds.set_active_stale_s(1800, self.campaign)
        before = marker.stat().st_mtime_ns
        with patch.object(ui, "_process_snapshot", return_value={}):
            snap = ui.snapshot(self.campaign)
        self.assertIsNone(snap["error"])
        self.assertEqual(snap["active_stale_s"], 1800)
        self.assertEqual(snap["claims"][0]["path"], path)
        self.assertEqual(snap["claims"][0]["clears_in"], 600)
        self.assertEqual(marker.stat().st_mtime_ns, before)
        self.assertEqual(ds.active_paths(self.campaign), [path])
        xd, xf = ds._exclusions_for(ds.active_paths(self.campaign))
        self.assertIn(path, xf)
        self.assertEqual(xd, [])

    def test_snapshot_observes_changed_timeout_without_pruning(self):
        path, marker = self.marker(700)
        ds.set_active_stale_s(600, self.campaign)
        with patch.object(ui, "_process_snapshot", return_value={}):
            snap = ui.snapshot(self.campaign)
        self.assertEqual(snap["active_stale_s"], 600)
        self.assertEqual(snap["claims"], [])
        self.assertTrue(marker.exists())
        self.assertEqual(ds.active_paths(self.campaign), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)

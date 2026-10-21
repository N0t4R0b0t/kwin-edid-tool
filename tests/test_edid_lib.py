"""Tests for connector lookup in edid_lib against fake sysfs/debugfs trees."""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import edid_lib  # noqa: E402


def modeline(name: str, clock: float, sync: str = "-hsync +vsync") -> str:
    return f'Modeline "{name}"  {clock}  1024 1072 1104 1184  600 603 613 619 {sync}\n'


class CvtTimingTest(unittest.TestCase):
    def run_with(self, standard_clock: float, refresh: int, reduced_clock: float = 400.0):
        def fake_run(command, **kwargs):
            clock = reduced_clock if "-r" in command else standard_clock
            return subprocess.CompletedProcess(command, 0, stdout=modeline("m", clock), stderr="")

        with mock.patch.object(edid_lib.subprocess, "run", side_effect=fake_run) as run:
            timing = edid_lib.cvt_timing(3456, 2160, refresh)
        return timing, [call.args[0] for call in run.call_args_list]

    def test_a_clock_within_the_limit_keeps_standard_blanking(self):
        timing, commands = self.run_with(standard_clock=500.0, refresh=60)
        self.assertEqual(timing["pixel_clock_khz"], 500000)
        self.assertEqual(len(commands), 1)
        self.assertNotIn("-r", commands[0])

    def test_a_clock_over_the_limit_falls_back_to_reduced_blanking(self):
        timing, commands = self.run_with(standard_clock=642.0, refresh=60, reduced_clock=481.75)
        self.assertEqual(timing["pixel_clock_khz"], 481750)
        self.assertEqual(len(commands), 2)
        self.assertIn("-r", commands[1])

    def test_reduced_blanking_is_not_attempted_for_refresh_rates_it_rejects(self):
        timing, commands = self.run_with(standard_clock=642.0, refresh=30)
        self.assertEqual(timing["pixel_clock_khz"], 642000)
        self.assertEqual(len(commands), 1)

    def test_a_clock_exactly_at_the_limit_is_kept(self):
        timing, commands = self.run_with(standard_clock=edid_lib.MAX_TMDS_CLOCK_MHZ, refresh=60)
        self.assertEqual(len(commands), 1)

    def test_missing_modeline_raises(self):
        done = subprocess.CompletedProcess([], 0, stdout="# nothing\n", stderr="")
        with mock.patch.object(edid_lib.subprocess, "run", return_value=done):
            with self.assertRaisesRegex(RuntimeError, "no Modeline"):
                edid_lib.cvt_timing(1024, 600, 60)

    def test_sync_polarity_is_parsed(self):
        done = subprocess.CompletedProcess([], 0, stdout=modeline("m", 100.0, "+hsync -vsync"), stderr="")
        with mock.patch.object(edid_lib.subprocess, "run", return_value=done):
            timing = edid_lib.cvt_timing(1024, 600, 60)
        self.assertTrue(timing["h_sync_positive"])
        self.assertFalse(timing["v_sync_positive"])
        self.assertEqual(timing["h_blank"], 160)
        self.assertEqual(timing["v_sync_width"], 10)

    @unittest.skipUnless(shutil.which("cvt"), "needs the system cvt utility")
    def test_the_mode_that_the_driver_dropped_now_fits_under_the_limit(self):
        timing = edid_lib.cvt_timing(3456, 2160, 60)
        self.assertLessEqual(timing["pixel_clock_khz"], edid_lib.MAX_TMDS_CLOCK_MHZ * 1000)

    @unittest.skipUnless(shutil.which("cvt"), "needs the system cvt utility")
    def test_a_thirty_hertz_request_still_works_with_standard_blanking(self):
        self.assertGreater(edid_lib.cvt_timing(1024, 600, 30)["pixel_clock_khz"], 0)


class ConnectorLookupTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.sysfs = self.root / "sys_class_drm"
        self.debugfs = self.root / "debugfs_dri"
        self.sysfs.mkdir()
        self.debugfs.mkdir()
        for target, value in (("SYSFS_DRM_DIR", self.sysfs), ("DEBUGFS_DRI_DIR", self.debugfs)):
            patcher = mock.patch.object(edid_lib, target, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    def add_sysfs(self, card: int, connector: str, status: str = "connected") -> Path:
        path = self.sysfs / f"card{card}-{connector}"
        path.mkdir()
        (path / "status").write_text(f"{status}\n")
        return path

    def add_debugfs(self, name: str, connector: str) -> Path:
        path = self.debugfs / name / connector
        path.mkdir(parents=True)
        return path

    def test_single_debugfs_entry_is_returned(self):
        expected = self.add_debugfs("1", "HDMI-A-1")
        self.assertEqual(edid_lib.find_debugfs_connector("HDMI-A-1"), expected.resolve())

    def test_missing_debugfs_entry_raises(self):
        with self.assertRaisesRegex(RuntimeError, "no debugfs entry"):
            edid_lib.find_debugfs_connector("HDMI-A-1")

    def test_symlink_aliases_collapse_to_one_entry(self):
        self.add_debugfs("1", "HDMI-A-1")
        (self.debugfs / "0000:01:00.0").symlink_to(self.debugfs / "1")
        result = edid_lib.find_debugfs_connector("HDMI-A-1")
        self.assertEqual(result, (self.debugfs / "1" / "HDMI-A-1").resolve())

    def test_distinct_directories_resolve_to_the_sysfs_card(self):
        # The layout seen on kernel 6.18 with the NVIDIA driver: the card minor,
        # its PCI-address alias, and the render node all carry the connector.
        expected = self.add_debugfs("1", "HDMI-A-1")
        (self.debugfs / "0000:01:00.0").symlink_to(self.debugfs / "1")
        self.add_debugfs("128", "HDMI-A-1")
        self.add_sysfs(1, "HDMI-A-1")
        self.assertEqual(edid_lib.find_debugfs_connector("HDMI-A-1"), expected.resolve())

    def test_connected_card_wins_over_disconnected_one(self):
        self.add_debugfs("0", "HDMI-A-1")
        expected = self.add_debugfs("1", "HDMI-A-1")
        self.add_sysfs(0, "HDMI-A-1", status="disconnected")
        self.add_sysfs(1, "HDMI-A-1", status="connected")
        self.assertEqual(edid_lib.find_debugfs_connector("HDMI-A-1"), expected.resolve())

    def test_ambiguous_entries_without_sysfs_match_raise(self):
        self.add_debugfs("1", "HDMI-A-1")
        self.add_debugfs("128", "HDMI-A-1")
        with self.assertRaisesRegex(RuntimeError, "multiple debugfs entries"):
            edid_lib.find_debugfs_connector("HDMI-A-1")

    def test_sysfs_edid_prefers_connected_card(self):
        self.add_sysfs(0, "HDMI-A-1", status="disconnected")
        connected = self.add_sysfs(1, "HDMI-A-1", status="connected")
        self.assertEqual(edid_lib.find_sysfs_edid("HDMI-A-1"), connected / "edid")

    def test_sysfs_edid_single_card(self):
        connected = self.add_sysfs(1, "HDMI-A-1")
        self.assertEqual(edid_lib.find_sysfs_edid("HDMI-A-1"), connected / "edid")

    def test_sysfs_edid_missing_raises(self):
        with self.assertRaisesRegex(RuntimeError, "no sysfs EDID"):
            edid_lib.find_sysfs_edid("HDMI-A-1")

    def test_sysfs_connector_name_is_matched_exactly(self):
        self.add_sysfs(1, "HDMI-A-10")
        with self.assertRaisesRegex(RuntimeError, "no sysfs EDID"):
            edid_lib.find_sysfs_edid("HDMI-A-1")


if __name__ == "__main__":
    unittest.main()

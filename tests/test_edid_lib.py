"""Tests for connector lookup in edid_lib against fake sysfs/debugfs trees."""

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import edid_lib  # noqa: E402


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

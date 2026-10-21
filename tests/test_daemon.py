"""Tests for the Sunshine EDID helper daemon: mode matching, persistence, seeding and connect handling."""

import io
import json
import logging
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "integrations" / "sunshine"))

import daemon  # noqa: E402
import edid_lib  # noqa: E402

logging.disable(logging.CRITICAL)  # the error paths under test log tracebacks on purpose


def make_edid(extensions: int = 0) -> bytes:
    """A minimal EDID whose base block checksum validates."""
    block = bytearray(128)
    block[126] = extensions
    block[127] = (256 - sum(block[0:127]) % 256) % 256
    return bytes(block) + bytes(128 * extensions)


def output_with_modes(*modes: tuple[str, int, int, float]) -> dict:
    return {
        "name": "HDMI-A-1",
        "currentModeId": "",
        "modes": [{"id": mode_id, "size": {"width": w, "height": h}, "refreshRate": hz} for mode_id, w, h, hz in modes],
    }


class TempStateTestCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.state = self.root / "state"
        patcher = mock.patch.object(daemon, "STATE_DIR", self.state)
        patcher.start()
        self.addCleanup(patcher.stop)


class ModeMatchingTest(unittest.TestCase):
    def test_synthesis_refresh_rounds_up_to_a_multiple_of_60(self):
        self.assertEqual(daemon.synthesis_refresh(30), 60)
        self.assertEqual(daemon.synthesis_refresh(60), 60)
        self.assertEqual(daemon.synthesis_refresh(61), 120)
        self.assertEqual(daemon.synthesis_refresh(144), 180)

    def test_best_matching_mode_requires_the_exact_resolution(self):
        output = output_with_modes(("a", 1920, 1080, 60.0))
        self.assertIsNone(daemon.best_matching_mode(output, 1920, 1200, 60))

    def test_best_matching_mode_tolerates_slight_refresh_undershoot(self):
        output = output_with_modes(("a", 3456, 2160, 59.7))
        self.assertEqual(daemon.best_matching_mode(output, 3456, 2160, 60)["id"], "a")

    def test_best_matching_mode_rejects_a_refresh_that_is_too_low(self):
        output = output_with_modes(("a", 3456, 2160, 30.0))
        self.assertIsNone(daemon.best_matching_mode(output, 3456, 2160, 60))

    def test_best_matching_mode_prefers_the_smallest_qualifying_refresh(self):
        output = output_with_modes(("slow", 1920, 1080, 60.0), ("fast", 1920, 1080, 144.0))
        self.assertEqual(daemon.best_matching_mode(output, 1920, 1080, 50)["id"], "slow")


class PersistenceTest(TempStateTestCase):
    def test_no_state_file_means_no_modes(self):
        self.assertEqual(daemon.load_modes("HDMI-A-1"), [])

    def test_unreadable_state_file_means_no_modes(self):
        self.state.mkdir()
        (self.state / "HDMI-A-1.json").write_text("{not json")
        self.assertEqual(daemon.load_modes("HDMI-A-1"), [])

    def test_remembered_modes_round_trip_as_tuples(self):
        daemon.remember_mode("HDMI-A-1", (3456, 2160, 60))
        self.assertEqual(daemon.load_modes("HDMI-A-1"), [(3456, 2160, 60)])

    def test_remembering_a_known_mode_moves_it_to_the_end(self):
        daemon.remember_mode("HDMI-A-1", (1024, 600, 60))
        daemon.remember_mode("HDMI-A-1", (3456, 2160, 60))
        modes = daemon.remember_mode("HDMI-A-1", (1024, 600, 60))
        self.assertEqual(modes, [(3456, 2160, 60), (1024, 600, 60)])

    def test_oldest_modes_are_evicted_past_the_cap(self):
        for width in range(1000, 1000 + daemon.MAX_PERSISTED_MODES + 2):
            modes = daemon.remember_mode("HDMI-A-1", (width, 600, 60))
        self.assertEqual(len(modes), daemon.MAX_PERSISTED_MODES)
        self.assertNotIn((1000, 600, 60), modes)
        self.assertEqual(modes[-1], (1000 + daemon.MAX_PERSISTED_MODES + 1, 600, 60))

    def test_saving_leaves_no_temporary_file_behind(self):
        daemon.remember_mode("HDMI-A-1", (3456, 2160, 60))
        self.assertEqual([p.name for p in self.state.iterdir()], ["HDMI-A-1.json"])
        self.assertEqual(json.loads((self.state / "HDMI-A-1.json").read_text()), {"modes": [[3456, 2160, 60]]})


class BaselineEdidTest(TempStateTestCase):
    def sysfs_returning(self, data: bytes):
        path = self.root / "edid"
        path.write_bytes(data)
        return mock.patch.object(edid_lib, "find_sysfs_edid", return_value=path), path

    def test_valid_edid_check(self):
        self.assertTrue(daemon.is_valid_edid(make_edid()))
        self.assertTrue(daemon.is_valid_edid(make_edid(extensions=1)))
        self.assertFalse(daemon.is_valid_edid(b""))
        self.assertFalse(daemon.is_valid_edid(make_edid() + b"\x00"))
        self.assertFalse(daemon.is_valid_edid(bytes([1]) + bytes(127)))

    def test_first_read_is_cached_and_never_reread(self):
        patcher, path = self.sysfs_returning(make_edid())
        with patcher:
            first = daemon.baseline_edid("HDMI-A-1")
            path.write_bytes(make_edid(extensions=1))  # a driver that reflects our override
            second = daemon.baseline_edid("HDMI-A-1")
        self.assertEqual(first, make_edid())
        self.assertEqual(second, first)

    def test_an_empty_read_is_rejected_and_not_cached(self):
        patcher, _ = self.sysfs_returning(b"")
        with patcher, self.assertRaisesRegex(RuntimeError, "not a valid EDID"):
            daemon.baseline_edid("HDMI-A-1")
        self.assertFalse((self.state / "HDMI-A-1.edid").exists())

    def test_an_invalid_cached_baseline_is_captured_again(self):
        self.state.mkdir()
        (self.state / "HDMI-A-1.edid").write_bytes(b"")
        patcher, _ = self.sysfs_returning(make_edid())
        with patcher:
            self.assertEqual(daemon.baseline_edid("HDMI-A-1"), make_edid())
        self.assertEqual((self.state / "HDMI-A-1.edid").read_bytes(), make_edid())


class ApplyModesTest(TempStateTestCase):
    def test_override_is_the_baseline_plus_one_extension_block(self):
        self.state.mkdir()
        (self.state / "HDMI-A-1.edid").write_bytes(make_edid())
        debugfs = self.root / "debugfs"
        debugfs.mkdir()
        with mock.patch.object(edid_lib, "find_debugfs_connector", return_value=debugfs):
            daemon.apply_modes("HDMI-A-1", [(1920, 1080, 60), (3456, 2160, 60)])
        written = (debugfs / "edid_override").read_bytes()
        self.assertEqual(len(written), 256)
        self.assertEqual(written[126], 1)
        self.assertEqual(sum(written[0:128]) % 256, 0)


class SeedTest(TempStateTestCase):
    def run_seed(self, modes):
        with mock.patch.object(daemon, "wait_for_connector"), \
             mock.patch.object(daemon, "baseline_edid") as baseline, \
             mock.patch.object(daemon, "load_modes", return_value=modes), \
             mock.patch.object(daemon, "apply_modes") as apply, \
             mock.patch.object(edid_lib, "reprobe") as reprobe:
            daemon.seed()
        return baseline, apply, reprobe

    def test_nothing_is_applied_without_persisted_modes(self):
        baseline, apply, reprobe = self.run_seed([])
        baseline.assert_called_once_with(daemon.CONNECTOR)
        apply.assert_not_called()
        reprobe.assert_not_called()

    def test_persisted_modes_are_applied_and_the_connector_reprobed(self):
        modes = [(3456, 2160, 60)]
        _, apply, reprobe = self.run_seed(modes)
        apply.assert_called_once_with(daemon.CONNECTOR, modes)
        reprobe.assert_called_once_with(daemon.CONNECTOR)

    def test_wait_for_connector_returns_once_the_entry_exists(self):
        with mock.patch.object(edid_lib, "find_debugfs_connector", side_effect=[RuntimeError("no"), Path("/x")]) as find, \
             mock.patch.object(daemon.time, "sleep"):
            daemon.wait_for_connector("HDMI-A-1")
        self.assertEqual(find.call_count, 2)

    def test_wait_for_connector_gives_up_after_the_timeout(self):
        with mock.patch.object(edid_lib, "find_debugfs_connector", side_effect=RuntimeError("no")), \
             mock.patch.object(daemon.time, "sleep"), \
             mock.patch.object(daemon.time, "monotonic", side_effect=[0.0, 1.0, 99.0]):
            with self.assertRaises(RuntimeError):
                daemon.wait_for_connector("HDMI-A-1", timeout=30.0)


class HandleConnectTest(unittest.TestCase):
    NEW_MODE = {"id": "new", "size": {"width": 3456, "height": 2160}, "refreshRate": 60.0}

    def run_connect(self, outputs_calls, *, restart_enabled=True, restart_ok=True, waited=None, apply_error=None):
        patches = {
            "kscreen_outputs": mock.patch.object(daemon, "kscreen_outputs", side_effect=outputs_calls),
            "select": mock.patch.object(daemon, "select_mode_and_verify", return_value=True),
            "remember": mock.patch.object(daemon, "remember_mode", return_value=[(3456, 2160, 60)]),
            "apply": mock.patch.object(daemon, "apply_modes", side_effect=apply_error),
            "reprobe": mock.patch.object(edid_lib, "reprobe"),
            "restart": mock.patch.object(daemon, "restart_kwin", return_value=restart_ok),
            "wait": mock.patch.object(daemon, "wait_for_mode", return_value=waited),
            "flag": mock.patch.object(daemon, "RESTART_KWIN", restart_enabled),
        }
        started = {name: patch.start() for name, patch in patches.items()}
        for patch in patches.values():
            self.addCleanup(patch.stop)
        daemon.handle_connect("HDMI-A-1", 3456, 2160, 30)
        return started

    def test_unknown_connector_is_ignored(self):
        mocks = self.run_connect([[{"name": "DP-1", "modes": []}]])
        mocks["remember"].assert_not_called()

    def test_an_existing_mode_is_selected_without_touching_the_edid(self):
        existing = output_with_modes(("have", 3456, 2160, 60.0))
        mocks = self.run_connect([[existing]])
        mocks["select"].assert_called_once()
        mocks["remember"].assert_not_called()

    def test_a_mode_discovered_live_is_selected_without_restarting_kwin(self):
        before = output_with_modes()
        after = output_with_modes(("new", 3456, 2160, 60.0))
        mocks = self.run_connect([[before], [after]])
        mocks["remember"].assert_called_once_with("HDMI-A-1", (3456, 2160, 60))
        mocks["restart"].assert_not_called()
        mocks["select"].assert_called_once()

    def test_an_undiscovered_mode_restarts_kwin_then_selects_it(self):
        empty = output_with_modes()
        mocks = self.run_connect([[empty], [empty]], waited=self.NEW_MODE)
        mocks["restart"].assert_called_once()
        mocks["select"].assert_called_once_with("HDMI-A-1", self.NEW_MODE)

    def test_the_restart_can_be_disabled(self):
        empty = output_with_modes()
        mocks = self.run_connect([[empty], [empty]], restart_enabled=False)
        mocks["restart"].assert_not_called()
        mocks["select"].assert_not_called()

    def test_a_failed_restart_skips_waiting_for_the_mode(self):
        empty = output_with_modes()
        mocks = self.run_connect([[empty], [empty]], restart_ok=False)
        mocks["wait"].assert_not_called()
        mocks["select"].assert_not_called()

    def test_a_mode_still_missing_after_the_restart_is_not_selected(self):
        empty = output_with_modes()
        mocks = self.run_connect([[empty], [empty]], waited=None)
        mocks["select"].assert_not_called()

    def test_an_impossible_timing_stops_before_reprobing(self):
        mocks = self.run_connect([[output_with_modes()]], apply_error=edid_lib.ImpossibleTimingError("too big"))
        mocks["reprobe"].assert_not_called()

    def test_any_other_apply_failure_stops_before_reprobing(self):
        mocks = self.run_connect([[output_with_modes()]], apply_error=OSError("debugfs"))
        mocks["reprobe"].assert_not_called()


class KwinRestartTest(unittest.TestCase):
    def test_no_session_means_no_restart(self):
        with mock.patch.object(daemon, "find_session", side_effect=RuntimeError("no session")):
            self.assertFalse(daemon.restart_kwin())

    def run_restart(self, returncode):
        pw = mock.Mock(pw_name="rsalvador")
        with mock.patch.object(daemon, "find_session", return_value=("1000", {"XDG_RUNTIME_DIR": "/run/user/1000"})), \
             mock.patch.object(daemon.pwd, "getpwuid", return_value=pw), \
             mock.patch.object(daemon.subprocess, "run", return_value=mock.Mock(returncode=returncode, stderr="boom")) as run:
            return daemon.restart_kwin(), run

    def test_kwin_unit_is_restarted_as_the_desktop_user(self):
        ok, run = self.run_restart(0)
        self.assertTrue(ok)
        self.assertEqual(run.call_args.args[0], ["runuser", "-u", "rsalvador", "--", "systemctl", "--user", "restart", daemon.KWIN_UNIT])

    def test_a_failing_systemctl_is_reported_as_failure(self):
        ok, _ = self.run_restart(1)
        self.assertFalse(ok)

    def test_wait_for_mode_returns_the_mode_once_advertised(self):
        output = output_with_modes(("new", 3456, 2160, 60.0))
        with mock.patch.object(daemon.time, "sleep"), \
             mock.patch.object(daemon, "kscreen_outputs", side_effect=[RuntimeError("not up"), [output]]):
            self.assertEqual(daemon.wait_for_mode("HDMI-A-1", 3456, 2160, 60)["id"], "new")

    def test_wait_for_mode_gives_up_after_the_timeout(self):
        with mock.patch.object(daemon.time, "sleep"), \
             mock.patch.object(daemon.time, "monotonic", side_effect=[0.0, 1.0, 99.0]), \
             mock.patch.object(daemon, "kscreen_outputs", return_value=[output_with_modes()]):
            self.assertIsNone(daemon.wait_for_mode("HDMI-A-1", 3456, 2160, 60, timeout=30.0))


class RequestHandlerTest(unittest.TestCase):
    def handle(self, line: bytes):
        handler = daemon.Handler.__new__(daemon.Handler)
        handler.rfile = io.BytesIO(line)
        with mock.patch.object(daemon, "handle_connect") as handle_connect:
            handler.handle()
        return handle_connect

    def test_connect_request_is_dispatched_with_a_rounded_refresh(self):
        self.handle(b"connect,3456,2160,59.94\n").assert_called_once_with(daemon.CONNECTOR, 3456, 2160, 60)

    def test_malformed_request_is_ignored(self):
        self.handle(b"hello\n").assert_not_called()

    def test_unknown_command_is_ignored(self):
        self.handle(b"disconnect,1,2,3\n").assert_not_called()

    def test_non_numeric_fields_do_not_raise(self):
        self.handle(b"connect,a,b,c\n").assert_not_called()


class SessionDiscoveryTest(unittest.TestCase):
    def test_wayland_socket_is_found_under_the_user_runtime_dir(self):
        listings = {"/run/user": ["1000"], "/run/user/1000": ["bus", "wayland-0", "wayland-0.lock"]}
        with mock.patch.object(daemon.os, "listdir", side_effect=lambda p: listings[p]), \
             mock.patch.object(daemon.os.path, "exists", return_value=True), \
             mock.patch.dict(daemon.os.environ, {}, clear=False):
            uid, env = daemon.find_session()
        self.assertEqual(uid, "1000")
        self.assertEqual(env["WAYLAND_DISPLAY"], "wayland-0")
        self.assertEqual(env["XDG_RUNTIME_DIR"], "/run/user/1000")
        self.assertEqual(env["DBUS_SESSION_BUS_ADDRESS"], "unix:path=/run/user/1000/bus")

    def test_no_wayland_session_raises(self):
        listings = {"/run/user": ["1000"], "/run/user/1000": ["bus"]}
        with mock.patch.object(daemon.os, "listdir", side_effect=lambda p: listings[p]):
            with self.assertRaisesRegex(RuntimeError, "no active Wayland session"):
                daemon.find_session()


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/env python3
"""
Root-owned daemon: listens on a Unix socket for "connect,width,height,refresh"
requests from Sunshine's "Do Command (On Client Connect)" hook, and makes sure
the target connector's EDID advertises a mode close to that resolution -
appending a new custom-mode extension block only if nothing already fits,
then selecting the best match via kscreen-doctor.

Note on timing: Sunshine picks a resolution for the CURRENT connection before
this hook fires (display_device::configure_display() / set_display_resolution()
in src/nvhttp.cpp run before proc::proc.execute(), which is what triggers this
hook). So the first time a new resolution is requested, that connection streams
at Sunshine's own closest-match fallback - this daemon just makes sure the EXACT
resolution is available for next time (a reconnect, or a future session).

No disconnect handling and no per-session teardown - modes only ever get
added, never removed automatically. Run edid-custom-resolutions.py --reset
manually if you want to wipe the accumulated custom modes and start over.

Persistence: every mode this daemon is asked for is recorded under STATE_DIR
and re-applied at boot by `--seed` (see sunshine-edid-seed.service), which
runs before the display manager so KWin starts up already knowing about them.
This is what makes a resolution survive a reboot - the debugfs override itself
is volatile, and the daemon only ever writes it in response to a connection,
which is far too late for an already-running compositor.

Install: see install.sh in this directory.
"""

from __future__ import annotations

import argparse
import json
import logging
import math
import os
import pwd
import socketserver
import subprocess
import sys
import time
from pathlib import Path

# edid_lib.py must be deployed alongside this file (install.sh and the
# PKGBUILD both do this) - Python's automatic "script's own directory"
# sys.path entry then finds it with no extra path handling needed. Not
# importable directly from a bare git checkout, where it lives two
# directories up instead; that's not a supported way to run this.
import edid_lib

SOCKET_PATH = os.environ.get("SUNSHINE_EDID_HELPER_SOCKET", "/run/sunshine-edid-helper.sock")
CONNECTOR = os.environ.get("SUNSHINE_EDID_HELPER_CONNECTOR", "HDMI-A-1")
STATE_DIR = Path(os.environ.get("SUNSHINE_EDID_HELPER_STATE_DIR", "/var/lib/sunshine-edid-helper"))

def env_enabled(name: str, default: bool) -> bool:
    """Read an on/off switch from the environment ("1"/"true"/"yes" and "0"/"false"/"no")."""
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() not in ("0", "false", "no", "")


# Restarting KWin is the only way to get a newly-added mode discovered live on drivers
# where reprobing doesn't work (NVIDIA), but it is OFF by default: on Plasma 6, restarting
# plasma-kwin_wayland.service also stops plasma-workspace-wayland.target, which ends the whole
# graphical session (every app) and the session does not come back by itself. Without it, a
# new mode is persisted and becomes selectable at the next boot via --seed.
RESTART_KWIN = env_enabled("SUNSHINE_EDID_HELPER_RESTART_KWIN", False)
KWIN_UNIT = "plasma-kwin_wayland.service"

# How close an existing mode's resolution/refresh needs to be to count as "already there".
REFRESH_TOLERANCE_HZ = 2.0

# A CTA-861 extension block fits 6 DTDs and we only ever append one block, so
# the accumulated set is capped - oldest-requested modes get evicted first.
MAX_PERSISTED_MODES = 6

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sunshine-edid-helper")


def find_session() -> tuple[str, dict]:
    """kscreen-doctor needs the desktop user's Wayland session to connect to KWin
    at all - this daemon runs as root via a system-level systemd unit, which has
    none of that, so every kscreen-doctor call would otherwise crash outright
    (confirmed live: SIGABRT, Qt can't find a display). Auto-detect the running
    session by finding a wayland-* socket under /run/user/<uid>/ rather than
    requiring the UID to be hardcoded - overridable via SUNSHINE_EDID_HELPER_UID
    for multi-user setups where auto-detection would be ambiguous.
    """
    override_uid = os.environ.get("SUNSHINE_EDID_HELPER_UID")
    uid_dirs = [override_uid] if override_uid else os.listdir("/run/user")

    for uid in uid_dirs:
        runtime_dir = f"/run/user/{uid}"
        try:
            sockets = [f for f in os.listdir(runtime_dir) if f.startswith("wayland-") and not f.endswith(".lock")]
        except (FileNotFoundError, NotADirectoryError, PermissionError):
            continue
        if not sockets:
            continue
        env = dict(os.environ)
        env["XDG_RUNTIME_DIR"] = runtime_dir
        env["WAYLAND_DISPLAY"] = sockets[0]
        bus_path = f"{runtime_dir}/bus"
        if os.path.exists(bus_path):
            env["DBUS_SESSION_BUS_ADDRESS"] = f"unix:path={bus_path}"
        return str(uid), env

    raise RuntimeError("no active Wayland session found under /run/user/*/ - is anyone logged into a graphical session?")


def find_session_env() -> dict:
    return find_session()[1]


def kscreen_outputs() -> list[dict]:
    result = subprocess.run(["kscreen-doctor", "-j"], capture_output=True, text=True, check=True, env=find_session_env())
    return json.loads(result.stdout)["outputs"]


def find_output(outputs: list[dict], connector: str) -> dict | None:
    return next((o for o in outputs if o.get("name") == connector), None)


def _state_file(connector: str) -> Path:
    return STATE_DIR / f"{connector}.json"


def _baseline_file(connector: str) -> Path:
    return STATE_DIR / f"{connector}.edid"


def load_modes(connector: str) -> list[tuple[int, int, int]]:
    """Every mode we've ever synthesized for this connector, oldest first."""
    try:
        raw = json.loads(_state_file(connector).read_text())
    except FileNotFoundError:
        return []
    except (ValueError, OSError):
        log.exception("state file for %s is unreadable, starting over", connector)
        return []
    return [tuple(m) for m in raw.get("modes", [])]


def remember_mode(connector: str, mode: tuple[int, int, int]) -> list[tuple[int, int, int]]:
    """Append a mode to the persisted set (moving it to the front of the eviction
    queue if already known) and return the full set to apply."""
    modes = [m for m in load_modes(connector) if m != mode]
    modes.append(mode)
    modes = modes[-MAX_PERSISTED_MODES:]

    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = _state_file(connector).with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"modes": [list(m) for m in modes]}, indent=2))
    tmp.replace(_state_file(connector))  # atomic - a torn state file on a crash would strand every saved mode
    return modes


def is_valid_edid(data: bytes) -> bool:
    """Whole 128-byte blocks, with a base block whose checksum validates."""
    return len(data) >= 128 and len(data) % 128 == 0 and sum(data[0:128]) % 256 == 0


def baseline_edid(connector: str) -> bytes:
    """The connector's real, un-overridden EDID.

    Cached on disk the first time we see it, and never re-read from sysfs
    afterwards. Two reasons: on drivers where reprobing DOES work, sysfs starts
    reflecting our own override, so rebuilding from it would append a second
    extension block on top of the first and keep growing. And on drivers where
    it doesn't (NVIDIA), sysfs stays pristine but we'd silently rebuild from
    scratch each time - which is exactly the bug that made every previously
    added mode vanish as soon as a different resolution was requested.

    The cache is captured by `--seed` at boot, before any override can exist.
    """
    cached = _baseline_file(connector)
    if cached.exists():
        data = cached.read_bytes()
        if is_valid_edid(data):
            return data
        log.warning("cached baseline EDID for %s is not a valid EDID, capturing it again", connector)

    original = edid_lib.find_sysfs_edid(connector).read_bytes()
    if not is_valid_edid(original):
        # An empty or garbled read (display off, connector not probed yet) must never be
        # cached: every later mode would fail against it until someone deleted the file.
        raise RuntimeError(f"sysfs EDID for {connector} is not a valid EDID ({len(original)} bytes) - is the display on?")
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    cached.write_bytes(original)
    log.info("cached baseline EDID for %s (%d bytes) at %s", connector, len(original), cached)
    return original


def apply_modes(connector: str, modes: list[tuple[int, int, int]]) -> None:
    """Rebuild the override from the baseline EDID plus the full accumulated set."""
    patched = edid_lib.append_modes_to_edid(baseline_edid(connector), modes)
    (edid_lib.find_debugfs_connector(connector) / "edid_override").write_bytes(patched)


def restart_kwin() -> bool:
    """Restart the compositor so it re-reads the connector's EDID.

    KWin only ever scans a connector's mode list at startup, so on a driver
    that won't reprobe live this is the only way to make a freshly added mode
    selectable within the current session. Goes through KWin's systemd user
    unit rather than `kwin_wayland --replace`: the unit is what actually owns
    the process under a Plasma 6 systemd-managed session, so letting systemd
    do the handover keeps the rest of graphical-session.target intact.
    """
    try:
        uid, env = find_session()
        user = pwd.getpwuid(int(uid)).pw_name
    except (RuntimeError, KeyError, ValueError):
        log.exception("can't identify the desktop session, not restarting KWin")
        return False

    log.warning("restarting %s as %s - this ends the whole graphical session, not just the stream", KWIN_UNIT, user)
    result = subprocess.run(
        ["runuser", "-u", user, "--", "systemctl", "--user", "restart", KWIN_UNIT],
        capture_output=True, text=True, env=env,
    )
    if result.returncode != 0:
        log.error("failed to restart %s: %s", KWIN_UNIT, result.stderr.strip())
        return False
    return True


def wait_for_mode(connector: str, width: int, height: int, refresh: int, timeout: float = 30.0) -> dict | None:
    """Poll until the compositor is back up and advertising the mode. Called
    after a KWin restart, so early failures are just 'not up yet', not errors."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        time.sleep(1.0)
        try:
            output = find_output(kscreen_outputs(), connector)
        except Exception:
            continue
        mode = output and best_matching_mode(output, width, height, refresh)
        if mode:
            return mode
    return None


def synthesis_refresh(requested_refresh: int) -> int:
    """Pick which refresh rate to actually build a new custom mode at.

    CVT-synthesized timings are always slightly imprecise versus their nominal
    refresh, and that error is proportionally much larger at low refresh rates:
    confirmed live, a requested 30Hz mode measured ~29.66Hz (~1.1% off) while a
    60Hz mode measured ~59.85Hz (~0.25% off). A static desktop never exposes
    that mismatch, but KWin's compositor animations (window slides, hover
    previews) are sensitive to consistent frame timing - a 30Hz custom mode
    was reproducibly laggy specifically during those effects, and fixed
    completely by using 60Hz instead.

    Sunshine encodes/streams at whatever fps the client actually requested,
    completely independent of the display mode's own refresh rate (confirmed
    against Sunshine's own capture/encode loop: capture happens at the
    display's real refresh, encoding paces to the client's requested fps
    separately) - so building the mode at a clean 60Hz-multiple doesn't force
    a weak client to render faster than it can. It just gives KWin a
    proportionally precise timing to pace against, while the client still
    gets whatever fps it asked for.
    """
    return max(60, math.ceil(requested_refresh / 60) * 60)


def best_matching_mode(output: dict, width: int, height: int, refresh: int) -> dict | None:
    """Find the mode on this output closest to the request - exact resolution
    match required. Refresh only needs to be at least what was requested
    (within a small undershoot tolerance for CVT's own imprecision, e.g. a
    60Hz request commonly decodes to ~59.7Hz) - Sunshine encodes at whatever
    fps the client actually asked for regardless of the mode's own refresh,
    so a mode built at a higher refresh (see synthesis_refresh()) still
    satisfies a lower-fps request correctly. Prefers the smallest qualifying
    refresh, i.e. the closest match from at-or-above the request."""
    candidates = [m for m in output["modes"] if m["size"]["width"] == width and m["size"]["height"] == height]
    if not candidates:
        return None
    tolerance = max(REFRESH_TOLERANCE_HZ, refresh * 0.05)
    qualifying = [m for m in candidates if m["refreshRate"] >= refresh - tolerance]
    if not qualifying:
        return None
    return min(qualifying, key=lambda m: m["refreshRate"])


def select_mode(connector: str, mode_id: str) -> None:
    subprocess.run(["kscreen-doctor", f"output.{connector}.mode.{mode_id}"], check=False, env=find_session_env())


def select_mode_and_verify(connector: str, mode: dict) -> bool:
    """Select a mode and confirm the compositor actually applied it, rather than
    trusting kscreen-doctor's exit code (which doesn't reflect whether the driver
    accepted the modeset - we already got burned once this session by an approach
    that looked correct at the protocol level but the driver rejected outright).
    A rejected selection leaves the previous mode active (confirmed via direct
    testing earlier this session), so failure here is safe, just a no-op."""
    select_mode(connector, mode["id"])
    outputs = kscreen_outputs()
    output = find_output(outputs, connector)
    applied = bool(output) and output.get("currentModeId") == mode["id"]
    if not applied:
        log.error(
            "mode %s (%dx%d@%.2f) on %s was NOT applied - driver likely rejected it, display remains on its previous mode",
            mode["id"], mode["size"]["width"], mode["size"]["height"], mode["refreshRate"], connector,
        )
    return applied


def handle_connect(connector: str, width: int, height: int, refresh: int) -> None:
    outputs = kscreen_outputs()
    output = find_output(outputs, connector)
    if not output:
        log.warning("no output named %r currently known to kscreen-doctor", connector)
        return

    existing = best_matching_mode(output, width, height, refresh)
    if existing:
        log.info("%dx%d@%d already available as mode %s (%.2fHz) on %s, selecting it", width, height, refresh, existing["id"], existing["refreshRate"], connector)
        if select_mode_and_verify(connector, existing):
            log.info("confirmed applied")
        return

    build_refresh = synthesis_refresh(refresh)
    log.info(
        "%dx%d@%d not available on %s, extending EDID (building the mode at %dHz rather than the "
        "requested %dHz for proportionally precise CVT timing - Sunshine will still encode/stream "
        "at the client's actual requested fps regardless of the mode's own refresh)",
        width, height, refresh, connector, build_refresh, refresh,
    )
    try:
        # Persist BEFORE applying: if anything below fails or the machine dies
        # mid-way, the mode is still recorded and the next --seed will apply it.
        modes = remember_mode(connector, (width, height, build_refresh))
        apply_modes(connector, modes)
    except edid_lib.ImpossibleTimingError as e:
        log.error("can't encode %dx%d@%d in an EDID DTD: %s", width, height, build_refresh, e)
        return
    except Exception:
        log.exception("failed to build/apply EDID extension for %dx%d@%d on %s", width, height, build_refresh, connector)
        return

    edid_lib.reprobe(connector)

    outputs = kscreen_outputs()
    output = find_output(outputs, connector)
    new_mode = output and best_matching_mode(output, width, height, refresh)

    if not new_mode:
        log.info(
            "%dx%d@%d added to the EDID but KWin hasn't discovered it live on %s - expected on NVIDIA, "
            "which doesn't support live reprobing. It's persisted now (%d mode(s) saved), so it will be "
            "seeded before KWin starts on every subsequent boot.",
            width, height, refresh, connector, len(modes),
        )
        if not RESTART_KWIN:
            log.warning("%dx%d@%d is persisted and becomes selectable after the next reboot (KWin auto-restart is off)", width, height, refresh)
            return
        if not restart_kwin():
            return
        new_mode = wait_for_mode(connector, width, height, refresh)
        if not new_mode:
            log.error(
                "KWin restarted but %dx%d@%d still isn't advertised on %s - the mode stays persisted, "
                "check `kscreen-doctor -o` and the seeding unit",
                width, height, refresh, connector,
            )
            return

    log.info("added new mode %s (%.2fHz) on %s, selecting it", new_mode["id"], new_mode["refreshRate"], connector)
    if select_mode_and_verify(connector, new_mode):
        log.info("confirmed applied - %dx%d@%d is now genuinely usable on %s", width, height, refresh, connector)


class Handler(socketserver.StreamRequestHandler):
    def handle(self) -> None:
        try:
            line = self.rfile.readline().decode().strip()
            parts = line.split(",")
            if len(parts) != 4 or parts[0] != "connect":
                log.warning("ignoring malformed request: %r", line)
                return
            _, width_s, height_s, refresh_s = parts
            width, height, refresh = int(width_s), int(height_s), round(float(refresh_s))
            handle_connect(CONNECTOR, width, height, refresh)
        except Exception:
            log.exception("error handling request")


def wait_for_connector(connector: str, timeout: float = 30.0) -> None:
    """Block until the connector's debugfs entry exists. At boot we deliberately
    run before the display manager, which can be earlier than the GPU driver
    finishing its own setup - so the connector may not be there on first look."""
    deadline = time.monotonic() + timeout
    while True:
        try:
            edid_lib.find_debugfs_connector(connector)
            return
        except RuntimeError:
            if time.monotonic() >= deadline:
                raise
            time.sleep(0.5)


def seed() -> None:
    """Re-apply every persisted mode, before KWin starts.

    This is the half that makes custom resolutions survive a reboot. The
    debugfs override is volatile, and the socket handler only writes it in
    response to a client connecting - which always happens after the
    compositor has already scanned the connector, and is therefore always too
    late. Applying the saved set here instead means KWin comes up with the
    modes already present and no restart is needed at all.
    """
    wait_for_connector(CONNECTOR)

    # Captures the pristine EDID on first boot after install, while we can still
    # be certain no override is in place.
    baseline_edid(CONNECTOR)

    modes = load_modes(CONNECTOR)
    if not modes:
        log.info("no persisted modes for %s yet, nothing to seed", CONNECTOR)
        return

    # No reprobe here: no compositor exists yet to notify, and at this point plymouth is
    # the only thing driving the display. Toggling the connector's force file under it
    # coincided with plymouth-quit hanging on the first boot that did so, which in turn
    # kept graphical.target from ever completing. KWin reads the override when it starts.
    apply_modes(CONNECTOR, modes)
    log.info("seeded %d persisted mode(s) onto %s: %s", len(modes), CONNECTOR, ", ".join(f"{w}x{h}@{r}" for w, h, r in modes))


def main() -> None:
    parser = argparse.ArgumentParser(description="Sunshine EDID helper daemon")
    parser.add_argument("--seed", action="store_true", help="Apply persisted modes and exit (run at boot, before the display manager)")
    args = parser.parse_args()

    if os.geteuid() != 0:
        raise SystemExit("must run as root (writes to debugfs)")

    if args.seed:
        seed()
        return

    if os.path.exists(SOCKET_PATH):
        os.remove(SOCKET_PATH)

    server = socketserver.UnixStreamServer(SOCKET_PATH, Handler)
    os.chmod(SOCKET_PATH, 0o666)  # sunshine runs as a regular user and needs to write to this
    log.info("listening on %s for connector %s", SOCKET_PATH, CONNECTOR)
    try:
        server.serve_forever()
    finally:
        os.remove(SOCKET_PATH)


if __name__ == "__main__":
    if sys.platform != "linux":
        raise SystemExit("linux only")
    main()

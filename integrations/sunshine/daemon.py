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

Install: see install.sh in this directory.
"""

from __future__ import annotations

import json
import logging
import os
import socketserver
import subprocess
import sys

# edid_lib.py must be deployed alongside this file (install.sh and the
# PKGBUILD both do this) - Python's automatic "script's own directory"
# sys.path entry then finds it with no extra path handling needed. Not
# importable directly from a bare git checkout, where it lives two
# directories up instead; that's not a supported way to run this.
import edid_lib

SOCKET_PATH = os.environ.get("SUNSHINE_EDID_HELPER_SOCKET", "/run/sunshine-edid-helper.sock")
CONNECTOR = os.environ.get("SUNSHINE_EDID_HELPER_CONNECTOR", "HDMI-A-1")

# How close an existing mode's resolution/refresh needs to be to count as "already there".
REFRESH_TOLERANCE_HZ = 2.0

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger("sunshine-edid-helper")


def find_session_env() -> dict:
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
        return env

    raise RuntimeError("no active Wayland session found under /run/user/*/ - is anyone logged into a graphical session?")


def kscreen_outputs() -> list[dict]:
    result = subprocess.run(["kscreen-doctor", "-j"], capture_output=True, text=True, check=True, env=find_session_env())
    return json.loads(result.stdout)["outputs"]


def find_output(outputs: list[dict], connector: str) -> dict | None:
    return next((o for o in outputs if o.get("name") == connector), None)


def best_matching_mode(output: dict, width: int, height: int, refresh: int) -> dict | None:
    """Find the mode on this output closest to the request - exact resolution match
    required, refresh within tolerance (CVT-RB timings don't land on the nominal
    refresh exactly, e.g. a 60Hz request commonly decodes to ~59.7Hz)."""
    candidates = [m for m in output["modes"] if m["size"]["width"] == width and m["size"]["height"] == height]
    if not candidates:
        return None
    best = min(candidates, key=lambda m: abs(m["refreshRate"] - refresh))
    if abs(best["refreshRate"] - refresh) > max(REFRESH_TOLERANCE_HZ, refresh * 0.05):
        return None
    return best


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

    log.info("%dx%d@%d not available on %s, extending EDID", width, height, refresh, connector)
    try:
        sysfs_edid_path = edid_lib.find_sysfs_edid(connector)
        original = sysfs_edid_path.read_bytes()
        patched = edid_lib.append_modes_to_edid(original, [(width, height, refresh)])

        debugfs_path = edid_lib.find_debugfs_connector(connector) / "edid_override"
        debugfs_path.write_bytes(patched)
    except edid_lib.ImpossibleTimingError as e:
        log.error("can't encode %dx%d@%d in an EDID DTD: %s", width, height, refresh, e)
        return
    except Exception:
        log.exception("failed to build/apply EDID extension for %dx%d@%d on %s", width, height, refresh, connector)
        return

    edid_lib.reprobe(connector)

    outputs = kscreen_outputs()
    output = find_output(outputs, connector)
    new_mode = output and best_matching_mode(output, width, height, refresh)
    if not new_mode:
        log.error("EDID override applied but %dx%d@%d still not found on %s after reprobe", width, height, refresh, connector)
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


def main() -> None:
    if os.geteuid() != 0:
        raise SystemExit("must run as root (writes to debugfs)")

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

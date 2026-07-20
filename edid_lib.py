"""
Shared EDID-patching logic: append custom-resolution modes to a real display's
EDID via a new CTA-861 extension block, without touching anything already
advertised. Used by both the manual CLI tool (edid-custom-resolutions.py) and
the on-connect helper daemon (daemon.py).

Background: only already-EDID-advertised modes get accepted cleanly by
KWin/the NVIDIA DRM-KMS driver - synthesizing modes at runtime via
kde_mode_list_v2 gets rejected by the driver at the kernel level (confirmed
by direct protocol testing against a real KWin/NVIDIA session). Baking a
resolution into the EDID instead makes it a genuinely real, driver-accepted
mode, so Sunshine's normal "select an already-advertised mode" path
(kwin_set_display_resolution in src/platform/linux/kwingrab.cpp) just works.
"""

from __future__ import annotations

import glob
import re
import subprocess
from pathlib import Path

# EDID Detailed Timing Descriptor field limits (VESA EDID spec).
MAX_PIXEL_CLOCK_10KHZ = 0xFFFF  # 655.35 MHz
MAX_ACTIVE_OR_BLANK = 0xFFF  # 12-bit field, 4095


class ImpossibleTimingError(ValueError):
    """Raised when a requested resolution/refresh can't be encoded in an EDID DTD at all."""


def parse_resolution(spec: str) -> tuple[int, int, int]:
    m = re.fullmatch(r"(\d+)x(\d+)@(\d+)", spec)
    if not m:
        raise ValueError(f"expected WIDTHxHEIGHT@REFRESH (e.g. 1024x600@60), got {spec!r}")
    width, height, refresh = (int(g) for g in m.groups())
    if refresh % 60 != 0:
        raise ValueError(f"{spec}: only multiples of 60Hz are supported (cvt -r requires it)")
    return width, height, refresh


def cvt_reduced_blanking(width: int, height: int, refresh: int) -> dict:
    """Shell out to the system `cvt` utility for VESA CVT-RB timing math instead of
    reimplementing it - avoids an entire class of off-by-one/rounding bugs."""
    result = subprocess.run(["cvt", "-r", str(width), str(height), str(refresh)], capture_output=True, text=True, check=True)
    modeline = next((line for line in result.stdout.splitlines() if line.startswith("Modeline")), None)
    if not modeline:
        raise RuntimeError(f"cvt produced no Modeline for {width}x{height}@{refresh}:\n{result.stdout}")

    # Modeline "1024x600R"   43.75  1024 1072 1104 1184  600 603 613 619 +hsync -vsync
    parts = modeline.split()
    pixel_clock_mhz = float(parts[2])
    h_active, h_sync_start, h_sync_end, h_total = (int(x) for x in parts[3:7])
    v_active, v_sync_start, v_sync_end, v_total = (int(x) for x in parts[7:11])
    h_sync_positive = parts[11] == "+hsync"
    v_sync_positive = parts[12] == "+vsync"

    return {
        "pixel_clock_khz": round(pixel_clock_mhz * 1000),
        "h_active": h_active,
        "h_blank": h_total - h_active,
        "h_sync_offset": h_sync_start - h_active,
        "h_sync_width": h_sync_end - h_sync_start,
        "v_active": v_active,
        "v_blank": v_total - v_active,
        "v_sync_offset": v_sync_start - v_active,
        "v_sync_width": v_sync_end - v_sync_start,
        "h_sync_positive": h_sync_positive,
        "v_sync_positive": v_sync_positive,
    }


def _check_field(name: str, value: int, max_value: int) -> None:
    if not (0 <= value <= max_value):
        raise ImpossibleTimingError(f"{name}={value} doesn't fit in its EDID field (max {max_value}) - resolution/refresh not encodable")


def pack_detailed_timing(t: dict) -> bytes:
    """Pack a VESA EDID 18-byte Detailed Timing Descriptor from cvt_reduced_blanking()'s output.
    Raises ImpossibleTimingError instead of silently truncating if any field overflows."""
    pclk_10khz = t["pixel_clock_khz"] // 10
    h_active, h_blank = t["h_active"], t["h_blank"]
    v_active, v_blank = t["v_active"], t["v_blank"]
    h_sync_off, h_sync_w = t["h_sync_offset"], t["h_sync_width"]
    v_sync_off, v_sync_w = t["v_sync_offset"], t["v_sync_width"]

    _check_field("pixel_clock", pclk_10khz, MAX_PIXEL_CLOCK_10KHZ)
    _check_field("h_active", h_active, MAX_ACTIVE_OR_BLANK)
    _check_field("h_blank", h_blank, MAX_ACTIVE_OR_BLANK)
    _check_field("v_active", v_active, MAX_ACTIVE_OR_BLANK)
    _check_field("v_blank", v_blank, MAX_ACTIVE_OR_BLANK)
    _check_field("h_sync_offset", h_sync_off, 0x3FF)  # 10-bit field
    _check_field("h_sync_width", h_sync_w, 0x3FF)
    _check_field("v_sync_offset", v_sync_off, 0x3F)  # 6-bit field
    _check_field("v_sync_width", v_sync_w, 0x3F)

    b = bytearray(18)
    b[0:2] = pclk_10khz.to_bytes(2, "little")
    b[2] = h_active & 0xFF
    b[3] = h_blank & 0xFF
    b[4] = ((h_active >> 8) & 0xF) << 4 | ((h_blank >> 8) & 0xF)
    b[5] = v_active & 0xFF
    b[6] = v_blank & 0xFF
    b[7] = ((v_active >> 8) & 0xF) << 4 | ((v_blank >> 8) & 0xF)
    b[8] = h_sync_off & 0xFF
    b[9] = h_sync_w & 0xFF
    b[10] = ((v_sync_off & 0xF) << 4) | (v_sync_w & 0xF)
    b[11] = (
        (((h_sync_off >> 8) & 0x3) << 6)
        | (((h_sync_w >> 8) & 0x3) << 4)
        | (((v_sync_off >> 4) & 0x3) << 2)
        | ((v_sync_w >> 4) & 0x3)
    )
    b[12] = 0  # H image size (mm) - unknown/irrelevant for a synthesized mode
    b[13] = 0  # V image size (mm)
    b[14] = 0
    b[15] = 0  # H border
    b[16] = 0  # V border
    # Digital separate sync (bits 4,3 set), plus polarity bits.
    b[17] = 0x18 | ((1 if t["v_sync_positive"] else 0) << 2) | ((1 if t["h_sync_positive"] else 0) << 1)
    return bytes(b)


def build_extension_block(modes: list[tuple[int, int, int]]) -> bytes:
    """Build a standalone CTA-861 extension block containing only Detailed Timing
    Descriptors for the given modes - no data block collection, so DTDs start
    immediately at byte 4. Raises ImpossibleTimingError if too many modes are
    requested to fit (max 6 - 108 of the 123 available bytes)."""
    if len(modes) * 18 > 123:
        raise ImpossibleTimingError(f"{len(modes)} modes don't fit in one extension block (max 6)")

    block = bytearray(128)
    block[0] = 0x02  # CTA-861 extension tag
    block[1] = 0x03  # revision 3
    block[2] = 4  # DTD offset - no data block collection before the DTDs
    block[3] = 0x00  # no underscan/audio/YCbCr, 0 native formats

    offset = 4
    for width, height, refresh in modes:
        timing = cvt_reduced_blanking(width, height, refresh)
        descriptor = pack_detailed_timing(timing)
        block[offset : offset + 18] = descriptor
        offset += 18

    block[127] = (256 - sum(block[0:127]) % 256) % 256
    assert sum(block) % 256 == 0
    return bytes(block)


def patch_base_block_extension_count(base: bytearray, new_count: int) -> None:
    base[126] = new_count
    base[127] = 0
    base[127] = (256 - sum(base) % 256) % 256
    assert sum(base) % 256 == 0


def append_modes_to_edid(original: bytes, modes: list[tuple[int, int, int]]) -> bytes:
    """Return a new EDID blob with one extra CTA-861 extension block appended,
    containing the given custom modes. `original` is left conceptually
    untouched - only its extension-count byte and checksum are patched."""
    if len(original) < 128 or len(original) % 128 != 0:
        raise ValueError(f"unexpected EDID length {len(original)}")
    if sum(original[0:128]) % 256 != 0:
        raise ValueError("base block checksum doesn't validate - refusing to build on top of it")

    patched = bytearray(original)
    extension = build_extension_block(modes)
    patch_base_block_extension_count(patched, new_count=patched[126] + 1)
    return bytes(patched) + extension


def find_debugfs_connector(connector: str) -> Path:
    candidates = glob.glob(f"/sys/kernel/debug/dri/*/{connector}")
    if not candidates:
        raise RuntimeError(
            f"no debugfs entry found for connector {connector!r} under /sys/kernel/debug/dri/*/\n"
            "(debugfs must be mounted, and this must run as root)"
        )
    if len(candidates) > 1:
        raise RuntimeError(f"multiple debugfs entries matched {connector!r}: {candidates} - pass a more specific connector name")
    return Path(candidates[0])


def find_sysfs_edid(connector: str) -> Path:
    candidates = glob.glob(f"/sys/class/drm/card*-{connector}/edid")
    if not candidates:
        raise RuntimeError(f"no sysfs EDID found for connector {connector!r} under /sys/class/drm/")
    return Path(candidates[0])


def reprobe(connector: str) -> None:
    """Force the kernel to fire a hotplug notification for this connector, so
    KWin rescans its mode list, without ever asking the compositor to disable
    the output itself.

    An earlier version of this used `kscreen-doctor output.CONNECTOR.disable`
    then `.enable` - that hangs indefinitely if the connector happens to be
    the only currently-enabled output, since KWin won't actually disable your
    sole active display (confirmed live: the disable request never returns).
    Writing to the DRM debugfs `force` file instead makes the kernel call
    drm_kms_helper_hotplug_event() directly, which is a notification to the
    compositor, not a request - KWin decides what to do with it, and a
    hotplug notification alone doesn't imply "go disable this output".

    The value written must never represent "disconnected" - "off" would
    likely trigger the exact same auto-disable behavior we're trying to
    avoid. Toggling through "on" (always safe - it just means "definitely
    connected", which is already true) and back to whatever the connector's
    force value actually was beforehand triggers the notification without
    leaving any lasting side effect on its auto-detection behavior.
    """
    force_path = find_debugfs_connector(connector) / "force"
    original = force_path.read_text().strip()
    force_path.write_text("on")
    force_path.write_text(original)

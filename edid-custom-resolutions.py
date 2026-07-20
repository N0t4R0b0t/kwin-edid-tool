#!/usr/bin/env python3
"""
Manually append custom resolutions to a real display's EDID via a new CTA-861
extension block, and apply the result through the kernel's debugfs
edid_override mechanism. See edid_lib.py for the underlying mechanism.

Usage:
  # Preview what would be built, don't touch anything:
  sudo ./edid-custom-resolutions.py --connector HDMI-A-1 --add 1024x600@60 --add 3840x2400@60 --dry-run

  # Build and apply:
  sudo ./edid-custom-resolutions.py --connector HDMI-A-1 --add 1024x600@60 --add 3840x2400@60

  # Undo (restore the display's real EDID):
  sudo ./edid-custom-resolutions.py --connector HDMI-A-1 --reset
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import edid_lib


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--connector", required=True, help="Connector name, e.g. HDMI-A-1 (must match both KWin's and DRM's naming)")
    parser.add_argument("--add", action="append", default=[], type=edid_lib.parse_resolution, metavar="WIDTHxHEIGHT@REFRESH", help="Custom resolution to add (repeatable)")
    parser.add_argument("--output", type=Path, help="Write the resulting EDID binary here instead of/in addition to applying it")
    parser.add_argument("--dry-run", action="store_true", help="Build and validate only - don't touch debugfs")
    parser.add_argument("--reset", action="store_true", help="Clear the EDID override and restore the real EDID")
    args = parser.parse_args()

    if args.reset:
        debugfs_path = edid_lib.find_debugfs_connector(args.connector) / "edid_override"
        debugfs_path.write_bytes(b"")
        edid_lib.reprobe(args.connector)
        print(f"Cleared EDID override for {args.connector}, restored real EDID.")
        return

    if not args.add:
        parser.error("--add is required unless --reset is given")

    sysfs_edid_path = edid_lib.find_sysfs_edid(args.connector)
    original = sysfs_edid_path.read_bytes()
    existing_extension_count = original[126]

    try:
        result = edid_lib.append_modes_to_edid(original, args.add)
    except edid_lib.ImpossibleTimingError as e:
        raise SystemExit(f"error: {e}")

    print(f"Original EDID: {len(original)} bytes ({existing_extension_count} existing extension block(s))")
    print(f"Appending 1 extension block with {len(args.add)} custom mode(s):")
    for width, height, refresh in args.add:
        print(f"  {width}x{height}@{refresh}")
    print(f"Result: {len(result)} bytes")

    if args.output:
        args.output.write_bytes(result)
        print(f"Wrote {args.output}")

    if args.dry_run:
        print("(--dry-run, not applying)")
        return

    debugfs_path = edid_lib.find_debugfs_connector(args.connector) / "edid_override"
    debugfs_path.write_bytes(result)
    edid_lib.reprobe(args.connector)
    print(f"Applied override to {debugfs_path}, reprobed {args.connector}.")
    print("Verify with: kscreen-doctor -o")


if __name__ == "__main__":
    if sys.platform != "linux":
        raise SystemExit("linux only")
    main()

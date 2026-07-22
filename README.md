# kwin-edid-tool

Add custom resolutions to a real display's EDID on Linux, so KWin (and the
underlying DRM/KMS driver) treats them as genuinely advertised modes instead
of runtime-synthesized ones.

## Why this exists

On Wayland/KWin, there's a protocol (`kde_mode_list_v2`, part of
`kde_output_management_v2`) for asking the compositor to synthesize a brand
new display mode at runtime. In practice, at least on NVIDIA's proprietary
DRM-KMS driver, this doesn't work: KWin accepts the request at the protocol
level, but the driver rejects the underlying atomic modeset test outright
(`Atomic modeset test failed! Invalid argument` in the kernel log) for any
timing that wasn't already present in the display's own EDID. Only modes the
display itself advertises get accepted reliably.

This tool sidesteps that entirely: instead of asking the compositor to
synthesize a mode at runtime, it patches the display's actual EDID (via the
kernel's `edid_override` debugfs mechanism) to genuinely include the
resolutions you want. From the driver's point of view, they're just regular
advertised modes - because they are.

Useful any time you need a real DRM output to offer a resolution its EDID
doesn't already list - dummy/headless display plugs, unusual client device
resolutions for remote-desktop or game-streaming setups, kiosk displays, etc.

## How it works

- Reads the display's current EDID (`/sys/class/drm/cardN-CONNECTOR/edid`)
- Computes VESA CVT (standard blanking, not reduced) timings for the
  requested resolution via the system `cvt` utility - no timing math
  reimplemented here. Standard blanking specifically because reduced
  blanking (`cvt -r`) only accepts refresh rates that are exact multiples of
  60Hz - confirmed live, a 30Hz request crashed the timing generation
- Appends a new CTA-861 extension block containing just the new mode(s) as
  Detailed Timing Descriptors - the original EDID (base block and any
  existing extensions) is left completely untouched, only the extension
  count and base-block checksum are patched
- Writes the result to `/sys/kernel/debug/dri/*/CONNECTOR/edid_override`
- Forces a reprobe by toggling the connector's DRM debugfs `force` file
  (through `on`, back to whatever it was) - this makes the kernel fire a
  hotplug notification directly, without ever asking the compositor to
  disable the output itself. An earlier version used
  `kscreen-doctor output.CONNECTOR.disable`/`.enable`, which hangs
  indefinitely if that connector happens to be your only currently-enabled
  output - confirmed live, KWin won't actually disable your sole active
  display.

Every generated EDID is fully spec-valid - verified against
[`edid-decode`](https://git.linuxtv.org/edid-decode.git) with zero errors or
warnings. The end-to-end mechanism (synthesize a timing, patch the EDID,
reprobe, select the new mode) has been verified live against real hardware:
a custom, non-standard resolution was genuinely accepted by the NVIDIA
driver and rendered.

## Usage

```bash
sudo ./edid-custom-resolutions.py --connector HDMI-A-1 \
  --add 1024x600@60 --add 3840x2400@60

# preview without touching anything:
sudo ./edid-custom-resolutions.py --connector HDMI-A-1 --add 1024x600@60 --dry-run

# undo:
sudo ./edid-custom-resolutions.py --connector HDMI-A-1 --reset
```

Bake in whatever resolutions you know you need, once, and be done with it.

## Integrations

- [Sunshine](integrations/sunshine/) - a connect-hook daemon that adds a
  Moonlight client's exact requested resolution automatically when it isn't
  already available.

## Requirements

- Linux with debugfs mounted (`CONFIG_DEBUG_FS`)
- Python 3.10+
- `cvt` (Arch: `libxcvt`)
- `kscreen-doctor` (KDE Plasma) for the reprobe/mode-select step
- Root - `edid_override` lives under `/sys/kernel/debug`, which is `700
  root:root` and not reachable via any capability short of
  `CAP_DAC_OVERRIDE` or actual root

## Security note

`/sys/kernel/debug` is root-only for good reason - writing to it is a
meaningfully privileged operation. If you're wiring this into another
service (see the Sunshine integration for an example), keep that privilege
scoped to a small, auditable helper rather than granting it to anything that
accepts untrusted network input directly.

## Known limitations

- Only tested against an NVIDIA + KWin (Plasma 6) setup, where it's
  confirmed working end-to-end with a real custom resolution. The EDID
  mechanism itself is driver/compositor-agnostic, but mode-selection
  currently shells out to `kscreen-doctor`, so it's KDE-specific for now.
- A resolution beyond what your GPU/output can physically drive will still
  fail cleanly rather than actually working - the underlying driver has the
  final say on whether it accepts a given timing, EDID or not. This is now
  a real, driver-level rejection rather than the compositor-level one this
  tool exists to route around.
- Only whole-number refresh rates are supported (a parsing choice, not a
  `cvt` limitation - arbitrary refresh rates work fine with standard
  blanking).
- One extension block holds up to 6 custom modes; requesting more than that
  in a single invocation will error out.

## License

MIT - see [LICENSE](LICENSE).

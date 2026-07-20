# sunshine-edid-helper

Add custom resolutions to a real display's EDID on Linux, so KWin (and the
underlying DRM/KMS driver) treats them as genuinely advertised modes instead
of runtime-synthesized ones.

## Why this exists

On Wayland/KWin, there's a protocol (`kde_mode_list_v2`, part of
`kde_output_management_v2`) for asking the compositor to synthesize a brand
new display mode at runtime - useful for things like
[Sunshine](https://github.com/LizardByte/Sunshine) trying to match a
streaming client's exact requested resolution.

In practice, at least on NVIDIA's proprietary DRM-KMS driver, this doesn't
work: KWin accepts the request at the protocol level, but the driver rejects
the underlying atomic modeset test outright (`Atomic modeset test failed!
Invalid argument` in the kernel log) for any timing that wasn't already
present in the display's own EDID. Only modes the display itself advertises
get accepted reliably.

This tool sidesteps that entirely: instead of asking the compositor to
synthesize a mode at runtime, it patches the display's actual EDID (via the
kernel's `edid_override` debugfs mechanism) to genuinely include the
resolutions you want. From the driver's point of view, they're just regular
advertised modes - because they are.

It was built for a dummy-plug display driving [Sunshine](https://github.com/LizardByte/Sunshine)
game-streaming sessions, but works for any real DRM output.

## How it works

- Reads the display's current EDID (`/sys/class/drm/cardN-CONNECTOR/edid`)
- Computes VESA CVT reduced-blanking timings for the requested resolution
  (via the system `cvt` utility - no timing math reimplemented here)
- Appends a new CTA-861 extension block containing just the new mode(s) as
  Detailed Timing Descriptors - the original EDID (base block and any
  existing extensions) is left completely untouched, only the extension
  count and base-block checksum are patched
- Writes the result to `/sys/kernel/debug/dri/*/CONNECTOR/edid_override`
- Forces a reprobe by disabling and re-enabling just that one output via
  `kscreen-doctor` (KDE Plasma/KWin only, currently)

Every generated EDID is fully spec-valid - verified against
[`edid-decode`](https://git.linuxtv.org/edid-decode.git) with zero errors or
warnings during development.

## Two ways to use it

### 1. Manual CLI tool

```bash
sudo ./edid-custom-resolutions.py --connector HDMI-A-1 \
  --add 1024x600@60 --add 3840x2400@60

# preview without touching anything:
sudo ./edid-custom-resolutions.py --connector HDMI-A-1 --add 1024x600@60 --dry-run

# undo:
sudo ./edid-custom-resolutions.py --connector HDMI-A-1 --reset
```

Bake in whatever resolutions you know you need, once, and be done with it.

### 2. Sunshine connect-hook daemon

A small root-owned daemon that listens on a Unix socket and, on each
Moonlight client connection, checks whether the client's exact requested
resolution is already available - if not, it adds it to the EDID
automatically.

```bash
sudo ./install.sh
```

Then in Sunshine's **General** settings, set **Do Command (On Client
Connect)** to:

```bash
sh -c "echo connect,${SUNSHINE_CLIENT_WIDTH},${SUNSHINE_CLIENT_HEIGHT},${SUNSHINE_CLIENT_FPS} | socat - UNIX-CONNECT:/run/sunshine-edid-helper.sock"
```

(or `nc -U` in place of `socat` if you have `openbsd-netcat` installed instead)

**Important timing note:** Sunshine picks the resolution for the *current*
connection before this hook fires, so the very first connection at a new
resolution streams at whatever Sunshine's own closest-match fallback picks.
This daemon just makes sure the exact resolution is genuinely available for
next time (a reconnect, or a later session) - it doesn't retroactively fix
the connection that triggered it. No disconnect/cleanup handling - modes
only ever get added, never removed automatically.

## Requirements

- Linux with debugfs mounted (`CONFIG_DEBUG_FS`)
- Python 3.10+
- `cvt` (Arch: `libxcvt`)
- `kscreen-doctor` (KDE Plasma) for the reprobe/mode-select step
- Root, for both the CLI tool and the daemon - `edid_override` lives under
  `/sys/kernel/debug`, which is `700 root:root` and not reachable via any
  capability short of `CAP_DAC_OVERRIDE` or actual root

## Security note

`/sys/kernel/debug` is root-only for good reason - writing to it is a
meaningfully privileged operation. Keep that privilege scoped to this small,
auditable daemon rather than granting it to anything that accepts untrusted
network input directly. That's exactly why this exists as a separate helper
rather than being built into Sunshine itself: Sunshine is a network-facing
daemon accepting connections from remote clients, and the resolution values
that would drive an EDID write come directly from that untrusted input. This
daemon only ever receives width/height/refresh triples over a local socket
and does exactly one privileged thing with them.

## Known limitations

- Only tested against an NVIDIA + KWin (Plasma 6) setup. The EDID mechanism
  itself is driver/compositor-agnostic, but the reprobe/mode-selection step
  currently shells out to `kscreen-doctor`, so it's KDE-specific for now.
- The daemon verifies that a mode selection was actually applied (by
  re-checking `kscreen-doctor`'s reported active mode afterward) rather than
  trusting its exit code, which doesn't reflect whether the driver accepted
  the modeset. A rejected selection is safe either way - the display stays
  on its previous working mode - but it does mean an "impossible" resolution
  (beyond what your GPU/output can physically drive) will fail cleanly
  rather than actually working.
- Only whole-number refresh rates are supported (a `cvt -r` requirement).
- One extension block holds up to 6 custom modes; requesting more than that
  in a single CLI invocation will error out (the daemon only ever adds one
  mode per connection, so this doesn't come up there).

## License

MIT - see [LICENSE](LICENSE).

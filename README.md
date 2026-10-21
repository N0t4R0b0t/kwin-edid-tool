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
  60Hz - confirmed live, a 30Hz request crashed the timing generation.
  The one exception: when standard blanking needs more than 600 MHz (the
  HDMI 2.0 limit) and the refresh is a multiple of 60Hz, reduced blanking is
  used instead. Confirmed live that the driver silently drops a 642 MHz
  3456x2160@60 mode, so it never reaches KWin; reduced blanking makes it
  482 MHz
- Appends a new CTA-861 extension block containing just the new mode(s) as
  Detailed Timing Descriptors - the original EDID (base block and any
  existing extensions) is left completely untouched, only the extension
  count and base-block checksum are patched
- Writes the result to `/sys/kernel/debug/dri/*/CONNECTOR/edid_override`
- Attempts a reprobe by toggling the connector's DRM debugfs `force` file
  (through `on`, back to whatever it was) - this makes the kernel fire a
  hotplug notification directly, without ever asking the compositor to
  disable the output itself. An earlier version used
  `kscreen-doctor output.CONNECTOR.disable`/`.enable`, which hangs
  indefinitely if that connector happens to be your only currently-enabled
  output - confirmed live, KWin won't actually disable your sole active
  display. **On NVIDIA, this reprobe attempt doesn't actually work** - see
  below.

Every generated EDID is fully spec-valid - verified against
[`edid-decode`](https://git.linuxtv.org/edid-decode.git) with zero errors or
warnings. The end-to-end mechanism (synthesize a timing, patch the EDID,
select the new mode) has been verified live against real hardware: a
custom, non-standard resolution was genuinely accepted by the NVIDIA driver
and rendered.

## Important: newly-added modes need a KWin restart to become selectable

This was discovered through extensive live debugging and is worth
understanding clearly rather than re-investigating later as a mystery bug.

**Adding** a resolution to the EDID always works live - the kernel-level
`edid_override` write succeeds every time, confirmed by the file's growing
byte count. But **discovering** it - KWin actually re-reading the connector
and adding it to its own advertised mode list - does not happen live on
NVIDIA, no matter how it's triggered. Four different mechanisms were tried
and confirmed ineffective on a real system:

1. The `force` debugfs toggle described above
2. Toggling a *different*, already-disabled output (hoping for a side effect)
3. KWin's own `org.kde.KWin.reconfigure()` D-Bus method
4. A genuine multi-output layout swap (disable/enable as part of a
   coordinated transition, not a bare single-output disable)

The only thing that worked was fully restarting the compositor
(`kwin_wayland --replace`) - which forces a real reinitialization of every
connector, at the cost of restarting your whole graphical session (every
app, this tool's own live connections, everything). It is not something to
trigger automatically or often.

**What this means in practice:** the daemon can add a resolution the moment
a client asks for something new, but it won't be genuinely selectable until
the next time KWin restarts. Once a resolution has been discovered this way,
it stays selectable for the rest of that session/boot, confirmed working
reliably across many reconnects.

### How the daemon works around it

An earlier version of this document told you to wait for "a reboot, a Plasma
update, or a deliberate `kwin_wayland --replace`". The reboot half of that
was wrong, and worth spelling out because it looks plausible: `edid_override`
lives in **debugfs**, so it does not survive a reboot at all, and the daemon
only ever wrote it in response to a client connecting - which is always
*after* the freshly-booted KWin has already scanned the connector. Every
reboot therefore threw the mode away and re-entered the same losing race.

The daemon now closes that loop with two pieces:

1. **Persistence.** Every synthesized mode is recorded in
   `/var/lib/sunshine-edid-helper/<connector>.json`, along with a cached copy
   of the connector's pristine EDID. The override is always rebuilt from that
   baseline plus the *full* accumulated set, which also fixes a subtler bug:
   rebuilding from live sysfs would either discard the previously added mode
   (when sysfs still shows the pristine EDID) or stack a new extension block
   on every request (once a connector detect has made sysfs reflect the
   override - see "The driver only re-reads the override at a connector
   detect" below).
2. **Boot seeding.** `sunshine-edid-seed.service` runs `daemon.py --seed`
   ordered `Before=display-manager.service`, re-applying the saved set while
   there is no compositor yet. KWin then starts up already advertising those
   modes, so no restart is involved and a reconnect gets the exact resolution
   immediately.

A genuinely new resolution - one never requested before on this machine -
still can't be discovered live. It is persisted straight away and becomes
selectable at the next boot, when seeding applies it before KWin starts.

The daemon can instead restart KWin to make it usable immediately, by setting
`SUNSHINE_EDID_HELPER_RESTART_KWIN=1` in the unit. This is **off by default**:
on Plasma 6, restarting `plasma-kwin_wayland.service` also stops
`plasma-workspace-wayland.target`, which ends the whole graphical session
(every open app, not just the stream), and systemd then refuses the start job
("Requested transaction contradicts existing jobs"), leaving you at the login
screen. Only enable it if you accept that, once per new resolution.

Note the cap: one CTA-861 extension block holds six detailed timings, so at
most six custom modes are kept, evicting least-recently-requested first.

## Troubleshooting a mode that does not show up

Hard-won notes from getting a `3456x2160@60` mode through on an NVIDIA HDMI
port. Every check below works without root unless marked.

### Check, in this order

1. **Is the override in the kernel?** (root) The size of
   `/sys/kernel/debug/dri/N/CONNECTOR/edid_override` is the connector's own
   EDID plus our one extension block. Monitors usually already carry an
   extension, so 256 + 128 = **384 bytes** is normal. Do not expect 256.
2. **Did the driver read it?** `stat`/`cat` the connector's sysfs
   `edid` (`/sys/class/drm/cardN-CONNECTOR/edid`). It is only the override once
   a connector detect has happened, so a stale copy right after a write is
   expected. Decode it with `edid-decode` and look for your timing as a
   `DTD` line in the last CTA-861 block.
3. **Did the driver keep the mode?** `grep WIDTHxHEIGHT
   /sys/class/drm/cardN-CONNECTOR/modes`. If the EDID has the DTD but this
   list does not, the driver rejected the timing (next section).
4. **Did the compositor see it?** `kscreen-doctor -j` and look at the
   output's `modes`. KWin only rescans a connector when it starts.

### A valid-looking mode can be silently dropped for its pixel clock

The override reached the kernel, sysfs showed the 384-byte EDID, `edid-decode`
was clean, and the mode still never appeared in the connector's mode list.
The cause was the pixel clock: standard CVT blanking for 3456x2160@60 needs
**642 MHz**, and the monitor's HDMI Forum block advertised
`Maximum TMDS Character Rate: 600 MHz` (its own 4K60 mode is 594 MHz). The
NVIDIA driver discards anything over that limit without a log message.

`cvt_timing()` now switches to reduced blanking (`cvt -r`) when the standard
clock exceeds `MAX_TMDS_CLOCK_MHZ` (600), which brings the same mode to
**481.75 MHz**. Reduced blanking only accepts multiples of 60Hz, so a request
at another refresh keeps standard blanking, and the daemon always builds at a
multiple of 60 anyway (`synthesis_refresh()`).

To check a mode by hand: compare the `DTD` pixel clock in
`edid-decode /sys/class/drm/cardN-CONNECTOR/edid` against the display's
`Maximum TMDS Character Rate` line. Modes that need more than that even with
reduced blanking are still written to the EDID and then dropped by the
driver; the tool does not warn about this yet.

### The driver only re-reads the override at a connector detect

Writing `edid_override` does not change what the driver exposes. It is picked
up the next time the connector is probed, and what triggers that matters:

| Action | Refreshes the kernel's EDID and mode list? |
| --- | --- |
| Writing `edid_override` | No, not by itself |
| Starting KWin (its first `drmModeGetConnector`) | Yes |
| `echo detect > /sys/class/drm/cardN-CONNECTOR/status` (root) | Yes, on demand |
| Toggling debugfs `force` (what `reprobe()` does) | Sends a hotplug event, but did not refresh the EDID |

Even after the kernel's list is correct, a running KWin does not rescan: with
`3456x2160` present in `.../modes`, `kscreen-doctor` still listed the old modes
until KWin was restarted. So the supported flow is unchanged: the mode is
persisted when first requested and appears after the next boot, when seeding
runs before KWin. Confirmed end to end on a reboot: the seed wrote the
saved mode, KWin listed `3456x2160` at 59.96Hz for the connector, and Sunshine
matched it exactly instead of falling back to the nearest mode.

An earlier statement here, that sysfs never reflects the override on NVIDIA,
was too strong. It does after a detect.

### Boot ordering

- **Do not order the helper after `graphical.target`.** That target only
  completes when the whole boot has finished, so anything slow or stuck holds
  the helper back. When `plymouth quit` timed out (see below),
  `plymouth-quit-wait` never finished, `graphical.target` stayed in
  `start waiting`, and the helper's start job (and a manual `systemctl start`)
  queued behind it forever. It is ordered `After=sysinit.target` for that
  reason; it only needs debugfs and its socket, and finds the desktop session
  when a client connects.
- **Known issue: `plymouth quit` times out on boots that run the seed unit.**
  Measured across three boots on the same machine (times are seconds since
  boot): without the seed unit `plymouth-quit.service` finished in about 4.3s
  (9.6 to 13.9). With the seed unit it timed out after 20s (about 9.4 to 29.3)
  both when the seed toggled `force` and after that toggle was removed, so the
  toggle is not the cause. The seed finishes just before `plymouth quit`
  starts in both cases, and `nvidia-drm` and `fbcon` come up at the same time
  as on the good boot. Effects: the display manager starts about 20s late, and
  `graphical.target` never becomes active ("Bootup is not yet finished"), which
  is why the helper must not be ordered after it. Everything else works,
  including the mode itself. The cause is not established. The suspect is the
  seed rewriting the connector's EDID while plymouth is still driving the
  display. Mitigation applied, **not yet confirmed on a boot**: the seed is now
  ordered `After=plymouth-quit.service`, still `Before=display-manager.service`,
  so the override is written once plymouth has gone and still well before KWin
  starts (it only starts after login). To confirm, check
  `systemctl status plymouth-quit` after a boot: it should show `Finished`
  within a few seconds, not `Result: timeout`, and
  `systemctl is-active graphical.target` should say `active`. If it still times
  out, the seed is not the cause and this note should be revisited.
  The seed also no longer toggles `force`: there is no compositor to notify at
  that point, so it was never needed.
- **Do not restart `plasma-kwin_wayland.service` to apply a mode.** It also
  stops `plasma-workspace-wayland.target`, which ends the whole graphical
  session, and the session does not come back (see above).

### Writing `force` by hand

`echo on > .../force` followed by `echo "$old" > .../force` fails on the
restore: `echo` appends a newline that the kernel rejects for `unspecified`,
which leaves the connector forced on. Use `printf` (no newline), and read the
value back afterwards. The daemon writes without a newline and was checked to
leave `unspecified` after a connect. To undo a stuck value:
`printf unspecified | sudo tee /sys/kernel/debug/dri/N/CONNECTOR/force`.

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
- A mode whose pixel clock exceeds the port's limit (600 MHz for HDMI 2.0),
  even with reduced blanking, is accepted into the EDID and then silently
  dropped by the driver. See "A valid-looking mode can be silently dropped".
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
- **Low target refresh rates used to cause visible compositor stutter during
  animations/effects, even though the mode "worked" for static content -
  the Sunshine daemon now avoids this automatically.** CVT-synthesized
  timings are always slightly imprecise versus their nominal refresh rate -
  confirmed via `edid-decode`, a requested 30Hz mode actually measured
  ~29.66Hz (~1.1% off), while a requested 60Hz mode measured ~59.85Hz
  (~0.25% off) - proportionally much tighter. A static desktop never exposes
  this mismatch (nothing changes frame to frame), but compositor animations
  (window slides, hover previews, notifications) are sensitive to consistent
  frame timing - a hover-preview effect at a synthesized 30Hz mode was
  reproducibly laggy while idle, then juddered visibly the instant an
  animation started. The [Sunshine daemon](integrations/sunshine/) now
  always builds new modes at the smallest clean multiple of 60 that's at
  least the requested refresh (`synthesis_refresh()` in `daemon.py`),
  regardless of what fps the client actually asked for - safe because
  Sunshine already encodes/streams at the client's requested fps
  independent of the display mode's own refresh rate, so a weak client
  isn't forced to render faster than it can. The standalone CLI tool
  (`edid-custom-resolutions.py`) still builds exactly what you ask it to,
  since that's a deliberate, explicit action - if you're adding modes
  manually, prefer an exact multiple of 60 yourself for the same reason.
  (This took a long, misleading debugging session to track down - GPU
  clock monitoring showed nothing conclusive since it was never a
  compute-bound issue, and KWin restarts appeared to help inconsistently
  because they were never the actual variable.)

## License

MIT - see [LICENSE](LICENSE).

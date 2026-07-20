# Sunshine integration

A root-owned daemon that listens on a Unix socket and, on each Moonlight
client connection, checks whether the client's exact requested resolution is
already available on the target display - if not, adds it to the EDID
automatically using the core tool at the repo root.

## Install

```bash
sudo ./install.sh
```

Or via the `kwin-edid-tool-git` AUR package, which installs the daemon and
a `sunshine-edid-helper.service` systemd unit automatically (still needs to
be enabled/started and wired up to Sunshine manually, see below).

## Configure Sunshine

Under Sunshine's **General** tab, find **Command Preparations** and add a new
entry:

- **Do**: `sh -c "echo connect,${SUNSHINE_CLIENT_WIDTH},${SUNSHINE_CLIENT_HEIGHT},${SUNSHINE_CLIENT_FPS} | socat - UNIX-CONNECT:/run/sunshine-edid-helper.sock || true"`
- **Undo**: leave blank

(or `nc -U` in place of `socat` if you have `openbsd-netcat` installed instead)

**The trailing `|| true` is required, not optional** - Sunshine aborts the
entire stream launch if any configured prep command exits non-zero. Without
it, this daemon being stopped/crashed for any reason would block every
connection, not just fail to enrich the EDID.

Command Preparations are read at Sunshine startup, not hot-reloaded - restart
`sunshine.service` after saving for it to take effect.

The connector the daemon manages defaults to `HDMI-A-1` - override via the
`SUNSHINE_EDID_HELPER_CONNECTOR` environment variable (set it in the
systemd unit, or export it before running `daemon.py` directly).

## Important timing note

Sunshine picks the resolution for the *current* connection before this hook
fires - display setup, resolution matching, and encoder probing
(`src/nvhttp.cpp` in Sunshine) all happen before `proc::proc.execute()`,
which is what triggers "Do Command" hooks. So the very first connection at a
brand-new resolution streams at Sunshine's own closest-match fallback; this
daemon just makes sure the exact resolution is genuinely available for next
time (a reconnect, or a later session). It doesn't retroactively fix the
connection that triggered it, and there's no code change to Sunshine
required to make this work - Sunshine's existing "select an already
advertised mode" logic just picks up the new mode automatically once it's
there.

## No disconnect handling

Modes only ever get added, never removed automatically. Run
`edid-custom-resolutions.py --reset` manually if you want to wipe the
accumulated custom modes and start over.

## Verification, not blind trust

The daemon re-checks `kscreen-doctor`'s reported active mode after selecting
one, rather than trusting its exit code (which doesn't reflect whether the
driver actually accepted the modeset). A rejected selection is safe either
way - the display stays on its previous working mode - but it does mean a
resolution beyond what your GPU/output can physically drive will fail
cleanly rather than actually working. Check `journalctl -u
sunshine-edid-helper` if a resolution isn't showing up.

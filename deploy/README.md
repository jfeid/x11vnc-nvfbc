# Deployed configuration

Copies of what is actually running on this machine, tracked so the deployment
is not knowledge that exists only on the filesystem.

> **Use these as an example, not as a recipe.** Several values belong to this
> machine and will be wrong on yours:
>
> | value | here | what it means |
> |---|---|---|
> | `/run/user/1000/gdm/Xauthority`, display `:1` | the logged-in user's session (uid 1000) | your user's uid and display |
> | `/run/user/110/gdm/Xauthority`, display `:0` | the GDM login screen (uid 110, `Debian-gdm`) | your display manager's uid and auth file, if you want the login screen reachable |
> | `-clip 2560x1440+0+0` | the left monitor (DP-4) of a 4480x1440 two-monitor screen | your monitor's geometry, or drop it to share the whole screen |
> | `-localhost` | only reachable through an SSH tunnel | keep it unless you have a reason not to |
> | `-rfbauth /etc/x11vnc.passwd` | password file, created with `x11vnc -storepasswd` | never commit it |

| file | installed at | owner |
|---|---|---|
| `x11vnc-wrapper.sh` | `/usr/local/bin/x11vnc-wrapper.sh` | `root:root 0755` |
| `x11vnc.service` | `/etc/systemd/system/x11vnc.service` | `root:root 0644` |

These are kept **byte-identical** to the installed copies on purpose — no added
header comments, no reformatting — so that a diff means drift and nothing else:

```bash
diff /usr/local/bin/x11vnc-wrapper.sh    deploy/x11vnc-wrapper.sh
diff /etc/systemd/system/x11vnc.service  deploy/x11vnc.service
```

Install a wrapper change (the `mv` matters: the kernel refuses to write to a
running executable, and doing it in two steps avoids a window where the file is
half-written):

```bash
sudo cp deploy/x11vnc-wrapper.sh /usr/local/bin/x11vnc-wrapper.sh.new
sudo chmod 755 /usr/local/bin/x11vnc-wrapper.sh.new
sudo mv -f /usr/local/bin/x11vnc-wrapper.sh.new /usr/local/bin/x11vnc-wrapper.sh
sudo systemctl restart x11vnc
```

Install a unit change:

```bash
sudo cp deploy/x11vnc.service /etc/systemd/system/x11vnc.service
sudo systemctl daemon-reload
sudo systemctl restart x11vnc
```

## The unit

```ini
[Unit]
Description=x11vnc remote desktop server
After=display-manager.service
[Service]
Type=simple
ExecStart=/usr/local/bin/x11vnc-wrapper.sh
Restart=always
RestartSec=3
```

`Restart=always` covers the wrapper being killed; the wrapper's own loop covers
x11vnc exiting while the wrapper survives (session end, display going away), so
neither layer alone is redundant.

`systemctl status x11vnc` reports the **wrapper's** pid as `MainPID`, not
x11vnc's — x11vnc is its child. To find the server process itself:

```bash
pgrep -af '^/usr/bin/x11vnc'
```

## What the wrapper does

It runs under `x11vnc.service` (`ExecStart=/usr/local/bin/x11vnc-wrapper.sh`,
`Restart=always`), and loops rather than exiting because the display it should
serve changes over the machine's life:

- **`:1` with `/run/user/1000/gdm/Xauthority`** — a user is logged in. Preferred.
- **`:0` with `/run/user/110/gdm/Xauthority`** — the gdm greeter, before login.
  uid 110 is gdm; that file does not exist while a session is active.
- neither reachable — sleep 2 and re-probe.

`xdpyinfo` is the reachability test, not just the file's existence, so a stale
`Xauthority` left behind by a dead session does not wedge it.

`DISPLAY` and `XAUTHORITY` are exported as well as passed as flags: NVFBC reads
the environment directly, unlike x11vnc which uses `-display`/`-auth`.

This is also why the service runs as **root** — it must serve the greeter on
`:0` (uid 110) and the user session on `:1` (uid 1000), and no single unprivileged
user can reach both.

## Why these flags

| flag | reason |
|---|---|
| `-clip 2560x1440+0+0` | serve only DP-4, not the whole 4480x1440 screen |
| `-localhost` | no direct exposure; reach it over an SSH tunnel |
| `-threads` | one thread per client |
| `-repeat -xkb` | keyboard behaviour; see `../keyboard-issues-and-future-work.md` |
| *(no `-wait`/`-defer`)* | deliberately unset. x11vnc auto-tunes them to `wait 10 / defer 10` when it measures framebuffer reads above 80 MB/sec, which measured better on both CPU and delivered frames than the `-wait 5 -defer 10` previously set here — see `../../bench/results/wait-defer.md` |
| `-nvfbc -nvfbc_nocursor -nvfbc_push` | NVIDIA capture instead of MIT-SHM — chosen for smoothness at a known CPU cost, see below |

### Why NVFBC is enabled

Re-enabled 2026-08-19. This is a deliberate trade, not a measurement win: the
benchmarks below still stand, and NVFBC costs more CPU per delivered frame than
MIT-SHM at every load. It is on because a few days of running on the shm path
felt sluggish in interactive use, and the extra frames are worth the CPU on this
machine.

| load | shm | NVFBC | |
|---|---|---|---|
| small 320x240 | 37.3 fps @ 30.2% | 50.9 @ 43.0% | +36% frames |
| medium 960x540 | 34.5 @ 35.2% | 42.1 @ 54.4% | +22% frames, 26% worse per frame |
| full 2560x1440 | 24.9 @ 45.3% | 27.2 @ 81.1% | +9% frames, 64% worse per frame |

NVFBC transfers the whole captured region over PCIe every frame regardless of
how much changed, then copies it twice more; shm reads only the changed tiles.
The efficiency gap therefore widens with change area. Measured latency is a wash
(58.9ms shm against 54.4ms NVFBC with push), so the perceived smoothness comes
from the higher frame rate, not from lower latency.

Full numbers: `../../bench/results/stock-comparison.md`.

**To switch back to MIT-SHM**, delete the three `-nvfbc*` lines from the
invocation. Do not instead append `-nonvfbc`: it works (later flags win) but
reads as self-contradictory and silently flips meaning if the flags are ever
reordered.

The shm path is still the fallback — NVFBC disables itself if the driver or
`libnvidia-fbc.so` is missing — and that fallback depends on the segment-handover
fix in `f3f28ad` when the service runs as root against a user-owned X server:
without it x11vnc aborts at startup with `X_ShmAttach BadAccess`, and `-noshm`
is far slower than either option here.

## Verifying what is actually running

The binary is replaced by rename, and a running process keeps executing the
inode it started with, so "what is installed" and "what is running" can differ
until a restart. Comparing the binary's mtime against the process start time
settles it:

```bash
stat -c %y /usr/bin/x11vnc
ps -o lstart= -p "$(pgrep -f '^/usr/bin/x11vnc' | head -1)"
```

A build is not identified by `x11vnc -version`, which only reports upstream's
0.9.17. Use the hash, or probe for a feature:

```bash
sha256sum /usr/bin/x11vnc
strings /usr/bin/x11vnc | grep -F 'MIT-SHM: handing segments'   # f3f28ad or later
```

## Not tracked here

- `/etc/x11vnc.passwd` — a credential. Never commit it.

# Deployed configuration

Copies of what is actually running on this machine, tracked so the deployment
is not knowledge that exists only on the filesystem.

| file | installed at | owner |
|---|---|---|
| `x11vnc-wrapper.sh` | `/usr/local/bin/x11vnc-wrapper.sh` | `root:root 0755` |

These are kept **byte-identical** to the installed copies on purpose — no added
header comments, no reformatting — so that a diff means drift and nothing else:

```bash
diff /usr/local/bin/x11vnc-wrapper.sh deploy/x11vnc-wrapper.sh
```

Install a change (the `mv` matters: the kernel refuses to write to a running
executable, and doing it in two steps avoids a window where the file is
half-written):

```bash
sudo cp deploy/x11vnc-wrapper.sh /usr/local/bin/x11vnc-wrapper.sh.new
sudo chmod 755 /usr/local/bin/x11vnc-wrapper.sh.new
sudo mv -f /usr/local/bin/x11vnc-wrapper.sh.new /usr/local/bin/x11vnc-wrapper.sh
sudo systemctl restart x11vnc
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
| `-nvfbc -nvfbc_nocursor` | NVFBC capture; cursor drawn by x11vnc instead |
| `-nvfbc_push` | ~10ms lower median latency, more frames at small/medium change areas — see `../../bench/results/push-direct.md` |
| `-clip 2560x1440+0+0` | serve only DP-4, not the whole 4480x1440 screen. Also selects NVFBC's capture region, which is the larger effect |
| `-localhost` | no direct exposure; reach it over an SSH tunnel |
| `-threads -wait 5 -defer 10` | upstream defaults for responsiveness |
| `-repeat -xkb` | keyboard behaviour; see `../keyboard-issues-and-future-work.md` |

`-nvfbc_direct` is deliberately **not** set: it matches `-nvfbc_push` on median
latency while adding a ~165ms tail.

## Not tracked here

- `/etc/systemd/system/x11vnc.service` — 12 lines, unchanged from what
  `../NVFBC-BUILD-AND-USAGE.md` documents.
- `/etc/x11vnc.passwd` — a credential. Never commit it.

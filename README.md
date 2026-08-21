# x11vnc-nvfbc benchmark harness

Reproducible before/after measurements for the NVFBC capture path.
Lives outside `x11vnc/` so the fork stays clean for upstream.

## Build

```bash
make                                  # or: make NVFBC_INC=/path/to/src/nvfbc
```

Needs `libX11` headers and `NvFBC.h` from the x11vnc fork (defaults to
`../x11vnc/src/nvfbc`). The Python tools need no build.

## The tools

| tool | purpose |
|---|---|
| `measure.py` | samples the running server: CPU, its NVFBC stats, delivered bytes. **Before/after CPU and grabs/frame.** |
| `compare.py` | diffs two `measure.py` result sets |
| `ab.sh` | runs two binaries head-to-head under an identical load and client. **The only sound way to compare delivered frames.** |
| `verify.sh` | end-to-end pixel/coordinate check of a build on a throwaway port |
| `rfbcheck.py` | minimal RFB client: verify pixels (Raw), or `--tight --compress N --quality N --stream` to drive the server's Tight encoder and report wire bytes plus a fill/palette/jpeg breakdown. `--h264` accepts encoding 50; `--fence` advertises RFB fence support and echoes ServerFence, so `--fence --slow N` imitates a paced consumer (plan §18) |
| `h264-testserver.sh` | launches a throwaway `-h264` server on a spare port (default 5906, X11 capture so it does not fight production's NVFBC session) to test the fence flow-control fix without touching 5900 |
| `loadgen` | deterministic X11 load: controllable area, rate and dirty fraction |
| `nvfloor` | this machine's NVFBC cost floor, independent of x11vnc |
| `diffcheck` | validates the NVFBC diff map against an independent per-tile memcmp |
| `pollmode` | compares sample/push and NOWAIT/timeout grab modes at a fixed poll rate |
| `latency.py` | damage-to-client latency: flips a rect and times how long it takes to reach a VNC client |
| `latency-ab.sh` | drives `latency.py` across capture configurations; `REVERSE=1` controls for sweep position |
| `waitdefer-ab.sh` | `-wait`/`-defer` variants, latency (`MODE=lat`) or throughput (`MODE=tput`) |
| `encoding-ab.sh` | Tight compression/quality A/B/A on a throwaway port with a local client. **The only sound way to measure encoder settings** - the live desktop and the transport to a real client both swamp the effect. |
| `root-ab.sh` | the shm-vs-NVFBC comparison that can only be done as root |
| `remote-check.sh` | exercises the NVFBC remote-control interface against a running server |
| `keytarget` | keypress-driven repaint window: the server-side target for `vncprobe.ps1` |
| `vncprobe.ps1` | Windows-side RFB probe: keypress -> first update latency over the real transport (tunnel/WLAN/VDSL) |
| `vncprobe.py` | same probe, run server-side over loopback: isolates the server half from the transport |
| `rfb-probe.py` | dumps the encoding list a client advertises, in its preference order. Answers "does this viewer support H.264, and under what number" without guessing |
| `h264serve.py` | serves a pre-encoded Annex-B file as RFB **encoding 50**. Reference implementation of the H.264 rect format, and the known-good stream to check an encoder against |

Recorded measurements and what each run was: `results/NOTES.md`.
Fork vs **stock** x11vnc: `results/stock-comparison.md`.
Push/direct flags: `results/push-direct.md`.
`-wait`/`-defer` under push: `results/wait-defer.md`.
Remote-control interface: `results/remote-control.md`.

`ab.sh` takes a command string per entry, so extra flags and builds without
NVFBC both work — it only passes `-nvfbc` to binaries that support it:

```bash
git -C ../x11vnc worktree add --detach /tmp/x11vnc-stock e2b726a
cd /tmp/x11vnc-stock && autoreconf -fiv && ./configure && make -j$(nproc)
cd - && GEOM=960x540+64+64 ./ab.sh \
    /tmp/x11vnc-stock/src/x11vnc \
    "/tmp/x11vnc-stock/src/x11vnc -noshm" \
    ../x11vnc/src/x11vnc
```

## Use

```bash
# before your changes (already recorded: results/baseline-20260813-210911.json)
./measure.py --label baseline

# after rebuilding and restarting x11vnc
./measure.py --label after

./compare.py baseline after
```

`measure.py` needs `DISPLAY`/`XAUTHORITY` pointing at the captured session:

```bash
DISPLAY=:1 XAUTHORITY=/run/user/1000/gdm/Xauthority ./measure.py --label after
```

Flags: `--duration N` (default 30 s per scenario), `--scenarios a,b,c`,
`--no-load` (draw nothing — passive only), `--no-floor` (skip the NVFBC probe),
`--blit` (video-like full-surface repaints instead of solid cells — the default
cell load never reaches libjpeg, so use this for anything JPEG-related).

Client-side Tight settings are recorded automatically in `context.client_encoding`,
read from the server log. They change encode cost and delivered bytes
substantially, so a result set without them is not interpretable.

## What it measures

`measure.py` samples the **already-running** x11vnc; it never restarts it.

| metric | source | meaning |
|---|---|---|
| `cpu_pct` | `/proc/PID/stat` utime+stime | % of one core |
| `new_fps` | server's own `NVFBC stats:` log lines | frames NVFBC reported as new **to us** - see the trap below |
| `grabs_per_sec` | same | `nvFBCToSysGrabFrame` calls issued |
| `grabs_per_frame` | derived | **headline ratio — should be ~1** |
| `cpu_ms_per_frame` | derived | CPU amortised per useful frame |
| `kb_per_sec` | `ss -tin bytes_sent` on :5900 | bytes actually delivered to clients |

`grabs_per_frame` is the cleanest pass/fail signal. Before the capture rework it
was 93–159, because every `copy_image()` call issued a fresh full-frame grab;
with one grab per scan cycle it now sits at 1.2–2.1. A number far above that
means the per-cycle caching has regressed.

### Scenarios

| name | load | note |
|---|---|---|
| `idle` | none | **uncontrolled** — reflects live desktop activity, context only |
| `small` | 320x240 @60fps | cursor/terminal-scale change |
| `medium` | 960x540 @60fps | video-window-scale change |
| `full` | 2560x1440 @60fps | fullscreen; covers the monitor for 30 s |
| `sparse` | 2560x1440, 2% of cells | scattered small changes across the screen |

`loadgen` uses server-side `XFillRectangle` on a fixed RNG seed, so the load
itself is cheap, deterministic, and produces real X damage. Its cell size
defaults to 32 px to line up with x11vnc's tile grid.

### Capture model

`nvfloor` measures this machine's NVFBC cost floor independently of x11vnc
(redundant-poll cost, full-screen grab cost, per-output grab cost). `measure.py`
folds those in to split measured CPU into estimated capture cost vs. everything
else (scan/compare/copy + libvncserver encoding), and projects where the two
headline fixes should land:

- `est_capture_pct` = `new_fps x full_grab + redundant_grabs x poll_cost`
- `proj_capture_pct` = `new_fps x output_tracked_grab` (one grab/frame, cropped)

It auto-detects that `-clip 2560x1440+0+0` is exactly output DP-4, so the
projection uses DP-4's measured grab cost.

## Trap: `new_fps` is not delivered frame rate

`new_fps` counts frames NVFBC reported as new **to us**, so it scales with how
often the server polls. A build that polls thousands of times per second
observes nearly every frame the display generates; a build that polls once per
scan cycle only observes the ones it consumes. Comparing the two reads as a
regression when the opposite is true.

Measured head-to-head on a 2560x1440 @60fps load, identical client, identical
CPU (80%):

| build | `new_fps` | grabs/s | **updates/s delivered** |
|---|---|---|---|
| pre-rework | 58.9 | 2669 | **20.1** |
| post-rework | 51.9 | 54 | **26.7** |

Lower `new_fps`, 33% more frames actually delivered.

**To compare delivered frames, use `ab.sh`**, which runs both binaries on
throwaway ports under the same load and the same client and reports
`updates/s`. Raw encoding is used deliberately so delivered bytes are
proportional to the area each build marks modified:

```bash
GEOM=2560x1440+0+0 ./ab.sh /usr/bin/x11vnc.bak-20260813 ../x11vnc/src/x11vnc
```

(every positional argument is a binary-plus-flags command string; the load
geometry comes from `GEOM`, and `BLIT=1` switches to video-like repaints)

Use `measure.py` for CPU and `grabs_per_frame`; use `ab.sh` for frame delivery.

## Hazard: test servers and X server auto-repeat

x11vnc **disables the X server's global key auto-repeat** while a client is
connected, unless `-repeat` is passed — `no_autorepeat` defaults to on. It
restores the setting via `cleanup.c` on a normal exit, which `kill -9` skips.

Every script here starts throwaway servers and stops them with `kill -9`, so
without `-repeat` a benchmark run leaves the *user's* desktop with auto-repeat
switched off: holding a key stops repeating, and nothing in the live service's
own log explains why. This happened once during development and took a bug
report to notice.

All harness scripts now pass `-repeat`, which makes both disable paths
(`connections.c` on first client, `check_autorepeat()` in `xevents.c`) no-ops.
If you add a new script that starts x11vnc, pass it too.

To check and repair by hand:

```bash
xset q | grep 'auto repeat:'
xset r on
```

## End-to-end keypress latency (client side)

`latency.py` measures the server half (damage -> client, same machine).
For the full round trip a remote user feels — key event through the transport,
app repaint, capture, encode, back through the transport — the probe sits on
the **client** machine:

- `keytarget` (server): override-redirect window that raises itself and
  repaints whenever the probe key arrives. The repaint is the provable response
  to the injected key. It uses `select()` on the X connection — a polling loop
  here would add tens of ms and falsify the measurement. By default it grabs
  **only the probe key** (`-k`, default Page Down `0xFF56`) on the root window
  and never takes focus, so the desktop stays usable while a run is going.
- `vncprobe.ps1` (Windows): minimal RFB 3.8 client; sends a key event and
  times the first FramebufferUpdate carrying rects, scoped to the keytarget
  rect so unrelated desktop changes can't count.

```bash
# server side
./keytarget -g 400x300+64+64 -d 300 &
../x11vnc/src/x11vnc -display :1 -auth /run/user/1000/gdm/Xauthority \
    -forever -shared -nopw -localhost -rfbport 5918 -repeat -noipv6 \
    -clip 2560x1440+0+0 -threads -nonap -nocursor
# client side (after: ssh -L 5918:127.0.0.1:5918 x11vnc — the ssh config
# entry already forwards 5900; the extra -L adds 5918 for this server)
.\vncprobe.ps1 -Port 5918 -Rect 64,64,400,300 -Trials 10
```

Gotchas found while validating this (all encoded in the tools already):

- Keep IPv6 out of the path — this is the one that looks like "the tunnel is
  down" and isn't. **Two different sockets can end up serving `::1`, and only
  one of them answers.** Whichever loses the startup bind race is visible in
  the server's own log:

  | log line at startup | owner of `::1` | behaviour |
  |---|---|---|
  | `Listening for VNC connections on TCP6 port N` … `Not listening on IPv6 interface.` | libvncserver | accepted by its listener thread, fine |
  | `rfbListenOnTCP6Port: error in bind IPv6 socket: Address already in use` … `Listening also on IPv6 port N (socket 9)` | x11vnc | **hangs**, see below |

  When x11vnc owns the socket it is `accept()`ed only by `check_ipv6_listen()`
  (`connections.c:1746`), whose single call site is `rfbPE()` (`util.c:598`).
  Under `-threads`, `watch_loop()` never reaches `rfbPE()` and every other
  caller is client- or input-driven, so while no client is attached nothing
  ever accepts. The kernel completes the handshake regardless, so the client's
  `connect()` succeeds and the banner simply never comes — measured: **220 s,
  no banner**; in another run it arrived 3.7 s after connect, the instant an
  unrelated IPv4 client attached and put `rfbPE()` back in play.

  The bench throwaway lands in the second row every time; the live 5900
  service happens to land in the first, which is why its `LocalForward 5900
  localhost:5900` works despite resolving to `::1`. What decides the race is
  not established — ruled out: `-nopw` vs `-rfbauth`, `-nonap`, `-nocursor`,
  `-xkb`, `-wait`/`-defer`/`-nowait_bog`, `-extra_fbur`, libvncserver version,
  binary version. Untested: the live service runs as **root**, every test here
  ran as the user. Don't depend on landing in the good row: use
  `-L 5918:127.0.0.1:5918` and pass `-noipv6`; either alone is enough.
- `-repeat` on the throwaway server: without it x11vnc switches off the X
  server's global auto-repeat for the user's live session (see hazard above).
- Don't use `-graball` on a machine someone is sitting at. It calls
  `XGrabKeyboard`, which GNOME here **grants** — every keystroke then goes to
  keytarget and the desktop cannot be typed into at all until it exits, with
  Escape the only interactive way out. Escape exits *before* the counter
  increments, so an aborted run is recognisable by `keytarget: 0 keypresses`
  with FAILs on every trial. The default single-key grab has none of this:
  only the probe key is intercepted, focus is left alone, and `-k` must match
  the probe's `-Keysym`.
- The window must be **visible**: a covered window repaints into nothing the
  capture path can see. keytarget is override-redirect and raises itself per
  keypress because mutter ignores raises on managed windows.
- Adaptive pacing bites: on a fast capture path x11vnc resets `-wait`/`-defer`
  to 10/10 unless given explicitly, so pass `-wait 2 -defer 2` if you want the
  pipeline floor rather than the production cadence.

Local reference (this machine, adaptive 10/10 pacing, keytarget flow):
median ~55 ms, min ~20 ms — i.e. ~2 delivery periods. Transport adds its RTT
on top; measure that with a TCP handshake to the ssh host.

Re-measured 2026-08-16 with `vncprobe.py` over loopback, same pacing:
`n=10 min=16.2 median=43.6 avg=52.8 max=140.9 ms`. The tail is real, not
noise — single trials land anywhere from one delivery period to ~140 ms, so
quote the median and report n.

## Noise floor

Two back-to-back baseline runs agreed within **±3–8%** on the loaded scenarios.
Treat anything under ~8% there as noise. `idle` is far noisier (grabs/s varied
±50%) because it captures whatever the desktop happens to be doing — do not
draw conclusions from it.

Keep these constant between runs or the comparison is invalid:

- **connected client count** (`compare.py` warns if it differs) — encoding CPU
  scales with it
- x11vnc command line (`compare.py` prints a diff)
- screen layout and resolution

## Caveats

- `cpu_pct` includes libvncserver encoding, which the capture fixes do not
  touch. Use `est_capture_pct` to see the capture path in isolation.
- `new_fps`/`grabs_per_sec` come from the server's own 10 s log intervals. A
  leading interval that began before the scenario is discarded; with 30 s
  windows that leaves ~2 usable intervals per scenario.
- `nvfloor` opens its own NVFBC capture session alongside the running server.
  It has been harmless in practice; `--no-floor` skips it.

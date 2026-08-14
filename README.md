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
| `rfbcheck.py` | minimal RFB client: fetch a rect and verify pixels, or stream and count updates |
| `loadgen` | deterministic X11 load: controllable area, rate and dirty fraction |
| `nvfloor` | this machine's NVFBC cost floor, independent of x11vnc |
| `diffcheck` | validates the NVFBC diff map against an independent per-tile memcmp |
| `pollmode` | compares sample/push and NOWAIT/timeout grab modes at a fixed poll rate |

Recorded measurements and what each run was: `results/NOTES.md`.
Fork vs **stock** x11vnc: `results/stock-comparison.md`.

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
`--no-load` (draw nothing — passive only), `--no-floor` (skip the NVFBC probe).

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

`grabs_per_frame` is the cleanest pass/fail signal. Today it is 93–159 because
every `copy_image()` call issues a fresh full-frame grab; one grab per scan
cycle should collapse it to ~1.

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
./ab.sh /usr/bin/x11vnc.bak-20260813 ../x11vnc/src/x11vnc "2560x1440+0+0"
```

Use `measure.py` for CPU and `grabs_per_frame`; use `ab.sh` for frame delivery.

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

# Recorded runs

> Comparing the fork against **stock x11vnc** (not just against its own earlier
> self) is in [stock-comparison.md](stock-comparison.md). Short version: with
> MIT-SHM working (the cross-uid attach blocker was fixed in `f3f28ad`), the
> shm path is more CPU-efficient per frame than NVFBC at equal or slightly
> lower frame rate, and the service was switched back to it on 2026-08-15.
> NVFBC remains in the tree for the cases where it wins (no shm available,
> XDamage-broken environments, fullscreen video). Read that before quoting any
> of the numbers below as "NVFBC is faster".
>
> **Superseded 2026-08-19:** the deployed service is on NVFBC again, now with
> `-nvfbc_push` and no `-wait`/`-defer`:
> `-nvfbc -nvfbc_nocursor -nvfbc_push -repeat -threads -clip 2560x1440+0+0 -xkb`.
> Why it was switched back is not recorded here.

> **Paths rewritten 2026-09-29:** the server command lines embedded in the
> JSON files originally held the absolute path of the local checkout. That
> prefix was replaced with `$REPO/` before publishing; nothing else in the
> records was changed. They describe the old layout of separate checkouts
> (`$REPO/x11vnc`, `$REPO/bench`), which became the repo root and
> `nvfbc-docs/bench/`.

All on the same machine: RTX 3060, driver 550.163.01, X screen 4480x1440 across
two outputs (DP-2 1920x1200+2560+0, DP-4 2560x1440+0+0), server run with
`-clip 2560x1440+0+0 -nvfbc -nvfbc_nocursor -threads -wait 5 -defer 10`, one
VNC client attached.

| file | binary | what it is |
|---|---|---|
| `baseline-20260813-204857.json` | `74b97bf2` | first baseline pass. Predates the capture-cost model, so it has no `est_capture_pct`. Kept only because diffing it against the next run shows the harness noise floor (±3-8% on loaded scenarios). |
| `baseline-20260813-210911.json` | `74b97bf2` | **the canonical before.** Full model. Binary identity was backfilled after the run - same pid throughout, binary unmodified since 2026-06-03 - and the file records a `note` saying so. |
| `after-20260813-223705.json` | `8d60c898` | first deployed rework. Superseded: `copy_screen()` unconditionally dropped the cached frame, costing a redundant full-frame DMA (~3ms) on every whole-screen update. |
| `after2-20260813-233607.json` | `6bace55e` | **the canonical after.** `copy_screen()` reuses the cycle's frame when called inside one, plus `nvfbcMutex` for `-threads` safety. |

## Headline

`baseline-...-210911` -> `after2-...-233607`:

| scenario | CPU | grabs/frame |
|---|---|---|
| idle | 26.7% -> 16.2% | 151.7 -> 1.9 |
| small | 60.6% -> 35.4% | 127.6 -> 2.1 |
| medium | 78.2% -> 52.2% | 158.8 -> 1.7 |
| full | 90.9% -> 89.7% (saturated) | 93.3 -> 1.2 |

## Do not read new_fps as delivered frame rate

`full` looks like a frame-rate regression in these files and is not. `new_fps`
counts frames NVFBC reported as new *to us*, which scales with poll frequency.
Measured head-to-head instead, identical client and load, both at 80% CPU:

| build | new_fps | grabs/s | updates/s delivered |
|---|---|---|---|
| `74b97bf2` (before) | 58.9 | 2669 | **20.1** |
| `6bace55e` (after) | 51.9 | 54 | **26.7** |

33% more frames actually delivered. See `../README.md` and use `ab.sh` for any
delivered-frame comparison.

## Keypress latency vs -wait/-defer pacing (2026-08-17)

Keytarget flow (`keytarget` + `vncprobe.py` over loopback), 30 trials per run,
A/B/A so baseline drift is visible rather than assumed. Server:
`-nopw -localhost -rfbport 5918 -repeat -noipv6 -clip 2560x1440+0+0 -threads
-nonap -nocursor`, one keytarget instance shared by all three runs.

| pacing | n | min | median | avg | max |
|---|---|---|---|---|---|
| `-wait 10 -defer 10` (A1) | 29 | 11.8 | 49.2 | 48.6 | 184.2 |
| `-wait 2 -defer 2` (B) | 30 | 12.5 | **36.3** | **34.4** | **67.3** |
| `-wait 10 -defer 10` (A2) | 30 | 12.1 | 52.3 | 52.5 | 123.6 |

Baseline reproduced within 3.1 ms (6%) either side of B, inside the ±3-8%
noise floor, so the B delta is real.

**The min does not move: ~12 ms either way.** That is the pipeline floor -
repaint, damage, capture, encode, deliver - and pacing has no effect on it.
What pacing changes is how long a request waits for the next delivery window:
median -14 ms and, more usefully, max 184/124 -> 67 ms. Tightening the tail is
the bigger win; the median is not where users notice this.

Even at 2/2 the median sits ~24 ms above the floor, so `-wait`/`-defer` is not
the only scheduling cost in the path - don't expect 2/2 to buy the floor.

Not measured here: CPU cost of 2 ms polling. Use `ab.sh`/`waitdefer-ab.sh`
before treating this as a recommendation for the production service.

End-to-end context: the same client (Windows, ssh tunnel over WLAN+VDSL) at
10/10 measured `n=10 min=49.5 median=84.8 max=277.6 ms`, of which ~41 ms was
transport (kernel `rtt:35.699/13.971` on that socket). At 2/2 the server half
would put the expected median near ~77 ms, with network loss still owning the
tail: that run's 277.6 ms outlier matches the socket's `rto:236` almost exactly.

### CPU cost of that pacing change (2026-08-17)

`MODE=tput waitdefer-ab.sh`, NVFBC push path, 960x540@60fps loadgen:

| variant | CPU | updates/s | MB/s | CPU per update/s |
|---|---|---|---|---|
| `-wait 5 -defer 10` | 47.0% | 50.8 | 98.2 | 0.93 |
| `-wait 1 -defer 1` | 56.1% | 59.7 | 117.8 | 0.94 |
| `-wait 10 -defer 10` | 45.8% | 54.9 | 109.1 | 0.83 |
| `-wait 20 -defer 20` | 31.1% | 34.7 | 69.1 | 0.90 |

Efficiency is flat there: under a real video load the knob buys work rather
than wasting it. That is **not** the regime the production service lives in,
so it does not answer whether to run 2/2. Measured that separately on the shm
path with a client attached, 15 s per run, baseline repeated for drift:

| pacing | load | CPU | updates/s | CPU per update/s |
|---|---|---|---|---|
| `-wait 10 -defer 10` | idle | 13.2% / 13.4% | 13.7 / 12.6 | ~1.00 |
| `-wait 2 -defer 2` | idle | **28.2%** | 15.9 | 1.77 |
| `-wait 10 -defer 10` | light (400x300@10fps) | 16.4% / 17.1% | 20.7 / 22.6 | ~0.77 |
| `-wait 2 -defer 2` | light | **25.5%** | 23.0 | 1.11 |

**Idle CPU more than doubles** (13.3% -> 28.2%) for ~20% more updates/s, and
per-update efficiency drops 77%. Under light load it is +52% CPU for +5%
updates. At idle the extra cost is pure polling waste - 500 damage checks a
second on a desktop that is not changing - which is exactly where a desktop
service spends most of its life.

Verdict: the 14 ms median and 184->67 ms tail from 2/2 are real, but paid for
with a doubling of idle CPU on the box the user is also working on. Not
recommended as a blanket production setting on this evidence. The unmeasured
middle (5/5) is the obvious next candidate.


## Client Tight settings: compression and JPEG quality (2026-08-19)

The *client* picks compression level and JPEG quality in SetEncodings and the
server obeys. They drive encode cost and delivered bytes directly, and until
this date no result set recorded them. `measure.py` now captures them from the
server log into `context.client_encoding`, so a run can no longer be ambiguous
about which settings produced it.

### Three files here are invalid - do not quote them

| file | why |
|---|---|
| `tight-c2q8-20260819-180005.json` | cell load. `loadgen`'s default `XFillRectangle` cells encode as Tight fills/palettes and never reach libjpeg, so a JPEG quality change moves nothing. The ~400:1 compression ratio is the tell. Needs `--blit`. |
| `blit-c2q8-20260819-181000.json` | blit load, but measured against the live service: the user's desktop was in use, and the only client was a real viewer behind an SSH tunnel and a VDSL link. Both uncontrolled, both larger effects than the setting. |
| `blit-c6q9-20260819-182459.json` | same. Produced 6/9 using **23% less** CPU than 2/8, which an encoder cannot do. Explained by the link capping delivery: fewer bytes out, so less CPU spent producing them. |

### The controlled result

`encoding-ab.sh`: private x11vnc on a throwaway port, one local
`rfbcheck.py --tight` client, load we generate, A/B/A ordering.

| leg | cpu% | MB/s | rects/s | jpeg% |
|---|---|---|---|---|
| 2:8 (A1) | 57.9 | 12.09 | 514.6 | 80.1 |
| 6:9 (B) | 61.6 | 20.44 | 352.3 | 92.8 |
| 2:8 (A2) | 55.9 | 12.26 | 460.0 | 84.3 |

The A legs reproduce within 3.5% CPU and 1.4% bytes - inside the +/-3-8% noise
floor - so the B delta is attributable.

**compression 6 / quality 9 against 2 / 8: +8.3% CPU, +67.9% bytes.**

The CPU effect is marginal, barely clear of noise. The bandwidth effect is
large. Going to 2/8 cuts wire bytes by 40% for an ~8% CPU saving. This server is
reached over an 82/9.9 Mbps VDSL link, where fullscreen Tight already wants
~65 Mbps, so 40% fewer bytes is the difference between saturating the
downstream and having room in it. **Rank these settings by bandwidth, not CPU** -
an earlier version of this analysis had it backwards.

`jpeg%` shows the mechanism: at quality 9 the encoder sends 92.8% of rects as
JPEG rather than palette, up from 80.1%, and each costs more bytes.

### How to measure them

Do not use `measure.py` against the running service for encoder questions. The
live desktop and the transport to the real client each move server CPU by more
than the setting under test. `encoding-ab.sh` removes both.

`rfbcheck.py --tight --stream` prints a sub-encoding breakdown
(`fill`/`palette`/`jpeg`/`copy`/`gradient`). Check it before trusting any
JPEG-related number: if `jpeg%` is low, the load is not exercising the path
under test, and that is how the first attempt above went wrong.


### JPEG quality sweep (2026-08-19)

`./encoding-ab.sh 2:6 2:7 2:8 2:9 2:6`, compression pinned at 2, first leg
repeated last as a drift bracket. `jpeg%` 98-99.5 throughout, so this is the
JPEG path and nothing else.

| quality | cpu% | MB/s | vs q8 | updates/s |
|---|---|---|---|---|
| 6 (A1) | 59.4 | 8.72 | | 41.5 |
| 7 | 61.1 | 9.96 | -14.5% | 39.5 |
| 8 | 61.7 | 11.65 | - | 39.1 |
| 9 | 65.5 | 19.08 | +63.8% | 35.1 |
| 6 (A2) | 59.9 | 8.49 | -26.1% | 40.6 |

The 2:6 legs reproduce within 0.8% CPU and 2.7% bytes, so the sweep holds.

**Quality 9 is a cliff, not a step.** 6->7->8 each cost ~16% more bytes; 8->9
costs 64%. That is libjpeg's top-of-scale behaviour - the last increment is
priced absurdly. Never run quality 9 on a bandwidth-constrained link.

CPU moves 59.4 -> 65.5 across the whole sweep, most steps inside noise. These
settings are a bandwidth knob, not a CPU knob.

Caveat on picking a floor: `loadgen -blit` draws a synthetic gradient. It
exercises libjpeg correctly, so the byte figures are sound, but it says nothing
about what low quality does to antialiased text - the content most sensitive to
JPEG quantisation. The numbers argue for going lower; the floor is a subjective
call against a real desktop.


### Tight JPEG chroma subsampling by quality level (2026-08-20)

Dumped real Tight JPEG payloads with `rfbcheck.py --dump-jpeg` and read their
sampling factors. This is invisible from the protocol and changes the meaning of
the quality knob:

| quality | sampling-factor | |
|---|---|---|
| 3, 5 | `2x1,1x1,1x1` | 4:2:2 - half chroma horizontally |
| 6, 7, 8, 9 | `1x1,1x1,1x1` | **4:4:4 - no chroma loss** |

**Do not set quality below 6.** There is a chroma cliff between 5 and 6 that the
byte-rate curve alone does not reveal - levels 6-9 differ only in quantisation,
level 5 and below also throw away half the chroma resolution. The earlier
recommendation to try quality 7, or 6, stands; 5 does not.

It also means the Tight path this server currently serves is **full-chroma**,
which is the baseline any H.264 comparison has to be made against. See
`nvfbc-docs/NVENC-H264-PLAN.md` §10.

## H.264 tiling: the freeze fix and its measurements (2026-08-22)

Context: `nvfbc-docs/NVENC-H264-PLAN.md` §22-§25. The viewer "freeze" under sustained
motion was never a freeze - TigerVNC silently refuses to display any H.264 rect
above ~2.36 Mpx, so the full-screen 2560x1440 rect never painted at all and what
was on screen was the last Tight frame. Fixed by splitting the served region
into two 2560x720 tiles.

| file | what it was |
|---|---|
| `fence-5906-*`, `fence-nvfbc-*`, `fence-rerun-*` | RFB fence flow control on a throwaway port, before the root cause was known. The fence work is real and kept, but it did **not** fix the freeze. |
| `fps5-test-*` | `-h264_fps 5` - the client-saturation theory, refuted: it froze identically. |
| `logrun-*`, `logcapture-*` | freezes reproduced deliberately while the Windows client wrote `C:\temp\vncviewer.log`. The log was silent, which is itself the result: the H.264 path has no logger at all. |
| `idrfix-*` | after `forced-idr` and VBR, still single-rect - still froze. |
| `tiled-*` | first tiled build, on the throwaway port. Played through both gate entries. |
| **`tiled-prod-20260822-005642.json`** | **the reference run.** Tiled build deployed to 5900, full bench, operator watching. `medium` 1559.8 -> 176.7 KB/s (-89%), `full` 1236.8 -> 371.5 KB/s (-70%), `full` CPU 85.2% -> 130.3%. This is the baseline Phase 3' has to beat. |
| `cqsweep_{0,15,19,23,27}-*` | `-h264_cq` swept live on 5900 under a full-screen blit load. cq scales bandwidth 21.8 -> 4.6 Mbps; CPU flat at 51-53% throughout, i.e. quality costs the GPU, not the CPU. It does **not** improve text - see §25. |

Two traps these runs exposed, both now fixed in the harness:

- `measure.py` took the first `pgrep x11vnc` hit and sampled `:5900` regardless.
  With a test server running alongside the live one it silently measured the
  wrong process. Use `--port`.
- A bench against a server without `-nvfbc` cannot trip the H.264 gate at all:
  the scan rate is too low, damage coalesces, and the run measures pure Tight
  while looking perfectly healthy. Use `NVFBC=1 ./h264-testserver.sh`.


## Phase 3': encode from the NVFBC buffer (2026-08-22)

| file | binary | what it is |
|---|---|---|
| `phase3-prod-20260822-160525.json` | `677242268677d06c` | the deployed Phase 3' build against the real TigerVNC client. `full` 130.3% -> 118.8%, captured 27.4 -> 35.9 fps. Compare against `tiled-prod-20260822-005642.json`, but note the two runs are 15 hours apart with different desktop content - the same-rig A/B below is the sound comparison. |
| `ab-base-20260822-160907.json` | `cd2e5c18` | pre-change `c5c7ddc`, throwaway port 5910, synthetic H.264 consumer, full-screen load. |
| `ab-phase3-20260822-160956.json` | `be27c29e` | same rig, same minute, Phase 3'. **113.3% -> 104.9% at 33.7 -> 41.6 captured fps: -24% CPU per captured frame.** |

Both A/B legs ran alongside the live server on 5900, which competed equally.
`measure.py --no-load` samples passively; the load was a separate 2560x1440
`loadgen` running for the whole leg, so the "idle" scenario label in those two
files means "the sampling window", not an idle screen.

**The copies are gone, counted not assumed.** The `h264 stats:` line now ends
with `N fb-skips/s` - scan cycles that skipped filling main_fb - and it equals
the NVFBC grabs/sec exactly (47 = 47).

**What is left is not ours.** Per-thread split under full load
(`/proc/PID/task/*` deltas; `perf_event_paranoid` is 3 so no profiler):
two `cuda-EvtHandlr` threads at ~35% each, x11vnc's own watch_loop at 30.9%.
They are NVENC's, one per tile encoder, and they sit in a `poll()` loop on the
NVIDIA fd - 42,000 wakeups/s each, 78% system time - waiting for the GPU.

**That cost tracks GPU wait time, not frames, and the `full` number is inflated
by the benchmark itself:**

| load | GPU | cuda-EvtHandlr each | delivered |
|---|---|---|---|
| encoders open, nothing submitted | 8% | 0.3% | 0 fps |
| 640x480@60 | 35% | 1.0% | 17.3 fps |
| full screen | 76% | 35% | 15.7 fps |

The middle row delivers *more* frames for 1%. Under `full` the load generator is
itself repainting 2560x1440 at 28 fps, so about half the GPU load is the bench.
See plan §26; `nvidia-smi --query-gpu=utilization.encoder` reads 0% on this
driver even while encoding, so do not use it.


## Shared CUDA context, on production (2026-08-22)

`sharedctx-prod-20260822-165741.json`, binary `4445df61`, same four-scenario
`measure.py` invocation as the two runs above, same real TigerVNC client:

| scenario | before Phase 3' | Phase 3' | + shared context |
|---|---|---|---|
| idle | 3.4% | 10.1% | 11.7% |
| small | 34.8% | 30.4% | 34.3% |
| medium | 42.3% | 33.3% | 33.0% |
| **full** | **130.3%** | **118.8%** | **72.8%** |

`full` in detail:

| | before | Phase 3' | + shared |
|---|---|---|---|
| CPU | 130.3% | 118.8% | **72.8%** |
| captured fps | 27.4 | 35.9 | 32.5 |
| CPU per captured frame | 47.86 ms | 33.15 ms | **22.49 ms** |
| wire | 371.5 KB/s | 400.8 KB/s | 386.8 KB/s |

**-44% CPU and -53% CPU per captured frame against the pre-Phase-3' baseline**,
with the wire unchanged. The gate held one continuous H.264 period across the
whole `full` window (16:56:42 -> 16:57:43), 0 fence timeouts, every frame
encoded direct from the NVFBC buffer.

Two things not to read too much into:

- **`idle` is uncontrolled and went up.** It draws nothing, so it measures
  whatever the operator's desktop happened to be doing; `kb_per_sec` was 25.3,
  12.5 and 70.4 across the three runs, which is the tell. Do not compare it.
- **`loadgen_achieved_fps` rose** (28.2 / 28.0 / 32.4). The server leaving more
  CPU and GPU headroom lets the load generator itself run faster, so the last
  run was working slightly harder for its 72.8%.

`avcodec_open2` is now 30-31 ms per tile against 92-142 ms before, because
creating the CUDA context is no longer part of it - the gate-entry hitch is
mostly gone as a side effect.


## AM5 platform, same binary (2026-09-29)

Same deployed binary as `sharedctx-prod` (`4445df61`, git `9aea6be`), same
service cmdline, same default four-scenario `measure.py`, same real TigerVNC
client (ZRLE c2 q8, H.264 on). Platform changed: i7-3770 / Z77 / DDR3-1333 ->
Ryzen 9 9900X / B850 / DDR5-6000. GPU and driver unchanged (RTX 3060,
550.163.01). Kernel moved 6.12.57 -> 6.12.107 in the meantime, so this is not a
pure hardware A/B.

| file | what it is |
|---|---|
| `am5-sharedctx-prod-rerun-20260929-193208.json` | **the AM5 baseline.** Four scenarios, reproduces the per-trial numbers below. |
| `am5-sharedctx-prod-20260929-191400.json` | first run. `idle`/`small`/`medium` agree with the rerun; its `full` (8.5%, loadgen 60 fps) is an **outlier that did not reproduce** in 8 later trials - do not use it. |
| `am5-full-threadcpu-20260929-192157.json` | `full` only, `--no-floor`, with `threadcpu.py` over the same window. 28.5%. |

| scenario | i7-3770 cpu% | 9900X cpu% | i7 cpu ms/frame | 9900X cpu ms/frame |
|---|---|---|---|---|
| idle | 11.7 | 9.1 | 10.9 | 8.47 |
| small | 34.3 | 12.5 | 7.1 | 2.84 |
| medium | 33.0 | 9.2 | 6.08 | 1.57 |
| **full** | **72.8** | **28.6** | **22.49** | **11.76** |

NVFBC floor: full-screen grab 5174 -> 1384 us, DP-4 output grab 2707 -> 820 us.

### `full`: where the 28.6% is

`threadcpu.py` under a 2560x1440@60 load, 8 trials (19:20-19:29), all within
28.1-29.2% total:

| thread | i7-3770 (plan §27) | 9900X |
|---|---|---|
| `cuda-EvtHandlr` (NVENC driver poll) | ~42% | **17.5%** (83% sys, ~92,000 wakeups/s) |
| x11vnc watch_loop | ~31% | **10.8%** |
| total | 72.8% | 28.5% |

Both halves shrank by about the same factor (2.4x / 2.9x), so the split is
unchanged: the driver poll thread is still ~60% of the `full` cost. It wakes
twice as often as on the i7 (92k vs 42-45k/s) at less cost per wakeup.

**The GPU is the bottleneck under `full`, and the bench is what saturates it.**
`nvidia-smi pmon`: Xorg at 80-91% SM (rendering loadgen's 3600 fills/frame),
x11vnc at 3-4% enc, GPU 91% total. loadgen reaches 38.5 fps (i7: 32.4), NVFBC
captures ~24 fps, the fence-paced client takes ~11.3 fps. Since the poll
thread's cost tracks GPU wait time, `full` measures GPU contention created by
the load generator more than anything in x11vnc. `medium` (GPU ~12-20%) is
the better proxy for real desktop motion: H.264 engaged, 9.2%.

Ruled out as the cause of the 8.5% outlier, each by a trial: the tool
(`measure.py` and `threadcpu.py` agree), `nvfloor` running first, the display
blanked (DPMS off), and the scenario order (the rerun repeats it exactly,
gate entering H.264 during `medium` as before). Unexplained. The outlier had
real content moving - 441.9 KB/s, 14 fps of H.264 sent - so the window was
drawn; Xorg simply rendered the same load at 60 fps with headroom.

Side note, kernel `WARNING` at `nvidia-drm-drv.c:1220`
(`nv_drm_revoke_modeset_permission`): not caused by NVFBC or x11vnc. It fires
on `close()` of any nvidia DRM fd (`drm_release -> drm_file_free`), so every
process that opens the device logs one - Xorg, gnome-shell, logind, dconf,
gst-plugin-scanner, gjs, and x11vnc (2 at its 19:07:10 NVFBC init, none when
the H.264 encoders open). `nvfloor` logs 3 per run. 36-1900 per boot. It
predates the AM5 swap and the 6.12.107 kernel: the i7 logged 400-1700 per boot,
and the first boot with any is 2026-06-03 11:26, the reboot that enabled
`nvidia-drm modeset=1` (driver 550.163.01 throughout). A driver-side WARN_ON
under modeset=1; nothing to fix in the fork.

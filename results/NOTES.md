# Recorded runs

> Comparing the fork against **stock x11vnc** (not just against its own earlier
> self) is in [stock-comparison.md](stock-comparison.md). Short version: with
> MIT-SHM working (the cross-uid attach blocker was fixed in `f3f28ad`), the
> shm path is more CPU-efficient per frame than NVFBC at equal or slightly
> lower frame rate, and the service was switched back to it on 2026-08-15.
> NVFBC remains in the tree for the cases where it wins (no shm available,
> XDamage-broken environments, fullscreen video). Read that before quoting any
> of the numbers below as "NVFBC is faster".


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

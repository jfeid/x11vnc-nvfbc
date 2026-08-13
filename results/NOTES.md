# Recorded runs

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

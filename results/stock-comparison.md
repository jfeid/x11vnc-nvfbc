# Is NVFBC actually worth it? (vs stock x11vnc)

Everything else in `results/` compares NVFBC-before against NVFBC-after. This
compares against **stock x11vnc**, built from `e2b726a` — the commit the fork
branched from, which uses `XShmGetImage` and has no NVFBC code at all.

Measured with `ab.sh`: identical load, identical client, raw encoding,
`-clip 2560x1440+0+0`, 20s per run, one client. 2026-08-14.

Reproduce the stock build:

```bash
git -C ../x11vnc worktree add --detach /tmp/x11vnc-stock e2b726a
cd /tmp/x11vnc-stock && autoreconf -fiv && ./configure && make -j$(nproc)
```

## Results

`ms/frame` is CPU cost per delivered frame — `cpu% / updates-per-sec` — which
normalises for the fact that the builds do not deliver the same frame rate.

Re-measured 2026-08-14 after fixing a CPU-accounting bug in `ab.sh`: the old
figures were floored into 10-point buckets (`bc` truncated the division before
scaling up), understating every CPU value by 0-10 points. Frame rates were
never affected. Relative conclusions survived; absolute values did not.

| load | build | CPU | updates/s | ms/frame |
|---|---|---|---|---|
| small 320x240 | stock +shm | 30.2% | 37.3 | **8.1** |
| | stock -noshm | 49.6% | 32.8 | 15.1 |
| | NVFBC before rework | 66.0% | 50.0 | 13.2 |
| | NVFBC after rework | 43.0% | **50.9** | 8.4 |
| medium 960x540 | stock +shm | 35.2% | 34.5 | **10.2** |
| | stock -noshm | 56.4% | 20.4 | 27.6 |
| | NVFBC before rework | 70.3% | 30.6 | 23.0 |
| | NVFBC after rework | 54.4% | **42.1** | 12.9 |
| full 2560x1440 | stock +shm | 45.3% | 24.9 | **18.2** |
| | stock -noshm | 76.6% | 9.4 | 81.5 |
| | NVFBC before rework | 85.4% | 19.5 | 43.8 |
| | NVFBC after rework | 81.1% | **27.2** | 29.8 |

The `NVFBC before rework` medium run took 22.9s for a 20s stream, so it was
disturbed; treat that one row as soft.

## What this says

**The answer depends entirely on whether MIT-SHM is available.**

*Against stock with working shm*, NVFBC is a frame-rate win and a CPU-efficiency
loss: it delivers 9-36% more frames but costs 1.04-1.64x more CPU per frame.

The gap **widens** as more of the screen changes — 8.1 vs 8.4 ms/frame at small
(a tie), 10.2 vs 12.9 at medium, 18.2 vs 29.8 at full. An earlier version of
this file claimed the opposite and predicted a crossover in NVFBC's favour at
large change areas; the corrected numbers show no such crossover, and the trend
runs the other way.

That direction makes sense: NVFBC always transfers the whole captured region
across PCIe regardless of how much changed, so its cost is O(screen area) and
essentially fixed per frame, and it then pays two further full-frame copies.
Stock reads sampled scanlines plus the changed tiles out of the X server, with
no PCIe readback in this path at all, so it is cheaper per byte and only pays
for what changed. At small change areas the two land in the same place; the
more that changes, the further ahead stock gets.

What NVFBC buys here is therefore frame *rate*, not CPU efficiency.

*Against stock without shm*, NVFBC wins decisively: 1.6-2.9x the frames and
1.8-2.7x better per-frame cost.

**Which case applies here:** Xorg for `:1` runs as `giannis`, and the x11vnc
service runs as **root**. The shm segments are created by x11vnc itself
(`shmget(IPC_PRIVATE, ..., IPC_CREAT | 0600)` in scan.c), so under a root
x11vnc they are root-owned mode 0600 and it is the *X server* (running as the
session user) that cannot attach them. Empirically confirmed 2026-08-14: the
stock build run as root against `:1` aborts at startup with

    X11 MIT Shared Memory Attach failed:
    X Error of failed request:  BadAccess (attempt to access private resource denied)
      Major opcode of failed request:  130 (MIT-SHM)
      Minor opcode of failed request:  1 (X_ShmAttach)

The error arrives asynchronously, so it is fatal rather than a graceful
fallback — root stock needs `-noshm` to run at all. The production service is
therefore the `-noshm` row. That is the configuration the fork is justified
against, and it is why commit `897cb8e` disables shm when NVFBC is active.

**Reaching the `+shm` rows.** Nothing about being root prevents MIT-SHM. The
blocker was that hardcoded 0600, which assumes the client and the X server are
the same user, so it failed for any user mismatch in either direction.

**Fixed** in `f3f28ad`: the segment is handed to the X server's uid via
`shmctl(IPC_SET)` rather than widening the mode to 0666, which would expose the
framebuffer to every local user. Confirmed as root against a uid-1000 Xorg —
`MIT-SHM: handing segments to X server uid 1000`, no BadAccess, server stays
up. Running x11vnc as the session user is therefore no longer required to reach
the shm path (and would not have covered the greeter on `:0` anyway, which is
uid 110 and still needs root).

## As root, with the fix: shm vs NVFBC

Same binary both ways, `-nonvfbc` vs `-nvfbc`, two runs 2026-08-14.

CPU here still comes from the pre-fix `ab.sh`, so these values are floored into
10-point buckets: read "30" as [30,40). Frame rates are unaffected and are what
the comparison rests on. A third root run with the corrected `ab.sh` would give
usable per-frame figures.

| load | path | CPU (bucketed) | updates/s, run 1 | run 2 |
|---|---|---|---|---|
| medium 960x540 | shm | [30,40) | 22.8 | 28.2 |
| | NVFBC | [50,60) | 41.2 | 39.4 |
| full 2560x1440 | shm | [40,50) | 23.1 | 23.0 |
| | NVFBC | [80,90) | 26.4 | 27.3 |

**Fullscreen matches the session-user result exactly**: shm ~23 frames against
NVFBC ~27, with NVFBC in a CPU bucket roughly twice as high. shm is the better
value there, and that conclusion is stable across users and runs.

**Medium shows a reproducible root-only penalty on the shm path.** Root shm
delivered 22.8 and 28.2 updates/s; the same binary as the session user gave
31.7, 32.1, 32.6, 33.6 and 34.5 across five runs. The ranges do not overlap.
It is specific to shm *and* to medium:

- root NVFBC matches user NVFBC (41.2/39.4 vs 42.1/43.6)
- root shm at fullscreen matches user shm (23.1/23.0 vs 24.3/24.9)
- only root shm at medium is depressed, and it is noisier too

Medium is the case that exercises the many small per-tile `XShmGetImage`
transfers, where fullscreen uses a handful of large `copy_screen` reads. So the
penalty tracks the *number* of shm requests rather than the bytes moved, which
is consistent with per-request overhead when the segment is owned by a
different uid than the requesting client. That is a hypothesis, not a
established mechanism - x11vnc's own startup probe did report 701 MB/sec as
root against 1272 MB/sec as the user, but a single timed sample cannot be
reconciled with fullscreen being unaffected, so it is a lead at best.

Practical effect: as root, shm's advantage at medium is reduced but its
fullscreen advantage - the larger one - is intact.

**The rework itself is unambiguous:** it beats the pre-rework NVFBC build on
every load, on both frame rate and CPU per frame — 8.4 vs 13.2 ms/frame at
small, 12.9 vs 23.0 at medium, 29.8 vs 43.8 at full, while delivering the same
or more frames (50.9 vs 50.0, 42.1 vs 30.6, 27.2 vs 19.5).

## Caveats

- Raw encoding, so encoder cost is identical across builds but lower than the
  Tight encoding a real client negotiates. Absolute CPU is understated; the
  comparison between builds is not affected.
- CPU comes from `/proc/<pid>/stat` over 20s. The session-user table is exact;
  the root table predates the `ab.sh` fix and is floored to 10-point buckets.
- One client attached. Encoding cost scales with client count.

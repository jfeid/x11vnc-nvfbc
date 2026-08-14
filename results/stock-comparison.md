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

| load | build | CPU | updates/s | ms/frame |
|---|---|---|---|---|
| small 320x240 | stock +shm | 20% | 37.7 | **5.3** |
| | NVFBC before rework | 60% | 51.2 | 11.7 |
| | NVFBC after rework | 30% | **52.3** | 5.7 |
| medium 960x540 | stock +shm | 30% | 33.6 | **8.9** |
| | stock -noshm | 50% | 18.2 | 27.5 |
| | NVFBC before rework | 70% | 38.0 | 18.4 |
| | NVFBC after rework | 50% | **43.1** | 11.6 |
| full 2560x1440 | stock +shm | 40% | 24.8 | **16.1** |
| | stock -noshm | 70% | 9.6 | 72.9 |
| | NVFBC before rework | 80% | 19.1 | 41.9 |
| | NVFBC after rework | 80% | **27.4** | 29.2 |

## What this says

**The answer depends entirely on whether MIT-SHM is available.**

*Against stock with working shm*, NVFBC is a frame-rate win and a CPU-efficiency
loss: 10-28% more frames delivered, but 1.3-1.8x more CPU per frame. NVFBC
transfers the whole captured region on every new frame regardless of how much
changed, so its cost is O(screen area). Stock reads sampled scanlines plus the
tiles that actually changed, so its cost is O(changed area). Stock wins on
efficiency at every size tested — the gap narrows as more of the screen changes,
which is the direction the crossover would eventually come from.

*Against stock without shm*, NVFBC wins decisively: 2.4x the frames at equal CPU
(medium), 2.9x at full screen, and 2.4-2.5x better per-frame cost.

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

Same binary both ways, `-nonvfbc` vs `-nvfbc`, 2026-08-14.

| load | path | CPU | updates/s | ms/frame |
|---|---|---|---|---|
| medium 960x540 | shm | 30% | 22.8 | 13.2 |
| | NVFBC | 50% | **41.2** | **12.1** |
| full 2560x1440 | shm | 40% | 23.1 | **17.3** |
| | NVFBC | 80% | **26.4** | 30.3 |

At fullscreen the picture is clear and matches the session-user measurements:
NVFBC buys 14% more frames for double the CPU, so shm is much better value.

**The medium row is unresolved.** As root, shm delivered 22.8 updates/s; as the
session user the same binary delivered 32.1, 32.6 and 33.6 across three runs,
all at the same 30% CPU. Full-screen shows no such gap (23.1 root vs 23.8
user), which is the opposite of what a general read-path slowdown would do.
x11vnc's own startup probe did report `fb read rate: 701 MB/sec` as root
against `1272 MB/sec` as the user, with otherwise byte-identical startup logs —
but that is a single timed sample, and it cannot be reconciled with fullscreen
being unaffected. Treat it as a lead, not a mechanism.

This matters because it decides the medium case: at 22.8 NVFBC wins on both
counts, while at ~32 shm would win on efficiency (9.4 vs 12.1 ms/frame). One
root sample against three user samples is not enough. **Repeat the root run
before acting on the medium row.**

**The rework itself is unambiguous:** it beats the pre-rework NVFBC build on
every load, on both frame rate and CPU per frame (5.7 vs 11.7, 11.6 vs 18.4,
29.2 vs 41.9 ms/frame).

## Caveats

- Raw encoding, so encoder cost is identical across builds but lower than the
  Tight encoding a real client negotiates. Absolute CPU is understated; the
  comparison between builds is not affected.
- CPU is sampled from `/proc/<pid>/stat` at 10ms granularity over 20s, so the
  10%-quantised values carry roughly +/-1 point of error.
- One client attached. Encoding cost scales with client count.

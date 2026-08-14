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
service runs as **root**. Root cannot attach SHM segments created by the user's
X server (`X_ShmAttach BadAccess`), so the production service is the `-noshm`
row. That is the configuration the fork is justified against, and it is why
commit `897cb8e` disables shm when NVFBC is active.

The `+shm` rows are only reachable by running x11vnc as the session user. If
that is possible here, stock x11vnc would use less CPU than the fork — at a
lower frame rate. That trade is worth a deliberate decision rather than an
assumption; see `x11vnc-deployment-setup` notes for why root was chosen.

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

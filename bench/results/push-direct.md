# Do -nvfbc_push and -nvfbc_direct do anything?

Both flags were implemented during the capture rework and left off by default
because their claimed benefit is latency, which nothing in `bench/` could see.
`latency.py` + `latency-ab.sh` measure it. 2026-08-14.

## Method

`loadgen -pipe` flips a 192x192 rect between two colours and stamps
`CLOCK_MONOTONIC` after `XSync`, so t0 means "the X server has this". A VNC
client sends its `FramebufferUpdateRequest` *before* the flip — so the server
already has a pending request and answers the moment it notices, instead of
folding a client round trip into the result — then records when the new colour
arrives. Both processes read the same clock.

Latency here is end to end: X server, capture, x11vnc's scan/defer scheduling,
encode, loopback socket.

## Latency (ms)

Four sweeps. Run 4 is the same as run 3 with the configuration order reversed,
as a control against position in the sweep.

| config | run1 n=40 | run2 n=40 | run3 n=150 | run4 n=150 rev | **avg p50** | avg mean | worst max |
|---|---|---|---|---|---|---|---|
| nvfbc (current default) | 58.5 | 62.2 | 71.2 | 67.2 | **64.8** | 61.6 | 110 |
| nvfbc +push | 58.8 | 42.7 | 57.1 | 58.9 | **54.4** | 52.7 | 96 |
| nvfbc +direct | 47.6 | 55.5 | 62.2 | 55.9 | **55.3** | 54.6 | **164** |
| shm (no nvfbc) | 58.0 | 56.3 | 61.1 | 60.2 | 58.9 | 56.2 | 116 |

**Push model cuts median latency by about 10ms**, roughly 16% off a ~65ms
median. That survives the order reversal — plain nvfbc was worst both when it
ran first (run 3) and when it ran last (run 4) — and it holds in both
high-sample runs. The magnitude is consistent with the mechanism: push model
removes up to `dwSamplingRateMs` (16ms) of driver-side sampling between an
application drawing and NVFBC having a frame.

**Direct capture adds nothing beyond push** (55.3 vs 54.4 avg p50, within
noise of each other) and has a markedly worse tail: 164ms and 163ms maxima in
the two high-sample runs, against 96ms and 89ms for push. Since `-nvfbc_direct`
implies `-nvfbc_push`, that tail is what direct capture adds on its own.

Note direct capture **does** engage with a small flip rect — the server logs
`[direct capture active]`. On a compositing desktop the fullscreen unoccluded
application is the compositor itself (gnome-shell), which is what NvFBC
attaches to, so the size of the thing being drawn is irrelevant.

## Throughput and CPU across the load range

`ms/frame` is `cpu% / updates-per-sec`.

| load | config | CPU | updates/s | ms/frame | MB/s |
|---|---|---|---|---|---|
| small 320x240 | nvfbc | 41.3% | 51.7 | 8.0 | 18.0 |
| | **+push** | 38.6% | **56.3** | **6.9** | 17.8 |
| medium 960x540 | nvfbc | 52.5% | 42.4 | 12.4 | 78.9 |
| | **+push** | 50.9% | **47.9** | **10.6** | 90.0 |
| full 2560x1440 | nvfbc | ~81% | **~27.0** | 30.0 | ~372 |
| | +push | ~55% | **~20.9** | 26.5 | ~220 |

The fullscreen row is three runs, and it is extremely repeatable:
26.9/26.4/27.7 updates/s without push against 20.7/21.0/20.9 with it.

**Push regresses the fullscreen case by 22% on delivered frames and 41% on
pixel data**, while using 26 points less CPU. Per-frame it is still cheaper
(26.5 vs 30.0 ms), so it is not being wasteful — it is delivering less.

The mechanism is visible in the rectangle counts: 3.4 rects per update with
push against 1.06 without, and ~10.4 MB per update against ~14 MB for a
14 MB screen. Push model generates a frame per damage event, so captures land
*mid-repaint* — loadgen issues 3600 fills per frame, and a capture taken part
way through sees only the tiles painted so far. Each captured frame is
internally coherent, but fewer complete screens per second reach the client.

Sampling at `dwSamplingRateMs` avoids this by construction: captures are 16ms
apart, which is longer than one full repaint, so each one tends to see a
complete screen.

### ...but not for video-like repaints

That repaint pattern is adversarial and unusual. A video player blits one large
image per frame rather than issuing thousands of small draws, so `loadgen -blit`
repaints with a single `XShmPutImage` of the whole window (59.8 fps achieved at
2560x1440). Under that load the regression **disappears entirely**:

| run | nvfbc | +push |
|---|---|---|
| 1 | 84.3% / 19.1 updates/s / 269 MB/s | 84.4% / 19.3 / 271 |
| 2 | 85.3% / 19.2 / 270 | 85.1% / 19.5 / 273 |

Identical within noise, and 1.0 rects per update for both — no fragmentation,
because one blit is one damage event and a capture cannot land part way through
it.

So the fullscreen regression is specific to applications that repaint a large
area via many small draw operations. Fullscreen *motion* content — video,
games — does not behave that way.

## Conclusion

**`-nvfbc_push` is worth enabling.**

- median latency down ~10ms (~16%)
- 9-13% more frames at small and medium change areas, at slightly lower CPU
- neutral at fullscreen under video-like repaints
- the only regression found needs a large area repainted via thousands of small
  draws, which is not how fullscreen motion content behaves

**`-nvfbc_direct` is not.** It matches push on the median while adding a ~165ms
tail, and it forces `-nvfbc_nocursor`.

## What this corrects

An earlier assessment in this project called the latency benefit of push model
"a reasonable hypothesis, not a result", on the grounds that `pollmode.c` found
no difference between sample and push modes. That was right at the time —
`pollmode.c` measures the new-frame ratio at a fixed poll rate, not latency, so
it could not have seen this. The hypothesis is now supported.

An earlier version of this file recommended `-nvfbc_push` outright, on the
strength of the latency result plus a throughput measurement taken only at
medium load. Extending the throughput measurement across the load range found
the fullscreen regression above, which that recommendation had not tested for.

The first two sweeps at n=40 disagreed with each other about which config won
(direct in run 1, push in run 2), because run-to-run variation of up to 16ms
exceeded the ~10ms effect. Do not draw conclusions from a single n=40 sweep
here; use n=150 and run the reversed control.

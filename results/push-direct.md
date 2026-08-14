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

## Throughput and CPU cost (medium load, 960x540 @60fps)

| config | CPU | updates/s |
|---|---|---|
| nvfbc | 51.7% | 43.9 |
| nvfbc +push | 50.5% | 48.3 |
| nvfbc +direct | 49.4% | 43.6 |

Neither flag costs CPU; all three are within noise of each other. Push may
deliver slightly more updates. The concern that push model would cost extra GPU
work by generating frames faster than they are consumed did not show up at this
load — an application rendering far above the served rate could still provoke
it, which this does not test.

## Conclusion

`-nvfbc_push` is worth enabling: ~10ms lower median latency, no CPU cost, no
throughput cost, better tail than either the current default or direct capture.

`-nvfbc_direct` is not: it matches push on the median while adding a ~165ms
tail, and it forces `-nvfbc_nocursor`.

## What this corrects

An earlier assessment in this project called the latency benefit of push model
"a reasonable hypothesis, not a result", on the grounds that `pollmode.c` found
no difference between sample and push modes. That was right at the time —
`pollmode.c` measures the new-frame ratio at a fixed poll rate, not latency, so
it could not have seen this. The hypothesis is now supported.

The first two sweeps at n=40 disagreed with each other about which config won
(direct in run 1, push in run 2), because run-to-run variation of up to 16ms
exceeded the ~10ms effect. Do not draw conclusions from a single n=40 sweep
here; use n=150 and run the reversed control.

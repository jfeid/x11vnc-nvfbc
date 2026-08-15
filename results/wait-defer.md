# Are -wait and -defer still needed with -nvfbc_push?

Yes — they control a different layer, and they now dominate latency by a wide
margin. 2026-08-15.

## What they actually control

| flag | effect |
|---|---|
| `-wait N` | `waitms`, the sleep between x11vnc scan cycles (`choose_delay`, screen.c) |
| `-defer N` | `screen->deferUpdateTime`, libvncserver's coalescing delay before sending |

Both are x11vnc/libvncserver scheduling. `-nvfbc_push` changes when the NVIDIA
driver *generates* a frame, which is upstream of both, so it makes neither
redundant. Push removes up to 16ms of driver-side sampling; these two decide how
long the frame then sits before it is scanned and sent.

## Measured, with `-nvfbc_push` on in every variant

Latency: two runs of n=100. Throughput: three runs, 960x540 @60fps.
`ms/frame` is `cpu% / updates-per-sec`.

| variant | latency p50 | CPU | updates/s | ms/frame |
|---|---|---|---|---|
| `-wait 5 -defer 10` (production) | 59.0 / 55.4 | ~54.0% | ~44.0 | 12.3 |
| `-wait 1 -defer 1` | **25.5 / 25.9** | ~60.9% | **~59.1** | 10.3 |
| `-wait 10 -defer 10` (x11vnc's own auto-tune) | 48.4 / 58.9 | ~47.9% | ~52.9 | **9.1** |
| `-wait 20 -defer 20` (upstream default) | 101.2 / 111.0 | ~32.4% | ~34.1 | 9.5 |

Latency spans **25ms to 111ms** across these settings. Push model's effect was
~10ms. So scheduling, not capture, is now the dominant term — tuning these is a
bigger lever than anything left in the capture path.

## The production setting is dominated

`-wait 5 -defer 10` is worse than `-wait 10 -defer 10` on **both** axes:
~20% fewer frames delivered (44.0 vs 52.9) for ~6 points *more* CPU (54.0 vs
47.9), at roughly equal latency. Reproduced across three throughput runs with
little scatter.

Scanning twice as often appears to cost more than it gains — most extra cycles
find nothing, and the work is not free. That is the shape of the data; the
mechanism is not established here.

## Options

- **Lowest latency: `-wait 1 -defer 1`.** 25ms against 59ms is a 2.3x
  improvement and is felt directly in interactive use. Costs ~7 CPU points, but
  delivers 34% more frames, so it is *more* efficient per frame than production
  (10.3 vs 12.3 ms/frame). Best choice if the machine is used interactively and
  has CPU headroom.
- **Lowest CPU: drop the flags entirely.** With no `-wait`/`-defer`, x11vnc
  auto-tunes to exactly `wait 10 / defer 10` when it measures framebuffer reads
  faster than 80 MB/sec (x11vnc.c: `waitms /= 2`, `defer_update = 10`), which is
  the best per-frame row here. More frames and less CPU than production.
- **Do not keep `-wait 5 -defer 10`.** Both alternatives beat it.

`-wait 20 -defer 20` is the cheapest in absolute CPU but has the worst latency
by far (~110ms) and the fewest frames; only worth it on a CPU-starved host.

## Caveat

One load (960x540 @60fps) and one client. `-wait 1` scans far more often, so its
CPU cost should scale with how much of the screen changes — the fullscreen case
was not measured here and could look different.

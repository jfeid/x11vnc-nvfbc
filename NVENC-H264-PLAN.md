# NVENC H.264 for x11vnc-nvfbc — implementation plan

Status: **Phases 0-2 complete** (2026-08-21), hybrid gate wired. Phase 3 as written is
pointless - see §14; the replacement target is x11vnc's scan/copy, not the upload.
Written 2026-08-19.

Goal: emit RFB **encoding 50** (the open H.264 encoding) from an NVENC-encoded
stream, so a TigerVNC client renders the desktop as H.264 instead of Tight+JPEG.

Two independent motivations, both measured rather than assumed:

- **CPU.** After the capture rework, capture is 17 of 67.9 points at `full`
  (25%), projected floor 60.6%. Capture is finished as an optimisation target;
  the rest is scan/compare/copy plus libvncserver encoding. A full-frame H.264
  path removes both, since it does no damage detection.
- **Bandwidth.** Tight+JPEG demands ~65 Mbps for fullscreen video-like content.
  The link to the real client is 89141/11000 Kbps VDSL2 17a. H.264 at 20-25
  Mbps for the same content is the difference between saturating that link and
  having room in it. See `bench/results/NOTES.md`.

---

## 1. What the client accepts — verified, not recalled

Confirmed against the connected client (`bench/`-adjacent probe, 2026-08-19)
and against TigerVNC upstream source.

The client advertises **encoding 50** at preference position 19 of 26, and the
viewer's options dialog offers H.264 as a selectable preferred encoding. Note
that LibVNCServer's `rfbEncodingH264` (`0x48323634`, `rfbproto.h:463`) is a
**different and incompatible** number with no implementation behind it. Ignore
it entirely.

### Rect wire format

From `TigerVNC/common/rfb/H264Decoder.cxx`:

```
U32  length          bytes of H.264 data that follow
U32  flags           0x1 = resetContext, 0x2 = resetAllContexts
U8   data[length]    H.264 Annex-B byte stream
```

`length == 0` with `resetAllContexts` is a valid reset-only message.

### Decoder context semantics — this drives the whole design

- Contexts are looked up by **exact rect geometry** (`isEqualRect`). A rect at
  a different position or size gets a *different* decoder context.
- `MAX_H264_INSTANCES = 64`; the oldest context is destroyed when exceeded.

**Consequence:** damage-driven variable rects would spawn a new decoder context
per distinct geometry, churn the 64-slot LRU, and force every frame to be an
IDR. Inter-frame prediction — the entire reason to use H.264 — would never
engage. See §3.

### Client-side decoders

- **Windows (this client):** Media Foundation MFT, `MFVideoFormat_H264`,
  `MF_LOW_LATENCY = TRUE`, decodes to NV12 then converts to BGRX.
- **Linux:** libavcodec, feeding `av_parser_parse2` — confirms Annex-B
  byte-stream framing rather than AVCC length-prefixed.

**4:2:0 is forced.** The MF path requests NV12 output; 4:4:4 will not decode.
Measured in Phase 0 and found acceptable — see §10.

### The SPS must be the first NAL of every access unit

Not optional, and not documented anywhere obvious. TigerVNC's
`H264WinDecoderContext::ParseSPS()` does **not scan** for the SPS:

```c
EXPECT((length >= 3 && buffer[0]==0 && buffer[1]==0 && buffer[2]==1) || ...);
...
EXPECT((type & 0x1f) == 7); // SPS NAL unit type
```

It checks `buffer[0..3]` for a start code and then demands NAL type 7. Anything
in front of the SPS — an access unit delimiter, an SEI — makes it return early,
leaving `full_width`/`full_height` at zero. The blit is then skipped by a bounds
check (`if ((offset_x >= full_width) || ...) return;`), so the client renders
**black with no error anywhere**: `ProcessInput` failures are swallowed
deliberately, with the comment *"Silently ignore errors, hoping its a temporary
encoding glitch"*.

So each rect payload must be `SPS + PPS + slice data`, with any AUD stripped.
This is `repeatSPSPPS` (§5) promoted from advisable to mandatory — which is fine,
since repeating parameter sets is also what lets a decoder join or resync
mid-stream.

Phase 0 hit this exactly: an AUD-first stream produced a black rectangle while
the protocol layer looked perfectly healthy for 583 frames.

---

## 2. The architectural constraint, and why it is not blocking

`x11vnc` does **not** bundle libvncserver. It links the system
`libvncserver.so.1` (0.9.15), which has no H.264 encoder — `nm -D` shows no
H.264 symbols at all, only the unused constant.

Forking libvncserver is not necessary. The system library exports everything
needed to emit our own rects alongside its encoders:

| symbol / field | use |
|---|---|
| `rfbRegisterProtocolExtension` | register an extension |
| `.enablePseudoEncoding` hook | called for any encoding libvncserver does not recognise, including 50 — set a per-client flag here |
| `cl->updateBuf`, `cl->ublen` | the same output buffer `rfbSendRectEncodingRaw` uses |
| `rfbSendUpdateBuf(cl)` | flush it |
| `rfbWriteExact(cl, buf, len)` | write a large payload directly |
| `cl->sendMutex` | **mandatory** — the service runs `-threads`, and writing concurrently with libvncserver's own writer corrupts the stream |

So the work is entirely inside the fork.

---

## 3. Design decision: one fixed full-screen rect per frame

Given §1's context semantics, the only sane shape is a **single rect covering
the served region** (`-clip 2560x1440+0+0`), constant for the life of the
connection, carrying one continuous H.264 stream.

This is how game streaming works, and it is what Sunshine does.

**What it costs:** damage-based partial updates are abandoned. The encoder sees
every frame whole. That is fine — H.264 inter-frame prediction handles a mostly
static screen far more cheaply than Tight handles a full-screen repaint — but
it means:

- Gate encoding on *any* damage, so an idle desktop does not stream continuously.
- A resize (`-clip` change / `ExtendedDesktopSize`) must send `resetContext`.
- Reconnect must send `resetAllContexts`.

---

## 4. Encoder route

Both routes were checked against what is actually installed on this machine.

### Route A — libavcodec `h264_nvenc` (recommended for first implementation)

Already satisfied, **zero new dependencies**:

```
libavcodec-dev      61.19.101 (FFmpeg 7.1)  /usr/include/x86_64-linux-gnu/libavcodec/
libavutil           59.39.100
h264_nvenc encoder  present in ffmpeg
libnvidia-encode.so.1, libcuda.so.1, nvcc, /usr/include/cuda.h   all present
```

FFmpeg owns the NVENC session lifecycle, capability probing, and driver-version
quirks. It also accepts CUDA device frames via `AVHWFramesContext` /
`AV_PIX_FMT_CUDA`, so the zero-copy path in §6 remains available.

### Route B — NVENC SDK directly

Needs `nv-codec-headers` (`nvEncodeAPI.h` is not installed) plus session
management, capability queries, and buffer pooling written by hand. More
control, materially more code, no advantage that Route A cannot reach.

`sunshine-reference/src/nvenc/` is only a partial reference: `nvenc_d3d11*.cpp`
is Windows/D3D11. On Linux Sunshine uses FFmpeg's nvenc. `nvenc_base.cpp` and
`nvenc_colorspace.h` are the portable parts.

**Decision: Route A.** Revisit only if FFmpeg's abstraction costs measurable
latency.

---

## 5. Encoder configuration

Non-negotiable for interactive use:

- `bf = 0` — any B-frame adds reorder latency.
- `repeatSPSPPS` / `AV_CODEC_FLAG_GLOBAL_HEADER` off, SPS/PPS in-band so the
  decoder can resync after loss or a mid-stream join.
- `tune = ll` / `ultralowlatency`, `preset` p1-p4 — measure, do not guess.
- `zerolatency`-equivalent: no lookahead, no frame delay.
- Rate control: CBR or capped VBR at ~20-25 Mbps for 2560x1440. The point is
  not to fit the link but to stay far enough below it that the bottleneck queue
  never fills.
- IDR interval: long, with periodic intra-refresh rather than large IDR spikes,
  which are the burst that causes head-of-line delay in the tunnel.

---

## 6. Capture path

`src/nvfbc/nvfbc_capture.c` uses `NVFBC_TOSYS` throughout (41 references),
concentrated in two functions:

- `nvfbc_start_capture()` — `NVFBC_TOSYS_SETUP_PARAMS` (line ~246, ~323)
- `nvfbc_grab_frame()` — `NVFBC_TOSYS_GRAB_FRAME_PARAMS` (line ~348)

`NVFBC_SHARED_CUDA` is already in the vendored `NvFBC.h` enum, unused.

The module's public API (`nvfbc_capture.h`) is clean and small, so adding a
capture-type selector plus a CUDA-pointer variant of `nvfbc_grab_frame()` is a
contained change. **Keep `NVFBC_TOSYS` working** — it is what the Tight
fallback needs.

---

## 7. Risks and decision points

| risk | assessment |
|---|---|
| ~~4:2:0 chroma degrades text~~ | **Resolved in Phase 0 — not a blocker.** Measured, not guessed: see §10. The effect is real and structured but small, and amounts to downgrading subpixel antialiasing to grayscale antialiasing. |
| Constant bandwidth when anything moves | Full-frame encoding has no partial-update path. Damage-gating limits it to "screen is changing", not "how much changed". |
| `-threads` stream corruption | All writes must hold `cl->sendMutex`. |
| Client without encoding 50 | Must fall back cleanly to Tight per client, not per server. |
| Cursor | Service runs `-nvfbc_nocursor`. Decide whether the cursor is composited into the encoded frame or left to the RFB cursor pseudo-encoding. |
| FFmpeg abstraction latency | Unquantified. Phase 2 measures it. |
| **4:2:0 vs the 4:4:4 Tight baseline** | Live after all. Tight is full-chroma at quality 6-9, so H.264 is a real step down on coloured content, and the operator called it "barely acceptable". Mitigations that do not work: grayscale antialiasing (§10), more bitrate (the 4:2:0 ceiling is structural), 4:4:4 encoding (Media Foundation will not decode it). See the hybrid below. |

### Hybrid fallback, if 4:2:0 stays unacceptable

RFB lets the server pick an encoding per update, so Tight and H.264 can coexist
on one connection: **Tight while the screen is static**, where text quality
matters and Tight is cheap because little changes, and **H.264 once motion is
detected**, where Tight collapses and nobody is reading fine text.

That keeps today's full-chroma quality for reading and gets H.264's efficiency
for video and scrolling. Cost: extra state, and careful handling of the H.264
decoder context across the switch - every return to H.264 needs `resetContext`
plus an IDR, since the client's context will have gone stale.

Worth prototyping in Phase 2 rather than deferring, because it changes what
Phase 1's rect-emission code has to be able to express.

---

## 8. Phases

Each phase ends with something measurable. Do not proceed on a failed phase.

### Phase 0 — de-risk the format and the picture quality (no x11vnc changes) — **DONE**

Cheapest possible answer to the two questions that could kill the project.

1. Encode a still of the real desktop with `h264_nvenc` at 4:2:0, 20-25 Mbps.
   View it next to the original at 100%. **This is the go/no-go on text.**
2. Write a throwaway RFB server that serves one static encoding-50 rect from a
   pre-encoded Annex-B file, and connect the real TigerVNC 1.16.2 client to it.
   Confirms the wire format, the flags, and that the MF decoder accepts our
   stream — before any of it is entangled with x11vnc.

The existing probe in the session scratchpad already does the RFB 3.8 server
handshake and can be extended for step 2.

### Phase 1 — encoding-50 transport inside x11vnc, no NVENC — **DONE**

Register the extension, catch encoding 50, set a per-client flag, emit rects
under `cl->sendMutex`. Feed it **software** H.264 (or a pre-encoded loop) so
transport bugs stay separate from encoder bugs.

Done when: the real client shows a moving picture over the real tunnel.

### Phase 2 — NVENC, system memory — **DONE**

Wire `h264_nvenc` to the existing `NVFBC_TOSYS` BGRA output. Convert BGRA→NV12
with libswscale first — correctness before speed. Measure against Tight with
`bench/encoding-ab.sh`.

Done when: CPU and bytes are both measured against the Tight baseline.

### Phase 3 — zero-copy CUDA

`NVFBC_SHARED_CUDA` plus `AVHWFramesContext` / `AV_PIX_FMT_CUDA`, and a CUDA
kernel for BGRA→NV12. Removes the GPU→CPU→GPU round trip.

Done when: measured against Phase 2. If the gain is small, Phase 2 is a fine
place to stop.

### Phase 4 — tuning

Preset/tune sweep, rate control, intra-refresh vs IDR, latency via
`bench/vncprobe.py`.

---

## 9. Measurement

`bench/encoding-ab.sh` is the harness. It isolates the server from the live
desktop and from the transport, which the earlier `measure.py` runs did not —
see `results/NOTES.md` for how that produced a physically impossible result.

It will need an H.264 leg alongside the Tight legs. `rfbcheck.py` will need to
request encoding 50 and size the rects; it already walks Tight rect headers
without decoding, and the H.264 rect header is far simpler (`U32 len, U32
flags`), so this is a small addition.

Compare on: cpu%, wire MB/s, and keypress latency. Not on `new_fps`.


## 10. Phase 0 results (2026-08-20)

### Step 1 — 4:2:0 text quality: acceptable

Captured the live 2560x1440 served region (browser with subpixel-antialiased
body text on white, terminal with coloured monospace on dark), encoded with
`h264_nvenc`, compared against the original.

| | SSIM vs original | RMSE on a text crop |
|---|---|---|
| 4:4:4 @ 100 Mbps | 0.998 | 0.30% |
| 4:2:0 @ 100 Mbps | 0.986 | 3.10% |
| 4:2:0 @ 20 Mbps | 0.969 | |
| 4:2:0 @ 10 Mbps | 0.958 | |

The 100 Mbps pair isolates subsampling from quantisation: at effectively
unlimited bitrate 4:4:4 converges to near-perfect while 4:2:0 plateaus at 0.986.
That residual cannot be bought back with bitrate.

The desktop runs `Xft.rgba: rgb` (subpixel antialiasing), which is the worst
case — glyph edges are encoded as colour fringes, exactly the chroma detail
4:2:0 discards. An amplified difference image shows every glyph legible in the
4:2:0 error signal and only featureless noise in the 4:4:4 one: the loss is real
and structured, 10x the RMSE.

**But at 4x magnification side by side it is not distinguishable**, and 4:2:0 at
20 Mbps is not distinguishable from 4:2:0 at 100 Mbps. For text the binding
constraint is the subsampling, not the bitrate, and its cost is bounded.

Accurate characterisation: **4:2:0 downgrades subpixel antialiasing to grayscale
antialiasing.** It does not smear text or add fringing. An earlier draft of this
plan overstated it as "very visible on a coding desktop"; that was wrong.

Free mitigation: switching the desktop to grayscale antialiasing costs nothing
under H.264, because there is then no chroma detail to lose.

**Correction (2026-08-20).** Two things came out of trying that on the real
desktop, and both matter:

1. **The baseline is 4:4:4, not something already subsampled.** Real Tight JPEG
   payloads dumped from the running encoder report sampling factor
   `1x1,1x1,1x1` at quality levels 6-9. Tight preserves full chroma. So moving
   to H.264 4:2:0 is a genuine quality regression against what a client sees
   today - it is not, as assumed for a while, merely swapping one subsampled
   path for another. (Levels 5 and below drop to 4:2:2; see
   `bench/results/NOTES.md`.)
2. **Grayscale antialiasing does not buy much here.** Switched on the live
   desktop, the operator could not distinguish it from subpixel rendering while
   looking directly at the console. If the subpixel fringes are not visible in
   the first place, removing them recovers nothing - and the degradation that
   *was* visible in the 4:2:0 samples comes from chroma loss on actual colour
   (syntax highlighting, coloured UI), not from antialiasing fringes.

The operator's verdict on 4:2:0 against this baseline was "barely acceptable".
That is a fair judgement against a fair reference, and §7 now carries the hybrid
option because of it.

### Step 2 — wire format and client decode: confirmed

`bench/h264serve.py` served a pre-encoded 1280x720 stream (150 frames, panning,
so inter-frame prediction is genuinely exercised) as encoding-50 rects to the
real TigerVNC 1.16.2 client over the real SSH tunnel. Picture confirmed by eye.

Confirmed by this: the rect layout (`U32 length, U32 flags, U8 data[]`), one
access unit per FramebufferUpdate, `resetAllContexts` at stream restart, and the
SPS-first requirement above.

The first attempt rendered black for 583 frames with a clean protocol layer
throughout. That is the failure signature to remember: **encoding 50 gives no
diagnostics.** A silent black rectangle is the client's way of reporting a
malformed stream, so Phase 1 should verify against a known-good stream before
trusting anything the encoder produces.


## 11. Hybrid: deciding static vs. motion

Chosen over H.264-everywhere because Tight is 4:4:4 at quality 6-9 and the
operator judged 4:2:0 "barely acceptable" against it (§10). Tight while the
screen is static, H.264 once it moves.

### The client handles interleaving — verified, not assumed

TigerVNC dispatches **per rectangle**, from a lazily-built per-encoding decoder
map (`DecodeManager.cxx:111`, `decoders[encoding]`). There is no mode, no
negotiation, and no switch cost at the protocol level: encodings can interleave
freely on one connection.

Two ordering guarantees make mixing safe, both in `DecodeManager::findEntry()`:

- `H264Decoder` is constructed `Decoder(DecoderOrdered)`, and the ordered check
  serialises a rect only against **earlier rects of the same encoding**
  (`entry->encoding == entry2->encoding`). H.264 rects therefore decode strictly
  in stream order, which a stateful codec requires.
- Cross-encoding correctness is spatial, not by-encoding: `lockedRegion`
  accumulates the affected region of every earlier queue entry
  (`assign_union`), and any rect overlapping it waits. So a Tight rect
  overlapping a pending H.264 rect cannot be applied out of order.

Because this design uses **one full-screen H.264 rect** (§3), every Tight rect
overlaps it, so transitions are fully serialised by construction.

**Server obligation on resume:** the client's H.264 context is keyed by rect
geometry and survives a Tight interlude, but its reference frames are stale by
then. Every return to H.264 must send `resetContext` (0x1) together with an IDR.

### The signal already exists

`xwrappers.c:564` `nvfbc_mark_tiles_from_diffmap()` walks the NVFBC diff map
each scan cycle and **returns the number of tiles it marked**. Enabled by
default (`options.c:251`, `nvfbc_with_diffmap = 1`) and validated against an
independent per-tile memcmp by `bench/diffcheck`. The detector needs no new
capture machinery — only to read a value already computed.

### Metric: dirty area per second, in screens

```
dirty_frac = tiles_marked / (ntiles_x * ntiles_y)      # per scan cycle
rate       = EWMA(dirty_frac * cycles_per_second)      # ~500 ms window
```

Normalising to screens/second keeps the threshold independent of resolution and
frame rate, so it survives a `-clip` change.

### Threshold, derived from measured constants

| | |
|---|---|
| Tight cost | 0.60 B/px (`encoding-ab`: 11.65 MiB/s / 39.1 fps / 518,400 px at quality 8) |
| Full-screen Tight frame | 2.21 MB (2560x1440) |
| Link budget | 9.13 MB/s (73 Mbps shaped) |
| **Tight affords** | **4.1 screens/s** |
| H.264 @ 20 Mbps | 2.5 MB/s, flat regardless of motion |

Entry threshold **~3 screens/s**, just under the affordability limit. Below it
Tight fits the link and looks better; above it Tight overruns and drops frames
while H.264 stays flat.

### It is not a knife-edge

| activity | dirty rate | vs 3/s |
|---|---|---|
| typing, cursor blink | ~0.005 screens/s | 600x below |
| quarter-screen video @30fps | ~7.5 screens/s | 2.5x above |
| dragging a window | ~20 screens/s | 7x above |
| scrolling a page @60fps | ~60 screens/s | 20x above |

Real usage clusters at the extremes; anything from 1 to 10 screens/s classifies
all of these identically. That margin matters because 0.60 B/px was measured on
`loadgen`'s gradient, which is harsher than real desktop content - the true
affordability limit is higher, which only widens the gap.

### Hysteresis

- **Enter H.264:** rate > 3 screens/s sustained >= 150 ms. Slow entry protects
  text quality against transients.
- **Exit to Tight:** rate < 0.75 screens/s (a quarter - deliberately asymmetric)
  sustained >= 300 ms, with a ~500 ms minimum dwell in H.264 to bound switch
  frequency.

**The exit refresh matters more than the thresholds.** On leaving H.264 the
client holds the 4:2:0 decode - nearly right, just chroma-degraded. A blocking
full-frame Tight update is 2.21 MB, ~0.24 s of link, a visible hitch. Re-send
progressively over ~0.5 s by marking tiles in batches instead: quality settles
in rather than stalling. That bounds the cost of switching, which is what allows
liberal exit, which is what keeps settled text crisp.

### Capture-path conflict, and why it resolves

`NVFBC_TOCUDA_SETUP_PARAMS` carries only `dwVersion` and `eBufferFormat` - **the
CUDA path has no diff map**. As written, Phase 3's zero-copy and this detector
are mutually exclusive.

They are not, because each mode already has the signal it needs:

- **In Tight mode** capture is on ToSys anyway (Tight needs CPU pixels), so the
  diff map is present. Use it to decide when to **enter** H.264.
- **In H.264 mode** every frame is encoded regardless, and **NVENC's output
  frame size is itself a motion signal** - a static screen yields tiny P-frames.
  Use it to decide when to **leave**.

No extra CUDA kernel, and Phase 3 survives.

### What Phase 2 must measure

1. **Cost of switching the NVFBC session between ToSys and CUDA.** If session
   recreation is expensive, or if two sessions cannot coexist on a consumer
   card, it is cheaper to stay on ToSys and accept the host round-trip - which
   §8 already treats as an acceptable stopping point.
2. **0.60 B/px on real desktop content**, not the gradient, to place the entry
   threshold properly.
3. **Perceived cost of the exit refresh** - whether progressive re-send is
   actually needed or a single full-frame update is tolerable.


## 12. Phase 1 results (2026-08-21)

`src/h264/h264_stream.{c,h}`: protocol extension, per-client state, rect
emission. Driven by `-h264_testfile`, a container of pre-encoded access units
(`bench/make-aus.py`) so transport could be proven without an encoder in the
picture. Hook is `h264_frame_tick()` at `screen.c`, immediately after
`scan_for_updates()` returns `tile_diffs`.

Verified with `bench/rfbcheck.py --tight --h264` and then against the real
TigerVNC client: desktop rendering via Tight with a 1280x720 H.264 video in the
corner, both encodings interleaved on one connection.

```
stream: 69.6 updates/s, 103.6 rects/s, 2.19 MB/s over 6.0s
  sub-encodings: fill=5, h264=352 (56.1%), h264_flags2=3, jpeg=213 (33.9%), palette=55
```

352 H.264 rects and 213 Tight rects, no desync. That is the hybrid's core
assumption (§11) demonstrated, not assumed.

### Do not lock cl->sendMutex in the emission path

The first attempt deadlocked and every session went blank with nothing logged.

`watch_loop()` already holds `LOCK(cl->sendMutex)` for **every client** across
its entire scan section (`screen.c`, the "send ban") - which is exactly the
window in which libvncserver is guaranteed not to be writing, and therefore
exactly when it is safe to emit. Taking that mutex again on the same thread
self-deadlocks: it is not recursive.

The trap is that `LIBVNCSERVER_HAVE_LIBPTHREAD` is defined in
`/usr/include/rfb/rfbconfig.h`, **not** in x11vnc's own `config.h`, so `LOCK()`
looks like it might compile away in x11vnc sources. It does not.

Symptom to recognise: watch_loop dies on the first client that enables encoding
50, the initial Tight update still arrives (sent before the deadlock), and every
later client times out because nothing is scanning. Threads sit in
`futex_wait_queue`, which reads as idle.

**Caller contract, now documented in the header:** hold the send ban, or be
single-threaded. Do not lock inside `h264_send_rect()`.


## 13. Phase 2 results (2026-08-21)

`src/h264/h264_encode.{c,h}` (libavcodec `h264_nvenc`) plus the §11 gate in
`h264_stream.c`. Options: `-h264`, `-h264_bitrate`, `-h264_fps`, `-h264_enter`,
`-h264_exit`, `-h264_force`, `-h264_testfile`.

| | idle | sustained motion |
|---|---|---|
| encoding | 100% Tight | 96.8% H.264 |
| wire | 0.30 MB/s | 1.83 MB/s (~15 Mbps) |
| CPU | 13.5% | 52.1% |

Route A needed no new dependencies, as predicted: `configure` found
libavcodec/libavutil already installed.

### Feed NVENC BGRA; do not convert on the CPU

The first version converted BGRA->NV12 with libswscale and cost ~55% of a core
at 2560x1440@30 - larger than the encode itself, and enough to make scrolling
visibly laggy because the tick could not keep up.

`h264_nvenc` accepts packed `bgr0`/`bgra` directly and converts on the GPU. The
AVFrame now points straight at `screen->frameBuffer` with no allocation and no
copy on our side, which is safe because the tick runs inside watch_loop's send
ban, after `scan_for_updates()` has finished writing.

`AV_CODEC_FLAG_GLOBAL_HEADER` puts SPS/PPS in extradata, which is what lets
every access unit be emitted as SPS+PPS+slice - the §1 requirement becomes a
property of the design rather than something to remember.

### Honour the viewer's preferred-encoding setting

TigerVNC advertises encoding 50 whether or not the user selected it (position 19
of 26 with Tight chosen), so "listed" does not mean "wanted". libvncserver walks
SetEncodings in order and sets `cl->preferredEncoding` on the first encoding it
recognises, having reset it to -1; ours is not one it knows, so if it is still
-1 when our callback fires, 50 arrived ahead of every real encoding and the user
picked H.264. Verified both ways.

### Check for a consumer before encoding, not at broadcast time

Encoding unconditionally and discarding the output cost 60.4% versus 12.8% for
the same Tight-only client, and starved the Tight path badly enough to look like
jerky text - a rendering complaint that was really CPU starvation.

### Two gate bugs, both silent

**The exit repaint was swallowed by its own guard.** Leaving H.264 marks the
whole screen for a Tight repaint, but `exclusive` is only recomputed *after*
`h264_update_gate()` returns, so the guard in `scan.c` dropped that very mark.
The screen froze on the last H.264 frame and updated only where fresh damage
landed. Drop the claim *before* asking for the repaint.

**Waiting for the Tight backlog to drain deadlocked the gate.** Entry must wait
for the connect-time paint, which at 2560x1440 outlasts any fixed grace on a
remote link. But requiring an empty `modifiedRegion` unconditionally means the
gate never engages under sustained motion, because it is never empty. The check
has to be a one-shot latch satisfied by drain **or** timeout.

### CPU is now the host->device upload

52% under motion is no longer colour conversion - it is ~300 MB/s of BGRA
crossing PCIe every second, plus x11vnc's own scan and copy. That is exactly
what `NVFBC_SHARED_CUDA` removes. §8 called Phase 3 optional if Phase 2's
numbers were good enough; they are not, so **Phase 3 is required**.

### Measurement note

Short windows badly over-weight the connect transient. A 10 s sample showed
"76% Tight during H.264 mode" and sent me hunting a leak that did not exist; at
34 s the same setup reads 97.3% H.264. The tell was in the rect geometry -
`2048x32 x45, 512x32 x45` is exactly one full-screen tile pass, not ongoing
traffic. Measure past the transient, and read rect sizes before diagnosing.


### Validation on real workloads (2026-08-21)

Operator testing over the real tunnel with TigerVNC, H.264 selected:

- **Typing** - indistinguishable from Tight. The gate keeps static screens
  entirely on Tight, so a keystroke is still a small immediate rect.
- **Scrolling** - "much better"; VSCode fine, YouTube fine. Possibly a shade
  slower than Tight on YouTube page-down, unconfirmed.
- **Transitions** - no jitter or jarring when handing back to Tight. The
  asymmetric hysteresis and the full repaint on exit are not perceptible.
- **Scrolling is visibly *cleaner* than Tight.** Not predicted: Tight's
  tile-based partial updates sometimes leave artifacts mid-scroll, whereas a
  full-frame encode is internally consistent by construction, so there is
  nothing to tear. Quality argument for H.264 during motion, independent of
  bandwidth and CPU.

The initial-draw delay reported earlier did not reproduce - fast with the tunnel
held up, and fast again after dropping and re-establishing it. No server-side
cause was found either: full-screen Raw fetches were flat at 1.63-1.65 s across
cold and warm, first-rect latency was 103 ms cold vs 131 ms warm, and
`avcodec_open2` costs 203 ms. Treated as a one-off.

### Open: is H.264 the right choice for *small-area* motion?

The gate keys on dirty rate, and §11 derived its threshold from when Tight stops
being affordable across the whole screen. A quarter-screen video at 30fps is
7.5 screens/s and trips the gate, but Tight encoding just that rectangle may
well be cheaper and faster than a full-frame 2560x1440 encode. Worth measuring
before assuming the single threshold is right - a dirty-*area* term, not just a
rate, may belong in the decision.


## 14. Phase 3 redirected (2026-08-21)

Phase 3 was going to move capture to `NVFBC_SHARED_CUDA` so frames never touch
host memory. Two measurements say don't.

### The upload is not the cost

Same motion, same server, only the encode rate varied:

| encode rate | CPU |
|---|---|
| 5 fps | 49.9% |
| 15 fps | 49.3% |
| 30 fps | 49.4% |

**Flat across 6x.** NVENC submission is asynchronous and the host->device copy
is DMA, so per-frame encoding costs the CPU almost nothing. The ~49% is
x11vnc's own scan and tile copy, which happens whatever the pixels are used
for. Zero-copy capture would remove work that is not being done.

§13 asserted the 52% was "~300 MB/s of BGRA crossing PCIe". That was never
measured and it was wrong.

### And the switch would be expensive anyway

`bench/nvswitch` measures the capture-type switch the hybrid would have needed:

```
create TO_SYS      : 18.3 ms
create SHARED_CUDA : 18.7 ms
full switch        : 19.6 ms
two sessions, one handle  : refused ("already running for this NvFBC client")
two sessions, two handles : refused ("a different context is already bound")
```

Concurrent ToSys and CUDA sessions are impossible, so every gate transition
would pay ~20 ms plus a full re-capture - on the exit path, which is the
quality-critical one. §11's "each mode uses the signal available to it" was a
neat resolution to a problem worth avoiding entirely.

### What the numbers say instead

Same server, same motion, only the client's encoding differing:

| | CPU | wire |
|---|---|---|
| Tight | 99.1% (saturated) | 25.79 MB/s |
| H.264 | 53.6% | 2.14 MB/s |

The hybrid already halves CPU against Tight and cuts bandwidth 14x. The
remaining cost is the scan/copy, and in H.264 mode most of it is wasted:
per-tile comparison exists to find damage for Tight, but a full-frame encode
does not care which tiles changed.

### Proposed Phase 3': encode the NVFBC buffer directly

While H.264 owns the output, skip x11vnc's tile compare and tile copy, and
point the encoder at NVFBC's own capture buffer rather than
`screen->frameBuffer`. The fork already captures only the served region, so the
geometry matches.

Still needed: the diff map for the gate (cheap, GPU-side, already enabled), and
a populated framebuffer at the moment of exit for the Tight repaint - so the
copy has to happen on the way out, not per frame.

No CUDA, no session switching, no loss of the diff map.


## 15. Entry threshold: 3 was too low, 8 is better (2026-08-21)

§11 derived ~3 screens/s from when Tight stops being affordable across the
*whole* screen. In use that turned out too eager: quarter-screen video is about
7.5 screens/s, so it tripped the gate and got encoded as a full 2560x1440 frame
when Tight only had to touch a quarter of the pixels.

Raised to **8 screens/s**, which sits above quarter-screen video and well below
a full-page scroll (20-60). Operator confirms scrolling is better. Now in the
wrapper as `-h264_enter 8`.

This is the dirty-*area* gap §14 flagged: rate alone does not distinguish "a
small region changing fast" from "the whole screen changing". Raising the
threshold papers over it adequately for this resolution, but a genuine area
term would be resolution-independent and would not need retuning if `-clip`
changes.

### Thresholds are tunable at runtime

`h264_enter`, `h264_exit`, `h264_bitrate` and `h264_fps` are exposed over the
remote-control interface (`remote.c`). The two thresholds are read on every
watch_loop tick so they apply immediately:

```
x11vnc -R h264_enter:8
x11vnc -Q h264_enter
```

Bitrate and frame rate are fixed at encoder open, so setting them closes the
encoder; the next tick that needs it reopens with the new value.

This matters more than it looks: finding the right crossover means trying values
against real scrolling, and restarting the service drops the session you are
judging with. Note that x11vnc's remote control goes through a single
`X11VNC_REMOTE` property on the display, so it cannot be aimed at a particular
server - it only works with exactly one x11vnc running, as
`bench/remote-check.sh` already documents.


## 16. Client version correction

The viewer is **TigerVNC 1.16.2**, not 1.15.0 as recorded earlier. Checked
against the v1.16.2 tag rather than master: `H264Decoder.cxx` differs only in
i18n and one error string, and `DecodeManager.cxx` has no changes to queueing or
pacing. Every Phase 0 finding stands - rect format, reset flags, per-rect
decoder contexts, and the SPS-first requirement.

Read the tag, not master, when checking client behaviour.


## 17. OPEN BUG: client freezes under sustained H.264 (2026-08-21)

Reproducible, four times, always the same way: run `bench/measure.py`, and a few
seconds into the `medium` scenario the viewer's picture stops updating and input
stops working. It recovers exactly when the gate hands back to Tight, ~60 s
later. Never seen in normal interactive use - only under sustained full-screen
change.

### Established by measurement

- **The server is healthy throughout.** `NVFBC stats` keep logging 18-22
  grabs/sec for the whole freeze, so watch_loop is cycling, not blocked.
- **Bytes keep leaving the socket** at 1.2-1.6 MB/s. Note this only proves they
  left x11vnc - sshd sits between the server and the viewer.
- **The stream is valid.** 640 access units captured with
  `rfbcheck.py --dump-h264` under the failing load decode with zero complaints:
  640 frames, Main profile, 2560x1440, one IDR with flags=1 at unit 1, sizes
  min 42 KB / median 84 KB / max 350 KB.
- **Decode is not expensive.** Software decode of that stream is 3.6 ms/frame
  on this CPU; the client has hardware Media Foundation.
- **`rfbcheck` never reproduces it** - it skips payloads instead of decoding, so
  it is an infinitely fast consumer.

### Three fixes that did NOT work

Each was plausible, each was wrong, each cost a freeze to disprove:

1. **Backpressure guard** - skip the frame when the socket queue exceeds 512 KB.
   No effect. `SIOCOUTQ` stays low because sshd drains eagerly into its own
   buffers, so the tunnel hides any real backpressure.
2. **Fit check** - refuse to start a write unless the whole access unit fits in
   the remaining send buffer, on the theory that `rfbWriteExact` was blocking
   watch_loop. Disproved directly: the NVFBC stats show watch_loop never
   stalled. (It did fix a real, separate problem - `systemctl restart` used to
   hang for a minute because a blocked write meant SIGTERM was never processed.
   Worth keeping on those grounds.)
3. **RFB flow control** - only send when `cl->requestedRegion` is non-empty, and
   clear it after. No effect on the freeze, and that is itself informative: the
   client *is* still requesting, so it is not falling silent. Correct on its own
   merits - libvncserver's encoders obey requests and ours did not - so kept.

### Current best hypothesis (untested)

`DecodeManager` gives each worker two buffers and the reader thread blocks in
`producerCond.wait()` when none are free. `H264Decoder` is `DecoderOrdered`, so
H.264 rects serialise onto a single worker, each costing a decode plus an
NV12->RGB32 conversion plus a 14.7 MB blit. If that is slower than the arrival
rate, buffers fill, the reader blocks, and a blocked reader stops the client
reading the socket *and* processing input - which is exactly the symptom.

It also explains why request-based pacing did nothing: TigerVNC pipelines its
update requests, so it keeps asking while its own queue backs up. Requests are
not a backpressure signal for this client.

`h264_fps` was lowered from 30 to 10 at runtime as a first test of this. **Not
yet verified** - it needs another bench run, and the operator has absorbed four
freezes already.

### If the hypothesis holds

The fix is adaptive rather than a fixed rate: measure what the client actually
consumes and back off. A fixed `-h264_fps` that is safe for a 2560x1440 client
on this hardware will be wrong for a different resolution or a faster viewer.

### If it does not hold

Stop changing the server. Get TigerVNC's own log from the Windows side
(`-Log *:stderr:100`) and find out what the client thinks is happening, because
three server-side theories in a row have now been wrong and the server-side
evidence is exhausted.


## 18. Freeze diagnosed and fenced (2026-08-21)

> **SUPERSEDED BY §20.** The mechanism proposed here - the viewer's socket loop
> never returning to its event pump - is **refuted**: the client acknowledges
> every frame within 500 ms throughout the freeze, which it can only do by
> exiting that loop. The fence *transport* described below is correct and
> retained; the *diagnosis* is wrong.

Read the client. TigerVNC **v1.16.2** source (tag `b555312`, not master) refutes
§17's buffer-exhaustion hypothesis and points at a different mechanism, and the
fix is RFB fence flow control implemented inside the fork.

### §17's hypothesis is wrong

- **`producerCond.wait()` on buffer exhaustion cannot happen here.**
  `framebufferUpdateEnd()` calls `decoder.flush()`, which blocks the reader
  until the decode queue drains. With one full-screen H.264 rect per
  FramebufferUpdate (§3), at most **one** decode buffer is ever in flight, so
  `freeBuffers` (8 of them: 2 × min(4,cores)) never empties. The plan reasoned
  about a single ordered worker but missed the per-update flush barrier in
  front of it.
- **Socket reads never block the UI thread either.** `FdInStream::readFd` does a
  `select()` with a **zero timeout**; a partial rect just makes `processMsg()`
  return false.

### What actually starves it

The viewer's socket handler `CConn::socketEvent` is **single-threaded** (the
FLTK main thread) and runs `while (processMsg())`, and it **corks the client's
output stream** for the whole loop (`cork(true)` before, `cork(false)` only
after it exits). Queued input events and the update-request/fence echoes sit in
that corked buffer. A server that keeps the socket continuously fed with
back-to-back full-frame updates keeps the loop from ever returning to FLTK's
event pump — so the picture stops advancing **and** input stops being sent,
clearing the instant motion stops and the socket drains. Matches the symptom.

Fix #3 (pace on `requestedRegion`) could not prevent it: **libvncserver 0.9.15
has no continuous-update and no fence support** (`nm`/headers confirm), so the
client is classical request-driven but pipelines requests **one deep** — it asks
for frame N+1 at the *start* of reading frame N — which keeps the server exactly
one frame ahead, and one frame ahead is enough to keep the loop perpetually fed.

### The fix: RFB fence flow control (in the fork)

`src/h264/h264_stream.c`: after each H.264 frame, send a `ServerFence`
(msg 248, `fenceFlagRequest`) and **withhold that client's next frame until it
echoes** — the one acknowledgement that survives sshd and proves the client's
loop drained the frame and came back. That forces the socket to run dry each
round, so the viewer returns to its event pump every frame, and it self-adapts
the rate to the client's true throughput (the adaptive rate §17 asked for).

Feasible entirely in the fork, all verified against LibVNCServer-0.9.15 +
TigerVNC v1.16.2 source:

- The viewer advertises `pseudoEncodingFence` (-312), so our
  `enablePseudoEncoding` learns which clients can be fenced.
- libvncserver routes the unknown `ClientFence` (msg 248) to our extension's
  `handleMessage` (only the type byte is pre-read; we read the rest and return
  TRUE, else the library would close the client).
- Fences ride the send ban and the same socket, in order, exactly like the
  H.264 rects.
- A **timeout** (`-h264_fence_timeout`, default 500 ms) degrades a lost echo to
  send-anyway, so a dropped fence can never freeze the stream the other way.
- The tick **holds** before encoding while gated rather than encode-and-drop, so
  the stream stays clean P-frames (one per ack) instead of an all-IDR storm.

New knobs: `-h264_nofence`, `-h264_fence_timeout N`, and remote-control
`h264_fence:0|1` / `h264_fence_timeout:N` for live A/B without a restart.

### Validated, and what is still open

Validated on a throwaway port 5906 (test-file replay, X11 capture, production on
5900 untouched) with `bench/rfbcheck.py --h264 --fence` (new `--fence` echoes
msg 248; `RFBCHECK_FENCE_NOECHO=1` withholds echoes):

- ungated the server floods at **~94 rects/s**; with fences echoed it sends
  **exactly one frame per echo** (e.g. 101/101, 98/98) and never runs ahead;
- with echoes withheld it floors at the **timeout rate ~2/s** and does not
  freeze;
- no desync, no client close, stream valid throughout.

Still open — because `rfbcheck` is a synchronous consumer it cannot reproduce
the single-threaded-viewer freeze, so this proves the **pacing is correct**, not
that it **cures the freeze**. Confirm that on the real client:

1. Live test on **5906** (not 5900) with `-h264`, the real Windows TigerVNC, and
   real motion — watch for the freeze and dump the stream with
   `rfbcheck.py --h264 --dump-h264` to confirm P-frame-dominant, few IDRs.
2. If anything still stalls, capture the client's own view at last:
   `vncviewer ... -Log '*:stderr:100'` on Windows (§17's mandated step).
3. Deploy to 5900 only after 1–2 look right (README install one-liner).

### Live-encoder validation, and one follow-up it exposed (2026-08-21)

The §18 numbers above were the test-file path. Repeated against the **live
NVENC path** via `bench/h264-testserver.sh` (port 5906, `ENTER=0.05` so a small
320x240 load trips the gate while staying at ~1.25 screens/s - far below
production's 8, so the operator's 5900 session stayed on Tight throughout):

| consumer | AUs | fences | IDR share |
|---|---|---|---|
| `--slow 40` (≈25 fps ceiling) | 101 | 100 echoed, 1:1 | **16%** |
| no `--slow` (fast) | 116 | 115 echoed, 1:1 | **2%** |

All 101 access units of the slow run decode with **zero complaints** (Main
profile, 2560x1440), and ffprobe's frame types match the wire flags exactly -
85 P to 16 I. So fence pacing preserves inter-frame prediction; it does not
degrade into an IDR storm.

**Follow-up (not changed yet, deliberately).** The IDR share tracks consumer
speed, which locates it in the *request* check from §17 fix 3, not in fencing:
when the tick outruns the consumer's requests, `h264_broadcast` finds
`requestedRegion` empty, refuses **after** the frame was already encoded, and
`need_idr` then forces the next delivered frame to be a full IDR (~350 KB vs an
84 KB median P). The clean repair is the same shape as the fence fix - extend
the hold to *before* the encode, so a frame is never produced for a client that
cannot take it. It is left out of this change on purpose: the live client test
should vary one thing at a time, and TigerVNC pipelines its requests one deep,
so a real viewer should hit this far less often than a strictly synchronous
`rfbcheck --slow` does. Measure it on the real client before acting.

### `bench/h264-testserver.sh` gotchas, now handled

The first version resolved `../x11vnc/src/x11vnc` against the **caller's** cwd,
so it only ran from inside `bench/` and died with "No such file or directory"
when invoked as `bench/h264-testserver.sh`. It now resolves against its own
directory and preflights: refuses port 5900 outright, reports a missing binary
with the build command, refuses a port already in use, and checks the display
is openable before launching. `NOFENCE=1` runs the unpaced "before" leg and
`ENTER=N` overrides the gate threshold, so both legs of the A/B are command-line
choices - **do not use `-R` for this while production is up**, since remote
control goes through one `X11VNC_REMOTE` property and needs exactly one server.

### The `medium` scenario sits on the threshold, and the test rig must use NVFBC (2026-08-21)

Two things that make or break a reproduction of §17, both found by trying:

**1. Capture method decides whether the gate can trip at all.** The gate's metric
is dirty area per second, which scales with how often the server scans. Under
X11 capture the scan rate is low and damage coalesces - one dirty region per
scan instead of 60/s - so `medium` measures far below 8 screens/s, H.264 never
engages, and the bench silently runs pure Tight. A full `measure.py` run against
such a server reported no freeze and meant nothing: the picture kept updating
(90 KB/s, Send-Q 0, client never dropped) simply because it was Tight all along.
The test server needs `NVFBC=1` (i.e. `-nvfbc -nvfbc_nocursor -nvfbc_push`).

A second NVFBC session **does** work alongside the live server's: the throwaway
initialises fine and tracks the same output DP-4. That is consistent with
`bench/nvfloor`, which has always opened one next to a running x11vnc. §14's
"two sessions are refused" applies within a single NvFBC client handle, not
across processes.

**2. `medium` is a knife-edge case for `-h264_enter 8`.** 960x540 is 0.1406 of
2560x1440, so the measured rate is 0.1406 x the achieved capture fps:

| achieved fps | measured rate | vs threshold 8 |
|---|---|---|
| 60 (the original freeze runs, logged 8.1) | 8.44 | trips |
| 53 (measured here) | 7.45 | **does not trip** |

So whether `medium` exercises H.264 at all depends on a few fps of capture rate.
This is §15's raised threshold behaving exactly as designed - it was moved to 8
specifically so quarter-screen video would stay on Tight - but it makes `medium`
an unreliable reproduction. Use `full` (2560x1440 = 1.0 screens/frame, 20-30
screens/s, unambiguously above the threshold) as the load that guarantees
sustained H.264, and read `medium` as a maybe.

## 19. The fence fix holds under the failing load (2026-08-21)

> **WRONG - SEE §20.** The operator was not watching this run. When observed,
> the viewer froze in the identical pattern. The "liveness" argument below is
> invalid: inbound bytes do not prove the client's loop ran (see §20).

`measure.py --port 5906` against the fenced build, NVFBC push, real TigerVNC
1.16.2 over the real tunnel. The gate entered H.264 **5 s into `medium`** and
held for **61 s** through all of `full`, exiting at 22:48:21 - i.e. §17's exact
failure window ("a few seconds into medium ... recovers ~60 s later"),
reproduced deliberately.

| scenario | pre-fence (5900, 21:03) | fenced (5906) | CPU pre → now |
|---|---|---|---|
| medium | 1559.8 KB/s | **975.7 KB/s (-37%)** | 43.7 → 41.5 |
| full | 1236.8 KB/s | **996.0 KB/s (-19%)** | 85.2 → 91.9 |

Fewer bytes for the same load, with CPU headroom to spare at `medium` (41.5%),
is what pacing to the consumer looks like. (`idle`/`small` bytes are up, but
those legs are Tight-only and the desktop was not quiescent - this session's own
terminal output was live damage. Do not read them as a regression.)

### The echo rate measures the client's event loop, from the server

The freeze was never visible in bytes-sent, because bytes left x11vnc during the
freeze too. But fences give a signal that bytes cannot: **TigerVNC writes its
fence echo into a corked output stream** that only flushes when `socketEvent`'s
`while (processMsg())` loop exits (§18). An echo arriving is therefore proof
that the viewer returned to its event pump - the precise thing a freeze
prevents.

Measured over 8 s of sustained full-screen H.264:

```
server -> viewer : 1028 KB/s
viewer -> server :  290 B/s   = ~12.6 round-trips/s   (13 B echo + 10 B request)
                                 1028 KB/s / 84 KB    = ~12.2 frames/s
```

Two independent numbers agreeing, and both incompatible with a stalled loop: a
client that never uncorks never echoes, so the server would be pinned to the
500 ms timeout - 2 fps, ~170 KB/s. Sustaining 1028 KB/s at 2 fps would need
514 KB access units, above the 350 KB maximum §17 ever recorded. The server was
running on echoes, not timeouts, and the viewer was alive throughout the load
that froze it 4/4.

**Still worth having:** the operator's own visual confirmation, and a run with
`NOFENCE=1` to show the freeze returning on the same rig - a negative control
this evidence does not replace.

### Second run, operator watching (2026-08-21)

Repeated because the first run went unobserved. Same rig, same protocol; the
gate entered at **8.1 screens/s** - the identical figure §17's freeze logged -
5 s into `medium` (22:55:14) and held **62 s** through `full` (exit 22:56:16).

| | run 1 | run 2 |
|---|---|---|
| medium | 975.7 KB/s, 41.5% | 954.1 KB/s, 40.9% |
| full | 996.0 KB/s, 91.9% | 997.6 KB/s, 92.6% |
| liveness during H.264 | 1028 KB/s out, 290 B/s in, ~12.6 rt/s | 1038 KB/s out, 290 B/s in, ~12 rt/s |

Reproducible to within 2%. No `backed up`, no fallback, no disconnect in either
run, and the viewer's echo round-trip rate held at ~12/s throughout - the loop
kept returning to its event pump under exactly the load that froze it 4/4.

## 20. Fences are not the cure either - and the client is not stalled (2026-08-21)

Observed by the operator, twice, with the same result each time: the viewer
freezes a few seconds into `medium` and recovers when the gate hands back to
Tight, exactly as §17 described. **Fence flow control does not fix it.** That is
theory number five.

### Two of my own claims, corrected

**1. Inbound bytes never proved the client was alive.** §19 argued that fence
echoes only flush when TigerVNC's socket loop exits and uncorks, so ~12
round-trips/s meant a live event loop. Wrong: `BufferedOutStream::flush()`
returns early **only while under 1024 bytes are buffered**

```c
if (corked && emulateCork && ((ptr - sentUpTo) < 1024))
    return;
```

so a corked stream still writes once ~1 KB accumulates, loop or no loop. The
conclusion was built on an unchecked premise and happened to be measuring an
artifact.

**2. Client saturation is refuted.** The theory: the fence paces the server to
the client's completion rate, driving it at 100% duty cycle with nothing left
for rendering or input. Tested by dropping `-h264_fps` 30 -> 5, roughly 40% of
measured capacity. **It froze identically.**

### The counted evidence, which is what makes this useful

The build now logs delivery counters (`h264 stats:`) rather than inferring rates
from bytes divided by an assumed access-unit size. Through the entire frozen
minute at 5 fps:

```
h264 stats: 4.7 fps sent, 2.2 MB/s, 47 echoes, 0 timeouts, 1 held, 0 unrequested
```

- **Frames sent and echoes match 1:1, with zero fence timeouts**, continuously,
  while the screen was frozen and input dead.
- At 4.7 fps the client must **exit** its `processMsg` loop between frames -
  there is nothing left to read for ~200 ms - which uncorks and flushes. Prompt
  echoes are therefore proof the loop *is* cycling.
- Holds fell to 1-3 per 10 s: the server was almost never waiting on the client.

**So the client's protocol layer is healthy and responsive throughout a freeze
in which it displays nothing and accepts no input.** Whatever is broken is
downstream of message processing - the decode/render path - not the socket loop
(§18) and not overall throughput (this section).

That is consistent with §1's warning, which is worth re-reading now: encoding 50
has **no diagnostics**, `ProcessInput`/`ProcessOutput` failures in
`H264WinDecoderContext` are swallowed on purpose ("Silently ignore errors,
hoping its a temporary encoding glitch"), and a decoder that stops producing
output renders a frozen or black rectangle while the protocol layer looks
perfect. §17's "the stream is VALID" was established with **ffmpeg offline**,
never with Media Foundation on Windows, which is the decoder that matters and
the one with the documented silent-failure modes.

Note also what varies with the frame rate: rate control is CBR at 20 Mbps, so
5 fps produced **~480 KB** access units against ~83 KB at 30 fps. Frame *size*,
not frame rate, is the thing that grew - and it froze at both.

### Do not change the server again until the client has been read

§17 set this rule after three failed theories; there are now five, two of them
mine. The next step is not another server-side patch. It is the viewer's own
log, which no theory so far has had:

```
vncviewer.exe -Log *:file:100 ...        # writes C:\temp\vncviewer.log
```

(`vncviewer.cxx:658` registers the Windows file logger at that fixed path;
`*:stderr:100` is useless for a Windows GUI build.) Reproduce the freeze with it
running and read what the client says about `H264` / `DecodeManager` /
`CConnection` during the frozen minute.

A zero-cost server-side companion measurement, needing no rebuild: during the
freeze, mash keys and watch inbound bytes on the client socket. Baseline traffic
is ~23 B per frame (13 B fence echo + 10 B update request); key events on top of
that prove the input path is live and localise the fault to display only.

### What the fence work is still worth

It is not wasted, but it must be described accurately: correct RFB flow control
that libvncserver 0.9.15 cannot do (it has neither fences nor continuous
updates), a 39% bandwidth reduction at `medium` for identical load, and - now
that it counts acks - **the instrument that measures what the client actually
consumes**. It is a good mechanism and the wrong cure. Keep it behind
`-h264_nofence`; do not present it as the fix.

### Input reaches the server throughout the freeze (2026-08-21)

Measured with no server change at all: inbound bytes on the client socket, in
5 s buckets, while the operator wiggled the mouse during the frozen window.
Idle baseline is ~107 B/s (update requests plus fence echoes).

```
t+  5s   109 B/s          gate has just entered H.264
t+ 10s   284 B/s   <-- mouse
t+ 15s   268 B/s   <-- mouse
t+ 20s   270 B/s   <-- mouse
t+ 25s   225 B/s   <-- mouse
t+ 30s   242 B/s   <-- mouse
...
t+ 60s   254 B/s   <-- mouse
t+ 65s   226 B/s   <-- mouse
```

Pointer events are 6 bytes each, and they arrive in a steady stream 2-3x above
baseline **during the minute the screen is frozen**. Server-side over the same
window: `4.8 fps sent, 47 echoes, 0 timeouts` in five of seven intervals.

So during a "freeze" the viewer is:

- **reading** our H.264 frames and acknowledging every one within 500 ms,
- **reading the mouse** and writing pointer events to the socket,
- **flushing** that output on schedule,
- and **displaying nothing**.

Message loop, input handling and socket writing are all alive. The operator's
"input stops responding" is the *appearance* of dead input caused by a dead
display: the pointer really does move on the X server and the desktop really
does respond - the client simply never paints the result. Nothing about this is
compatible with a stalled client, a starved event loop, or a throughput limit.

**The fault is isolated to the client's H.264 display path.** Which is precisely
where §1 warns there are no diagnostics and where `H264WinDecoderContext`
swallows `ProcessInput`/`ProcessOutput` failures by design.

One anomaly worth keeping: the interval covering the `medium` -> `full`
transition read `3.6 fps sent, 31 echoes, 5 timeouts, 115 held` - the only
interval with timeouts, i.e. the client briefly stopped acking when the whole
screen started changing. Everything else was clean.

### The client's H.264 path has no logging at all (2026-08-21)

Checked before reading any client log, so expectations are right: **not one
`LogWriter` exists in any H.264 file** of TigerVNC 1.16.2 -
`H264Decoder.cxx`, `H264DecoderContext.cxx`, `H264WinDecoderContext.cxx`,
`H264LibavDecoderContext.cxx`. The only outputs are `throw`s during *context
construction* (MF init failure, codec not found); once running, `decode()` logs
nothing and swallows every `ProcessInput`/`ProcessOutput` failure by design.

So `-Log *:file:100` **cannot** produce an H.264 decode error, however verbose.
Its value is what it rules out - a decoder exception, a reconnect, a mid-stream
pixel-format or encoding change, repeated decoder construction - plus whatever
`CConnection` and `DecodeManager` say. If it is silent through the freeze, that
is itself a result: it eliminates every failure mode the client can report and
leaves only the one it cannot.

Practical note: the Windows file logger writes to a **hardcoded**
`C:\temp\vncviewer.log` (`vncviewer.cxx:658`) and `Logger_File::write()` opens it
lazily with `fopen(...); if (!m_file) return;` - so if `C:\temp` does not exist,
which is the Windows default, logging fails **silently**. Create the directory
first. The file is also rotated to `.bak` on every viewer start, so capture the
log before reconnecting.

## 21. Two real encoder defects, found on the wire (2026-08-21)

The client log (level 100, `C:\temp\vncviewer.log`) was **silent through the
entire frozen minute** - no exception, no reconnect, no format change, no
decoder re-creation. As §20 predicted, it cannot report an H.264 fault. But its
closing stats were decisive:

```
DecodeManager: H.264: 288 rects, 1,06168 Gpixels, 136,426 MiB
```

288 x 2560x1440 = 1.0617 Gpixels exactly, and the server sent ~287 frames. **The
client received, queued and counted every single frame** and painted none of
them. A screenshot taken mid-freeze shows TigerVNC's own stats overlay reading
`3 upd/s, 6.14 Mpix/s, 15.97 Mbps` with a regular sawtooth - and the overlay was
*animating*, so FLTK's draw cycle was running. Data in, decode counted, window
redrawing, desktop image static.

That isolates the fault to `H264WinDecoderContext::decode()` never reaching
`pb->imageRect()` - i.e. `decoded` never becoming true. Dumping the live stream
found why.

### Defect 1: the frame that resets the decoder is not an IDR

`h264_enc_frame()` requests a keyframe with `pict_type = AV_PICTURE_TYPE_I`, but
**`h264_nvenc` defaults `forced-idr` to false**, so that request yields a plain
I slice. Confirmed by dumping the real stream (`rfbcheck --dump-h264`) and
parsing NAL types:

```
AU  1  500051 B  flags=1   SPS+PPS+I-slice(NON-IDR)   <== RESET_CONTEXT
AU  2..11        flags=0   SPS+PPS+P
AU 12            flags=0   SPS+PPS+IDR                (GOP boundary)
```

`H264_RESET_CONTEXT` makes `H264Decoder::decodeRect` **destroy the decoder
context and construct a new one**. A fresh H.264 decoder cannot start on a
non-IDR picture: Media Foundation returns `MF_E_TRANSFORM_NEED_MORE_INPUT`,
`decoded` stays false, `imageRect()` is never called, and the viewer paints
nothing - silently, with no diagnostic, exactly the §1 failure mode. It stays
stuck until the next GOP IDR, which at `gop_size = fps * 10` was **10 seconds**
away, and any further refusal re-arms `need_idr` and resets it again.

Fix: `av_opt_set(ctx->priv_data, "forced-idr", "1", 0)`, plus `gop_size` cut
from `fps*10` to `fps*2` so a missed start costs 2 s rather than 10.

**Scope, stated honestly:** this bites whenever the gate enters H.264 with the
encoder *already open*. It does **not** explain the observed 23:30 freeze, where
the log shows the encoder was opened fresh at entry and a fresh encoder's first
frame is an IDR regardless. So this is a genuine, verified defect and a
guaranteed silent-freeze mechanism - but not proof that the freeze is cured.

### Defect 2: strict CBR pads every frame with filler

Same dump, counting NAL type 12:

```
AU 1 (500,051 B):  slice  19,474 B  +  FILLER 480,507 B  (96.1%)
AU 2 (500,051 B):  slice 305,745 B  +  FILLER 194,236 B  (38.8%)
AU 3 (500,051 B):  slice 247,432 B  +  FILLER 252,549 B  (50.5%)
```

Every access unit was a constant 500,051 bytes - exactly 20 Mbps / 5 fps - with
the remainder filler. `rc=cbr` makes NVENC transmit padding to hold the bitrate
whatever the content. That is why lowering `-h264_fps` *raised* bandwidth, and
it defeats §0's entire bandwidth motivation on a 9.9 Mbps uplink.

Fix: `rc=vbr` with the same `rc_max_rate` ceiling.

### Verified after both fixes

Same test, two gate entries:

| | before | after |
|---|---|---|
| RESET_CONTEXT frame, entry 1 | `I-slice(NON-IDR)` | **IDR** |
| RESET_CONTEXT frame, entry 2 | `I-slice(NON-IDR)` | **IDR** |
| filler | 39-96% per AU | **0.0%** (0 of 1,715,229 B) |
| mean access unit | 500,051 B | **34,304 B** |
| static-screen frames | 500,000 B | **316-484 B** |

A 14.6x drop in mean frame size, and a motionless screen now costs ~320 bytes a
frame instead of half a megabyte.

### Method note worth keeping

§17 declared "the stream is VALID" because 640 access units decoded cleanly in
**ffmpeg**. ffmpeg happily starts on a non-IDR I-frame; Media Foundation does
not. Validating a stream with a lenient decoder says nothing about a strict one.
Check the **structure** - is the frame that follows a context reset actually an
IDR - not merely whether some decoder accepts it.

## 22. ROOT CAUSE: the client cannot display 2560x1440 H.264 at all (2026-08-22)

Not a freeze. Not load. Not pacing, not fences, not capture, not the gate.
**The viewer never paints a 2560x1440 H.264 rect, at any rate, from any server.**

### How it was isolated

`bench/h264serve.py` serves a pre-encoded Annex-B file as encoding 50 - no
x11vnc, no gate, no live encoder, no fences, no Tight. Serving 90 s of
2560x1440 to the real client:

```
sent 681 frames ... ended: client closed the connection
```

681 frames delivered and consumed, and the screen stayed **black the whole
time**. The access units were structurally perfect - `00 00 00 01 67` (4-byte
start code, NAL type 7) then SPS+PPS+SEI+SEI+IDR, exactly what §1 requires.

Then a resolution sweep, same tool, same wire format:

| size | pixels | encoder | result |
|---|---|---|---|
| 1280x720 | 0.92 Mpx | NVENC | plays |
| 1600x1200 | 1.92 Mpx | NVENC | plays |
| 1920x1080 | 2.07 Mpx | NVENC | plays |
| 2048x1152 | 2.36 Mpx | NVENC | plays |
| **2560x1440** | **3.69 Mpx** | **NVENC** | **BLACK** |
| **2560x1440** | **3.69 Mpx** | **libx264** | **BLACK** |

Two independent encoders produce the same black screen at 2560x1440, and
everything at or below 2048x1152 plays. The ceiling is in the **client**.

### What this reinterprets

Every earlier observation now reads differently, and consistently:

- The "freeze" was never a freeze. H.264 was painting **nothing at all**, from
  the first frame of every gate entry. What looked like a frozen desktop was the
  last **Tight** frame, left on screen because §11's design suppresses Tight
  while H.264 owns the output.
- "Recovery ~60 s later, exactly when the gate returns to Tight" - because the
  gate exit calls `mark_rect_as_modified()` for the whole screen. Nothing
  recovers; Tight simply paints over the stale image. The operator asked what
  brings the encoder back to life, and the answer is that nothing does.
- The client acknowledging every frame, counting all 288 rects, keeping its
  event loop alive and its input path working: all consistent. It received and
  parsed everything perfectly. It just could not display it.
- "Only under sustained full-screen change" - sustained motion is merely what
  holds the gate in H.264 long enough to notice. Brief excursions (a scroll)
  ended before the missing paint was obvious, which is why §13's interactive
  testing passed.

### What was NOT the cause (seven refuted theories)

Socket-backlog guard; write-fit check; RFB request pacing; DecodeManager buffer
exhaustion; socket-loop starvation; fence pacing; client saturation. Two of
those were mine, and I called the fence fix "validated" against a run nobody
watched - it was overturned the moment the operator looked at the screen.

### Still worth keeping from the wrong turns

- **forced-idr** (§21): `h264_nvenc` defaults it false, so the access unit
  carrying `H264_RESET_CONTEXT` was a non-IDR I-slice. Real defect, fixed.
- **VBR instead of CBR** (§21): strict CBR padded every frame to the bitrate
  with filler NALs - 96%/39%/50% measured. Mean AU 500,051 B -> 34,304 B, and a
  static screen 500,000 B -> ~320 B per frame. Real defect, fixed.
- **Fence flow control** (§18): correct RFB flow control the stock library
  cannot do, -39% bandwidth, and the counters that finally replaced inference
  with measurement. Not the cure; keep behind `-h264_nofence`.
- **Delivery counters**: added because rates derived from bytes / assumed frame
  size produced a confidently wrong conclusion. Count events, do not derive them.

### Method lessons

1. **§17's "the stream is VALID" was tested with ffmpeg.** ffmpeg is lenient;
   Media Foundation is not, and it reports nothing. Validate structure against
   the strict decoder's rules, and validate end-to-end against the real client.
2. **Remove your own code from the experiment early.** `h264serve.py` existed
   since Phase 0 and would have isolated this in minutes at any point.
3. **A measurement that agrees with your theory is not evidence until you have
   checked what else could produce it.** Inbound fence bytes "proved" liveness;
   they were a 1 KB buffer flushing on its own schedule.

## 23. FIXED: tiled H.264 (2026-08-22)

Confirmed by the operator on the real client: **the picture plays through both
gate entries**, tracking the load instead of freezing. Same bench, same
`medium`+`full` scenarios, same rig that froze 4/4 for the previous week.

### The change

`h264_encode.{c,h}` becomes multi-instance (`h264_enc_t *`, one per tile), and
`h264_stream.c` splits the served region into as few horizontal bands as keep
each under `H264_MAX_TILE_PIXELS`, giving each its own encoder, its own IDR
chain and its own rect. All bands go out in **one FramebufferUpdate** with
`nRects = ntiles`.

For 2560x1440 that is 2 tiles of 2560x720. Verified on the wire:

```
AU   bytes flags       rect  layout
 1    9488     1   2560x720  SPS(2560, 720)+PPS+SEI+SEI+IDR
 2    9350     1   2560x720  SPS(2560, 720)+PPS+SEI+SEI+IDR
 3   11190     0   2560x720  SPS(2560, 720)+PPS+SEI+P
 4   11165     0   2560x720  SPS(2560, 720)+PPS+SEI+P
```

Bands, not columns, because framebuffer rows are contiguous: each encoder is
pointed at `screen->frameBuffer + y * stride` with the existing stride, so the
zero-copy property of the single-rect path is preserved. Tile count is derived
from the geometry, so a `-clip` change or a different screen adapts by itself,
and band height is rounded up to a multiple of 16 to keep every tile a whole
number of macroblock rows and avoid SPS frame-cropping entirely.

### Measured, tiled build

| scenario | KB/s | CPU |
|---|---|---|
| medium | 299.5 | 37.0% |
| full | 155.9 | **122.0%** |

Delivery through both entries: `4.7-4.8 fps sent, 47-49 echoes, 0 timeouts`.

**CPU is the cost of this fix and should not be glossed over:** `full` went from
~90% to 122% because there are now two NVENC sessions instead of one, each with
its own submission and its own host->device path. Worth measuring against
`-h264_fps 30` and a preset sweep (§8 Phase 4) before deploying, and worth
remembering that a screen needing 3 or 4 tiles pays proportionally more.

### The limit is the client's, and it is not ours to tune

`H264_MAX_TILE_PIXELS` is exposed as `-h264_tile_pixels` for experimentation,
but it is **not a tuning knob** - it is a measured property of TigerVNC's
Windows decoder (§22). A different viewer, or a TigerVNC built against a
different Media Foundation, may sit elsewhere. The honest position: we tile to
2,359,296 px because that is what this client was measured to accept, and any
client whose limit is lower will show the same silent black rect with no way for
the server to detect it.

That is the deeper problem with encoding 50 and it is unchanged by this fix:
**the protocol gives the server no way to learn that the client failed to
display a rect.** Everything in §17-§22 followed from that single gap.

### Deployed to 5900, full bench (2026-08-22)

Tiled build installed as `/usr/bin/x11vnc` (`d2ca7a61…`), production flags
unchanged - `-clip 2560x1440+0+0` makes tiling engage by itself. Same bench,
same client, against the pre-fix `hybrid-flowctl` run of 21:03:

| scenario | KB/s before | KB/s after | | CPU before | CPU after |
|---|---|---|---|---|---|
| idle | 5.8 | 25.3 | *(uncontrolled)* | 2.3% | 3.4% |
| small | 6.3 | 6.4 | +2% | 36.0% | 34.8% |
| medium | 1559.8 | **176.7** | **-89%** | 43.7% | 42.3% |
| full | 1236.8 | **371.5** | **-70%** | 85.2% | **130.3%** |

`idle` is the uncontrolled scenario and this session's own terminal output was
live damage on the desktop; do not read it as a regression.

**Bandwidth is now well inside the link.** `medium` fell 8.8x. Against the Tight
baseline of §14 (25.79 MB/s under motion) the hybrid now costs ~0.36 MB/s at
`full` - roughly 70x less, where the original H.264 figure was 12x less.

**The fence gate turned into the adaptive rate §17 asked for.** At a 30 fps
target the counters read `11-12 fps sent, 0 timeouts, 279-306 held` per 10 s:
two thirds of encode attempts are declined because the client has not yet
acked, so the server settles at the ~11-12 fps this client can actually take
for 2560x1440 in two tiles. Nobody configured that number; it is measured every
frame.

**CPU is the open cost.** `full` went 85.2% -> 130.3%, and the capture model
attributes only 13.3 points to capture, so the rest is scan/copy plus two NVENC
sessions. That makes §14's Phase 3' the obvious next work: while H.264 owns the
output, x11vnc's per-tile compare and copy exist only to find damage for Tight,
which is not being used. Skipping them, and pointing the encoders at NVFBC's
own buffer, targets exactly the part of the 130% that is now waste.

## 24. Phase 3' implementation brief — encode from the NVFBC buffer (2026-08-22)

Written to be picked up cold in a new session. Everything below was verified
against the tree at `fb6536c`.

### Why: measured attribution, not assumed

From the deployed run (`bench/results/tiled-prod-20260822-005642.json`, `full`):

```
total CPU               130.3%
NVFBC capture            13.3%   from the machine's measured NVFBC floor
NVENC encode, 2 tiles     9.3%   measured standalone, see below
UNACCOUNTED             107.7%
```

The encode figure is measured, not modelled: encoding 2560x720 with the
production settings costs **3.87 ms of CPU per frame** (differential of two
ffmpeg runs, source-only vs source+encoder, 150 frames), so both tiles at the
~12 fps this client accepts come to ~9% of one core. `nvidia-smi` reports the
**encoder block at 32%** during that run. The GPU is doing the encoding; the
CPU is not where the video work happens.

### Correction to §14: the compare is already gone

§14 said the residual was "scan/compare/copy" and that "per-tile comparison
exists to find damage for Tight". The comparison is in fact **already skipped**
whenever NVFBC's diff map is active: `scan.c:3605-3619` calls
`nvfbc_mark_tiles_from_diffmap()`, which fills `tile_has_diff[]` from the GPU
diff map and sets `nvfbc_scanned = 1`, bypassing `scan_display()` entirely.

**The target is the copy, and there are two of them per dirty tile:**

1. `copy_tile()` -> `copy_image(tile_row[nt], ...)` (`scan.c:1913`) copies
   NVFBC's buffer into an XImage (`xwrappers.c:525-541`).
2. `copy_tile()` then memcpy's that XImage into `main_fb`.

At `full` every tile is dirty, so that is 2 x 14.7 MB per captured frame; at
27.4 captured fps, **~800 MB/s of CPU memory traffic**. That is the 107.7%, and
in H.264 mode the destination is only read by our own encoder.

### The change

While `h264_owns_output()`, skip the tile copies and point each tile's encoder
straight at NVFBC's buffer.

Anchors:

| what | where |
|---|---|
| NVFBC's own BGRA system buffer | `xwrappers.c:63` `nvfbc_frame_buffer`, refreshed at `:300`/`:310` |
| stride | `xwrappers.c:498` `src_stride = width * 4` |
| served -> frame offset | `xwrappers.c:74`, set at `:238` (`nvfbc_src_dx/dy`) |
| access lock | `NVFBC_LOCK` / `NVFBC_UNLOCK` |
| damage suppression guard | `scan.c:1778` in `mark_rect_as_modified()` |
| tile copy to skip | `scan.c` `copy_tiles()` / `copy_tile()` |
| tick ordering | `screen.c:4853-4863`, inside the send ban |
| current encoder input | `h264_stream.c`, `screen->frameBuffer + y * paddedWidthInBytes` |

Suggested new accessor in `xwrappers.c`, so `h264_stream.c` never touches NVFBC
internals:

```c
/* Base of the SERVED region inside NVFBC's capture buffer, or NULL if NVFBC
   is not active or the last grab failed.  Valid until the next grab. */
const uint8_t *nvfbc_served_pixels(int *stride);
```

Each tile then encodes from `base + tile_y * stride`, exactly as it does today
from the framebuffer - the band layout does not change.

### Traps, in the order they will bite

1. **The cursor disappears.** `h264_frame_tick()` deliberately runs *after*
   `check_cursor_changes()` (`screen.c:4856-4862`), which draws the soft cursor
   into the framebuffer - so the cursor is in the H.264 stream today. NVFBC
   captures with `-nvfbc_nocursor`, so encoding from its buffer loses the cursor
   during motion. Decide explicitly: composite it into a scratch copy of the
   affected band, leave it to the RFB cursor pseudo-encoding, or accept it.
   Do not discover this from a bug report.
2. **The framebuffer goes stale, and the exit repaint needs it.** Leaving H.264
   marks the whole screen for Tight (`h264_update_gate()`). If the copies were
   skipped for the whole H.264 period, `main_fb` holds pixels from the moment
   the gate engaged, and Tight will faithfully repaint that stale image. Call
   `copy_screen()` on the exit path *before* `mark_rect_as_modified()`. This is
   the same class of bug as §13's swallowed exit repaint.
3. **Only skip when H.264 owns every client.** `h264_owns_output()` is already
   exactly that condition (`exclusive`); gate the skip on it and nothing else.
4. **Honour `nvfbc_src_dx/dy`.** They are 0 in this deployment because `-clip
   2560x1440+0+0` matches tracked output DP-4, so a bug here will not show on
   this machine and will corrupt any other geometry.
5. **Keep the framebuffer path working.** Without NVFBC (X11 capture) the tick
   must still encode from `screen->frameBuffer`. Two sources, chosen per tick.
6. **Buffer lifetime.** `nvfbc_frame_buffer` is overwritten by the next grab.
   The tick runs in `watch_loop` after the grab and inside the send ban, so it
   is safe today - state that assumption in a comment rather than relying on it
   silently.
7. **`-nvfbc_direct`.** Direct capture may change buffer semantics; check
   `nvfbc_last_frame.is_direct_capture` before assuming.

### Also in scope: a quality target (`cq`)

The operator reports text is "a bit soft during motion" - acceptable, but worth
fixing while the encoder is open on the bench. VBR currently produces only
**1.6-3.2 Mbps against the 20 Mbps ceiling**, so roughly 10x of headroom is
unspent; quality costs GPU, not CPU, and barely moves the link at these rates.

Add `-h264_cq N` -> `av_opt_set(ctx->priv_data, "cq", ...)` alongside `rc=vbr`
in `h264_enc_open()`. Lower is better quality; start around 19-23 and sweep.
Expose it over remote control next to `h264_bitrate`/`h264_fps` in `remote.c`
(it is fixed at encoder open, so it must call `h264_encoders_reset()`), because
judging softness needs live A/B against real content, not a restart per value.

### Verification

- `bench/measure.py --port <test port> --duration 30` and compare `full`
  against the 130.3% baseline. Expect capture ~13% + encode ~9% + a small
  residual; anything near 50% means a copy is still happening.
- Confirm the picture still tracks the load on the real client, and that the
  **gate exit repaint is crisp** - that is trap 2 showing up.
- `rfbcheck.py --h264 --fence --dump-h264` plus `nalscan` to confirm the wire
  structure is unchanged: 2 tiles of 2560x720, real IDR on every
  `H264_RESET_CONTEXT` frame, no filler.
- Watch `h264 stats:` for `timeouts` staying 0; a rise means the encoder input
  changed under the client.

### Done when

`full` CPU is materially below 130%, the picture is unchanged on the real
client, the exit repaint is clean, and the cursor decision is made and written
down.

## 25. Encoder quality knobs swept: none of them help (2026-08-22)

`-h264_cq`, `-h264_preset` and `-h264_tune` were added so the encoder could be
tuned without a rebuild. Swept against real content, **all three are inert for
picture quality, and the defaults are already the best available.** Recorded so
nobody spends another evening on them.

### Live, on the deployed server (full-screen blit load, 30 s each)

| cq | KB/s delivered | Mbps | CPU |
|---|---|---|---|
| off | 2724.6 | 21.8 | 51.0% |
| 27 | 576.0 | 4.6 | 53.0% |
| 23 | 798.6 | 6.4 | 52.6% |
| 19 | 1074.8 | 8.6 | 53.2% |
| 15 | 1338.3 | 10.7 | 52.6% |

CPU is flat across every setting: **quality costs the GPU, not the CPU.**

### Offline, against real desktop text in motion

Scrolling a real screenshot, SSIM against a lossless reference - the case the
operator described as "a bit soft during motion":

| setting | Mbps | SSIM |
|---|---|---|
| **vbr default (cq off)** | **1.75** | **0.95829** |
| cq 15 | 1.75 | 0.95829 |
| cq 17 | 1.51 | 0.95793 |
| cq 19 | 1.42 | 0.95752 |
| cq 23 | 1.19 | 0.95638 |
| cq 27 | 0.99 | 0.95423 |
| preset p1 … p7 | 1.73-1.97 | 0.95826-0.95830 |
| tune ll / ull / hq | 1.75 | 0.95829 (identical) |
| constqp qp=8 | 2.51 | 0.95848 |

Three findings:

1. **cq only subtracts.** It is a quality *target* with the bitrate as ceiling,
   so on content the encoder already handles well it can only lower spending.
   cq 15 converges exactly to the default; everything above it is worse.
2. **preset and tune are noise.** p1 to p7 spans 0.00004 SSIM. ll/ull/hq are
   identical to five decimal places. p4/ll are fine.
3. **More bits do not help.** `constqp qp=8` spends **43% more bandwidth for
   +0.19 milli-SSIM**. Rate control is not the constraint.

### So the softness is the 4:2:0 chroma floor, as §10 measured

Quality plateaus at ~0.958 no matter how many bits are thrown at it, which is
the same shape §10 found: 4:2:0 at 100 Mbps plateaus at 0.986 while 4:4:4
reaches 0.998, and "that residual cannot be bought back with bitrate". This
sweep confirms it end to end on real content with the real encoder settings.

4:4:4 is not available: TigerVNC's Media Foundation path requests NV12 output
and will not decode it (§1).

**The only remaining levers for text crispness are structural, not encoder
settings:**

- The hybrid gate already keeps text on Tight while the screen is static, which
  is when text is actually read. Softness appears only during motion.
- `-h264_enter` trades bandwidth for crispness: raising it keeps more content on
  4:4:4 Tight at the cost of Tight's bandwidth under motion.

The knobs stay - they cost nothing, they are the right thing to have exposed,
and `cq` is a legitimate way to *cap* bandwidth if a link ever needs it. They
are simply not the answer to soft text.

## 26. Phase 3' landed, and it moved the CPU somewhere unexpected (2026-08-22)

§24 is implemented. While H.264 owns every client, `scan_for_updates()` returns
right after the diff map has marked the tiles and never fills `main_fb`; the
encoder reads NVFBC's capture buffer where it already is. The copies are gone,
counted rather than assumed - the `h264 stats:` line now ends with
`N fb-skips/s`, and it equals the NVFBC grabs/sec exactly.

### What it cost and what it bought

Same-rig A/B, both binaries in turn on a throwaway port with a synthetic H.264
consumer, identical full-screen load, production untouched on 5900
(`results/ab-base-20260822-160907.json`, `results/ab-phase3-20260822-160956.json`):

| | base `c5c7ddc` | Phase 3' |
|---|---|---|
| CPU | 113.3% | **104.9%** |
| captured fps | 33.7 | **41.6** |
| CPU per captured frame | 33.55 ms | **25.35 ms** |
| H.264 fps delivered | 15.1 | 15.7 |
| fb-skips/s | - | **47** (= grabs/s) |

On production against the real TigerVNC client, `full` went **130.3% -> 118.8%**
with captured frames up 27.4 -> 35.9 fps
(`results/phase3-prod-20260822-160525.json`).

So: **-24% CPU per captured frame, and 24% more frames captured for less total
CPU.** Delivered frame rate is unchanged because the fence pacing, not the
server, sets it.

### §24's attribution was wrong by an order of magnitude

§24 predicted `full` would land near 30% and said "anything near 50% means a
copy is still happening". The copies are provably gone and it landed at 105%.
The error was in the model, not the measurement: 2 x 14.7 MB per captured frame
is ~1 GB/s of memcpy, and on this machine that is **~8% of a core, not 108%**.
The A/B delta (113.3 -> 104.9, at a *higher* capture rate) is exactly that size.

Do not size a memcpy in CPU-percent by intuition again. Measure the bandwidth
against the machine's actual memcpy rate first.

### Where the CPU actually is: NVENC's CUDA threads poll for GPU completion

Per-thread attribution of the running server under full load - `/proc/PID/task/*`
utime/stime deltas plus `voluntary_ctxt_switches`, no profiler needed since
`perf_event_paranoid` is 3:

```
     tot     user      sys   volsw/s  thread
   36.0%     8.1%    27.9%    41929   cuda-EvtHandlr     <- one per open tile encoder
   34.4%     7.4%    27.0%    41185   cuda-EvtHandlr
   31.6%    27.7%     3.9%      240   x11vnc (watch_loop)
  102.7%  TOTAL
```

**Two thirds of the process is the CUDA driver's own threads, and it is almost
all system time.** 42,000 voluntary context switches per second per thread, at
15.7 encoded fps - roughly 2,700 wakeups per encoded frame. `/proc/TID/syscall`
says syscall **7 (`poll`)** and `/proc/TID/wchan` says **`do_sys_poll`**: the
threads sit in a `poll()` loop on the NVIDIA device fd waiting for the GPU to
signal completion. The cost is syscall and scheduler overhead, not computation.

### It scales with GPU wait time, not with frames

Same 2-tile production config, three loads, thread CPU and GPU utilisation
sampled together:

| load | GPU | cuda-EvtHandlr each | volsw/s each | delivered |
|---|---|---|---|---|
| none (encoders open, nothing submitted) | 8% | **0.3%** | 100 | 0 fps |
| 640x480@60 | 35% | **1.0%** | 350 | 17.3 fps |
| full screen | 76% | **35%** | 41,500 | 15.7 fps |

The middle row is the important one: **the same encoders, delivering *more*
frames than the full-screen case, cost 1% instead of 35%.** What changed is how
long the GPU takes to finish, and the poll loop runs for as long as that takes.
The tile count tracks the thread count exactly - forcing 4 tiles
(`-h264_tile_pixels 1000000`) produces 4 such threads - and a server with
`-nvfbc` but no `-h264` has none at all, so they belong to NVENC, not NVFBC.

Two corrections to what was first written here, both from the measurement above:

- **They do not spin unconditionally.** Encoders left open with nothing being
  submitted cost 0.3% each. The earlier reading of "constant, not per frame"
  came from comparing `-h264_fps 30` against `-h264_fps 8` under the *same*
  full-screen load: that holds GPU busy-time roughly constant, because most of
  it is the load generator and NVFBC's full-frame DMA rather than our encodes,
  so halving the frame count did not shorten the waits. It is invariant to
  frame rate, not to GPU load. §14's flat 5/15/30 fps result is the same effect.
- **The 70% is a property of this benchmark, not of the deployment.** Under
  `full`, the load generator is itself repainting 2560x1440 at 28 fps; GPU goes
  35% -> 76% between the two loaded legs and roughly half of that is the
  benchmark. A real desktop under ordinary motion looks like the middle row.
  Something genuinely GPU-heavy - a game, a 3D application - would look like
  the bottom one.

### What that makes the next target

Nothing in x11vnc's own code is now worth optimising for CPU: capture, diff map,
tick, encode submission and RFB output together are 31%.

**The lever is the CUDA context's completion-wait mode.** A context created with
`CU_CTX_SCHED_BLOCKING_SYNC` sleeps on an interrupt instead of polling, which
would trade a little wake-up latency for the whole of that 70% under GPU load.
libavcodec's `h264_nvenc` creates its own context with default flags and does
not expose them, but it will use one handed to it through `hw_device_ctx`, so
the route is to create the CUDA context in the fork and pass it in. Untested.
Route B of §4 (the NVENC SDK directly) also owns context creation.

**What is NOT worth doing:** closing the tile encoders when the gate returns to
Tight. That was the first recommendation written here and the measurement kills
it - see the note at the end of §27 for the numbers after the shared context.

One caveat on the instrumentation: `nvidia-smi --query-gpu=utilization.encoder`
reported 0% in every leg, including ones demonstrably encoding. Do not use that
field on this driver; §24's "encoder block at 32%" came from a different query
and is not reproduced here.

### Traps §24 listed, resolved

1. **The cursor: §24's premise was wrong, and nothing had to change.** The
   cursor is not in `main_fb` and never was in the H.264 stream. libvncserver
   composites the soft cursor only for a client with
   `enableCursorShapeUpdates == FALSE`, and the real client takes the
   pseudo-encoding - the production log says so on every connect:
   `Enabling full-color cursor updates` / `Enabling X-style cursor updates`.
   x11vnc's own `draw_cursor()` is `use_multipointer` only. **Decision: the
   cursor stays client-side, rendered from the RFB cursor pseudo-encoding.**
2. **Stale framebuffer: real, fixed, and regression-tested.** Every path out of
   H.264 mode goes through `h264_release_output()`, which drops `exclusive`,
   refills `main_fb` with `copy_screen()`, then marks. There are four such
   paths, not the one §24 anticipated: the gate going quiet, the backed-up
   fallback, the last H.264 client leaving, and `exclusive` dropping without the
   gate exiting (a second Tight-only viewer joining).

   The test that proves it: paint a solid colour **after** the gate has engaged
   and leave it static, then stop the driving load. The region changed during
   the skip and is static afterwards, so the diff map will never re-mark it. A
   control build with the refill removed repaints the pre-H.264 desktop and the
   colour is invisible forever:

   | build | R2 reads back as |
   |---|---|
   | refill removed (control) | `faf9f7` desktop, **FAIL: 0.00% are ff00ff** |
   | as shipped | **PASS: 100.00% are ff00ff** |
3. **Only skip when H.264 owns every client:** gated on `exclusive`, nothing else.
4. `nvfbc_src_dx/dy` honoured in `nvfbc_served_pixels()`, with a bounds check
   that returns NULL rather than reading outside the captured frame.
5. **Framebuffer path still works:** `h264_frame_source()` falls back to
   `screen->frameBuffer`, and repairs it with `copy_screen()` first if the
   copies had been skipped.
6. **Buffer lifetime** is stated in the comment on `nvfbc_served_pixels()`: the
   next grab is at the top of the next `scan_for_updates()`, and the tick runs
   after this cycle's grab, inside the send ban.
7. **`-nvfbc_direct` is a non-issue.** Direct capture is a ToSys optimisation
   inside the driver; the destination is still `nvfbc_state.frame_buffer`.

Two conditions were added that §24 did not call for, both to avoid a stale
state with no exit from it: skip only when `nvfbc_scanned` (otherwise
`scan_display()` derives `tile_count` by comparing against a `main_fb` that has
stopped tracking the screen, reports everything dirty forever, and the gate can
never exit), and only when `fs_factor` (otherwise `copy_screen()` is a no-op and
the refill silently does nothing).

### Wire structure unchanged

`rfbcheck.py --h264 --fence --dump-h264` over 444 access units: every rect
2560x720, SPS first on all 444, both `H264_RESET_CONTEXT` frames real IDRs,
**0 bytes of filler**, 0 fence timeouts.

## 27. The blocking-sync context: hypothesis refuted, but it found the real fix (2026-08-22)

§26 proposed giving `h264_nvenc` a CUDA context created with
`CU_CTX_SCHED_BLOCKING_SYNC` so the driver thread would sleep on an interrupt
instead of polling. Implemented as `-h264_cuda_sched`, which builds one context
up front and hands it to every tile encoder through `hw_device_ctx`
(`AV_CUDA_USE_CURRENT_CONTEXT`; libcuda is `dlopen`ed, so the build gains no
CUDA dependency and any failure falls back to libavcodec's own context).

Swept under the full-screen load, 2 tiles:

| `-h264_cuda_sched` | driver threads | total CPU | delivered | GPU | wakeups/s |
|---|---|---|---|---|---|
| `auto` (libavcodec's own, one per encoder) | 2 | **105.3%** | 15.7 fps | 75% | 43,000 each |
| `spin` (shared) | 1 | **69.5%** | 16.1 fps | 76% | 45,700 |
| `yield` (shared) | 1 | **70.8%** | 15.7 fps | 75% | 46,200 |
| `blocking` (shared) | 1 | **69.9%** | 15.6 fps | 76% | 45,300 |

**The scheduling flag is inert.** All three shared modes poll at the same rate;
`CU_CTX_SCHED_BLOCKING_SYNC` does not change what `cuda-EvtHandlr` does. That
flag governs how a thread calling `cuCtxSynchronize` waits, not the driver's
own event-handler thread, which polls regardless.

**What actually cost 36 points was one context per encoder.** libavcodec creates
a CUDA context per `AVCodecContext`, each with its own polling thread, so the
cost scaled with the tile count - and the tile count exists only because the
client cannot decode a rect larger than 2.36 Mpx (§22). Sharing one context
collapses N polling threads into one. Delivered frame rate is unchanged or
slightly better, so nothing serialises behind the shared stream.

**Default is now `blocking`** - i.e. one shared context. `auto` restores the old
per-encoder behaviour. The flag names are kept because they cost nothing and
another driver may not be indifferent to them.

### Where that leaves `full`

Confirmed on production against the real client after deploying, same
four-scenario `measure.py` invocation as every earlier production run
(`results/sharedctx-prod-20260822-165741.json`):

    130.3%   before Phase 3'   -> 47.86 ms per captured frame
    118.8%   Phase 3'          -> 33.15 ms
     72.8%   + shared context  -> 22.49 ms

**-44% CPU, -53% per captured frame, wire unchanged at ~390 KB/s.** `medium`
went 42.3% -> 33.0%, `small` is flat within noise, and `idle` is uncontrolled
(it draws nothing, so it measures whatever the desktop was doing - 25.3, 12.5
and 70.4 KB/s across the three runs).

The gate held one continuous H.264 period across the whole `full` window,
0 fence timeouts, every frame encoded direct from the NVFBC buffer.

x11vnc's own thread is 30.8% of that 69.4%; the remaining 37.8% is the single
driver thread, and it is still 29.2% *system* time at 45,000 wakeups/s. Removing
it entirely would need NVENC to stop polling, which nothing in the libavcodec
API reaches - Route B of §4 (the SDK directly) is the only remaining lever, and
it is not obviously worth it for one thread.

Remember §26's caveat: this is the synthetic full-screen load, where the load
generator itself drives the GPU to 76%. Under ordinary desktop motion the same
thread costs ~1%.

### Verified with the shared context

- Three gate entries and exits, encoders closed and reopened each time: **one**
  context created, reused throughout.
- Four tiles (`-h264_tile_pixels 1000000`) on the one context: **one** driver
  thread, not four.
- The Phase 3' exit repaint still lands (`00ddaa` region reads back 100%).
- Wire structure unchanged: 632 access units, all SPS-first, both
  `H264_RESET_CONTEXT` frames real IDRs, 0 filler, every rect 2560x720.
- A bad mode name (`-h264_cuda_sched banana`) logs and falls back to `auto`.
- `avcodec_open2` gets *faster* after the first tile - 216/230 ms against
  483/557 ms - because the context already exists. That halves the gate-entry
  hitch as a side effect.

Two tools this needed are now in `bench/`: `threadcpu.py` (per-thread CPU with
the user/sys and context-switch split) and `nalscan.py` (the access-unit
structure check §21 and §26 describe but which had never been checked in).


### Closing the encoders when idle: still not a CPU measure (2026-08-22)

Re-measured after the shared context, since the earlier answer was based on the
per-encoder world. Client attached, screen static, against a control that never
engaged the gate (`-h264_enter 100`, because at `-h264_enter 1` ordinary desktop
activity trips it and the control is not a control):

| | encoders never opened | encoders open, screen static |
|---|---|---|
| CUDA driver threads | 0 | 1 |
| that thread's CPU | - | **0.2%** |
| process total | 9.2% | 8.8% |
| VRAM | 107 MiB | **341 MiB** |

**CPU: nothing to win.** 0.2%, below the noise - the totals come out *lower* in
the encoders-open leg, which is desktop activity, not the encoders. The shared
context already took everything this idea was once supposed to buy.

What remains are resources another application might want:

- **VRAM, at most 234 MiB** - and less in practice. The shared CUDA context is
  created on the first encoder open and lives for the process lifetime, so
  closing the encoders would release the two session buffers but not the
  context. Splitting those needs a build that closes one and keeps the other;
  234 MiB is the upper bound, not the saving.
- **Two NVENC sessions held open.** The more plausible argument: OBS, game
  capture or a browser doing WebRTC encode contend for the driver's concurrent
  session cap. Not tight on this driver - the 4-tile test opened four.

The cost side is cheaper than it was: reopening both tiles is ~60 ms now (30 ms
each) rather than ~250 ms, and it adds nothing client-side, since gate entry
already sets `need_idr` and the client rebuilds its decoder on every entry.

**If it is ever wanted, it is for the session/VRAM reason and the trigger should
be a long idle - 30-60 s of continuous Tight, not "a few seconds"** - so
ordinary scrolling never pays the reopen while a machine left alone releases the
resources. That is a different feature with a different justification.

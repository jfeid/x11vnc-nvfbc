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
   pre-encoded Annex-B file, and connect the real TigerVNC 1.15.0 client to it.
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
real TigerVNC 1.15.0 client over the real SSH tunnel. Picture confirmed by eye.

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

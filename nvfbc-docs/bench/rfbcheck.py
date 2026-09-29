#!/usr/bin/env python3
"""
rfbcheck.py - fetch a rectangle over RFB and verify its pixels.

End-to-end check that what a VNC client receives actually corresponds to the
right part of the screen. Catches capture coordinate-mapping bugs that compile
cleanly and look fine in a log.

  ./rfbcheck.py --port 5901 --rect 100,100,256,256 --expect ff8000
  ./rfbcheck.py --port 5901 --info
  ./rfbcheck.py --port 5901 --tight --compress 6 --quality 9 --stream 30

Assumes the server allows the "None" security type (start it with -nopw).
"""

import argparse
import os
import socket
import struct
import sys
import time


def recv_exact(s, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = s.recv(n - len(buf))
        if not chunk:
            raise EOFError(f"connection closed with {n - len(buf)} bytes outstanding")
        buf += chunk
    return bytes(buf)


def handshake(s):
    ver = recv_exact(s, 12)
    if not ver.startswith(b"RFB "):
        raise RuntimeError(f"not an RFB server: {ver!r}")
    s.sendall(b"RFB 003.008\n")

    ntypes = recv_exact(s, 1)[0]
    if ntypes == 0:
        reason = recv_exact(s, struct.unpack(">I", recv_exact(s, 4))[0])
        raise RuntimeError(f"server refused connection: {reason.decode(errors='replace')}")
    types = recv_exact(s, ntypes)
    if 1 not in types:
        raise RuntimeError(f"server requires auth (types {list(types)}); start it with -nopw")
    s.sendall(bytes([1]))                     # None

    res = struct.unpack(">I", recv_exact(s, 4))[0]
    if res != 0:
        raise RuntimeError("security handshake failed")

    s.sendall(bytes([1]))                     # ClientInit, shared
    w, h = struct.unpack(">HH", recv_exact(s, 4))
    pf = recv_exact(s, 16)
    namelen = struct.unpack(">I", recv_exact(s, 4))[0]
    name = recv_exact(s, namelen).decode(errors="replace")
    return w, h, pf, name


def set_bgra_format(s):
    """Pin the wire format to 32bpp little-endian BGRA so pixels are unambiguous.

    With big-endian-flag 0 and shifts r=16 g=8 b=0, each pixel lands in memory
    as B,G,R,pad - matching what the parsing below assumes.
    """
    pf = struct.pack(">BBBBHHHBBBBBB",
                     32,                   # bits-per-pixel
                     24,                   # depth
                     0,                    # big-endian-flag
                     1,                    # true-colour-flag
                     255, 255, 255,        # red/green/blue max
                     16, 8, 0,             # red/green/blue shift
                     0, 0, 0)              # padding
    s.sendall(struct.pack(">B3x", 0) + pf)


RFB_ENC_RAW = 0
RFB_ENC_TIGHT = 7
RFB_ENC_H264 = 50
# rfbEncodingCompressLevel0 = 0xFFFFFF00, rfbEncodingQualityLevel0 = 0xFFFFFFE0
# (/usr/include/rfb/rfbproto.h). The client picks these; the server obeys them.
COMPRESS_BASE = -256
QUALITY_BASE = -32

# RFB fence flow control (TigerVNC common/rfb/{encodings,msgTypes,fenceTypes}.h).
# The pseudo-encoding advertises support; message 248 carries the fence both ways.
# The server's H.264 path fences the stream and withholds the next frame until we
# echo, so echoing here is what lets --fence imitate a real TigerVNC consumer -
# and pairing it with --slow is what reproduces a slow one.
RFB_ENC_FENCE = -312
RFB_MSG_FENCE = 248
FENCE_FLAG_BLOCK_BEFORE = 0x00000001
FENCE_FLAG_BLOCK_AFTER = 0x00000002
FENCE_FLAG_REQUEST = 0x80000000

TIGHT_FILL = 0x08
TIGHT_JPEG = 0x09
TIGHT_EXPLICIT_FILTER = 0x04
TIGHT_FILTER_COPY, TIGHT_FILTER_PALETTE, TIGHT_FILTER_GRADIENT = 0, 1, 2
TIGHT_MIN_TO_COMPRESS = 12


def read_compact_len(s):
    """Tight's 1-3 byte length field; returns (value, header_bytes)."""
    b = recv_exact(s, 1)[0]
    n, used = b & 0x7F, 1
    if b & 0x80:
        b = recv_exact(s, 1)[0]
        n |= (b & 0x7F) << 7
        used += 1
        if b & 0x80:
            b = recv_exact(s, 1)[0]
            n |= b << 14
            used += 1
    return n, used


def skip_tight_rect(s, w, h, tally=None, dump=None):
    """Consume one Tight rect and return its size on the wire.

    Walks the Tight headers only - no zlib inflate, no JPEG decode. For
    measuring what the server's encoder produced, the size and the sub-encoding
    it chose are the whole answer, and decoding would just burn client CPU
    inside the measurement.

    Recording the sub-encoding matters more than it looks: solid-colour loads
    come back as `fill`/`palette` and never reach libjpeg, so a JPEG quality
    setting measured against them moves nothing.
    """
    ctl = recv_exact(s, 1)[0]
    used = 1
    ctl >>= 4                       # low nibble is per-stream zlib reset flags

    if ctl == TIGHT_FILL:
        recv_exact(s, 3)            # TPIXEL: 3 bytes for our 32bpp/depth-24 format
        used += 3
        kind = "fill"
    elif ctl == TIGHT_JPEG:
        n, c = read_compact_len(s)
        payload = recv_exact(s, n)
        if dump is not None:
            dump(payload)
        used += c + n
        kind = "jpeg"
    elif ctl > TIGHT_JPEG:
        raise RuntimeError(f"invalid tight compression control 0x{ctl:x}")
    else:
        if ctl & TIGHT_EXPLICIT_FILTER:
            filt = recv_exact(s, 1)[0]
            used += 1
        else:
            filt = TIGHT_FILTER_COPY
        if filt == TIGHT_FILTER_PALETTE:
            ncolours = recv_exact(s, 1)[0] + 1
            recv_exact(s, ncolours * 3)
            used += 1 + ncolours * 3
            bpp = 1 if ncolours == 2 else 8
            kind = "palette"
        elif filt == TIGHT_FILTER_GRADIENT:
            bpp, kind = 24, "gradient"
        elif filt == TIGHT_FILTER_COPY:
            bpp, kind = 24, "copy"
        else:
            raise RuntimeError(f"unknown tight filter {filt}")
        plain = ((w * bpp + 7) // 8) * h
        if plain < TIGHT_MIN_TO_COMPRESS:
            recv_exact(s, plain)
            used += plain
        else:
            n, c = read_compact_len(s)
            recv_exact(s, n)
            used += c + n

    if tally is not None:
        tally[kind] = tally.get(kind, 0) + 1
    return used


def wanted_encodings(tight=False, compress=None, quality=None, h264=False,
                     fence=False):
    """Encoding list in preference order, plus the settings pseudo-encodings."""
    encs = [RFB_ENC_TIGHT if tight else RFB_ENC_RAW]
    if h264:
        encs.insert(0, RFB_ENC_H264)
    if fence:
        encs.append(RFB_ENC_FENCE)
    if compress is not None:
        encs.append(COMPRESS_BASE + compress)
    if quality is not None:
        encs.append(QUALITY_BASE + quality)
    return tuple(encs)


# Set by main() when --fence is given, so read_update echoes ServerFence.
fence_state = None  # {"echoed": int, "requests": int}


def echo_fence(s):
    """Read a ServerFence body (the type byte is already consumed) and, if it
    carries the request bit, echo it back as a ClientFence - exactly what
    TigerVNC's CConnection::fence does. Returns the flags seen."""
    recv_exact(s, 3)                                    # padding
    (flags,) = struct.unpack(">I", recv_exact(s, 4))
    length = recv_exact(s, 1)[0]
    payload = recv_exact(s, length) if length else b""
    if flags & FENCE_FLAG_REQUEST and not os.environ.get("RFBCHECK_FENCE_NOECHO"):
        out_flags = flags & (FENCE_FLAG_BLOCK_BEFORE | FENCE_FLAG_BLOCK_AFTER)
        s.sendall(struct.pack(">B3xIB", RFB_MSG_FENCE, out_flags, length)
                  + payload)
        if fence_state is not None:
            fence_state["echoed"] += 1
    if fence_state is not None:
        fence_state["requests"] += 1
    return flags


def set_encodings(s, encodings=(0,)):
    s.sendall(struct.pack(">BBH", 2, 0, len(encodings))
              + b"".join(struct.pack(">i", e) for e in encodings))


def request_update(s, x, y, w, h, incremental=0):
    s.sendall(struct.pack(">BBHHHH", 3, incremental, x, y, w, h))


h264_sink = None


def read_update(s, tally=None, dump=None):
    """Read one FramebufferUpdate.

    Raw rects are returned as {(x,y,w,h): bgra_bytes}. Tight rects are counted
    and discarded - see skip_tight_rect - so the returned dict is empty under
    --tight and callers should read `tally` instead.
    """
    while True:
        msg = recv_exact(s, 1)[0]
        if msg == 0:
            break
        elif msg == 2:            # Bell
            continue
        elif msg == 3:            # ServerCutText
            recv_exact(s, 3)
            n = struct.unpack(">I", recv_exact(s, 4))[0]
            recv_exact(s, n)
            continue
        elif msg == RFB_MSG_FENCE:   # ServerFence: echo it (flow control)
            echo_fence(s)
            continue
        else:
            raise RuntimeError(f"unexpected server message type {msg}")

    recv_exact(s, 1)
    nrects = struct.unpack(">H", recv_exact(s, 2))[0]
    rects = {}
    for _ in range(nrects):
        x, y, w, h, enc = struct.unpack(">HHHHi", recv_exact(s, 12))
        if enc == RFB_ENC_RAW:
            data = recv_exact(s, w * h * 4)
            rects[(x, y, w, h)] = data
            nbytes = len(data)
            if tally is not None:
                tally["raw"] = tally.get("raw", 0) + 1
        elif enc == RFB_ENC_H264:
            # U32 length, U32 flags, then that many bytes of Annex-B
            plen, flags = struct.unpack(">II", recv_exact(s, 8))
            payload = recv_exact(s, plen)
            if h264_sink is not None:
                h264_sink(payload, flags, w, h)
            nbytes = 8 + plen
            if tally is not None:
                tally["B_h264"] = tally.get("B_h264", 0) + nbytes
                tally["h264"] = tally.get("h264", 0) + 1
                if flags:
                    k = f"h264_flags{flags}"
                    tally[k] = tally.get(k, 0) + 1
        elif enc == RFB_ENC_TIGHT:
            nbytes = skip_tight_rect(s, w, h, tally, dump)
            if tally is not None:
                tally["B_tight"] = tally.get("B_tight", 0) + nbytes
                g = f"G_{w}x{h}"
                tally[g] = tally.get(g, 0) + 1
        else:
            raise RuntimeError(
                f"server used encoding {enc}; this client handles raw(0), "
                f"tight(7) and h264(50)")
        if tally is not None:
            tally["bytes"] = tally.get("bytes", 0) + nbytes
            tally["rects"] = tally.get("rects", 0) + 1
    return rects


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5901)
    ap.add_argument("--rect", help="x,y,w,h to fetch")
    ap.add_argument("--expect", help="expected pixel colour as RRGGBB")
    ap.add_argument("--absent", help="colour that must NOT dominate the rect (RRGGBB). "
                                     "Note x11vnc does not scan while no client is "
                                     "attached, so keep one connected or a cold read "
                                     "legitimately returns a stale framebuffer.")
    ap.add_argument("--info", action="store_true", help="just print ServerInit")
    ap.add_argument("--stream", type=float, metavar="SECS",
                    help="pump incremental updates for SECS and report client-side fps")
    ap.add_argument("--tight", action="store_true",
                    help="request Tight instead of Raw, so the server does real "
                         "encoding work - the point when measuring the encoder. "
                         "Payloads are sized and discarded, not decoded, so pixel "
                         "checks still need Raw.")
    ap.add_argument("--compress", type=int, choices=range(10), metavar="0-9",
                    help="Tight compression level to request (pseudo-encoding -256+N)")
    ap.add_argument("--quality", type=int, choices=range(10), metavar="0-9",
                    help="JPEG quality level to request (pseudo-encoding -32+N)")
    ap.add_argument("--h264", action="store_true",
                    help="also advertise encoding 50 and accept H.264 rects "
                         "(sized and skipped, not decoded)")
    ap.add_argument("--fence", action="store_true",
                    help="advertise RFB fence support (pseudo-encoding -312) and "
                         "echo ServerFence messages. With the fence flow-control "
                         "fix the server withholds the next H.264 frame until this "
                         "echo arrives, so --fence --slow N imitates a consumer "
                         "that can take only 1000/N frames per second")
    ap.add_argument("--slow", type=float, default=0.0, metavar="MS",
                    help="sleep MS after each update, to imitate a viewer that "
                         "has to decode. Without this the client is effectively "
                         "infinitely fast and cannot reproduce anything that "
                         "depends on a slow consumer - which is most of what "
                         "goes wrong with a pushed video stream.")
    ap.add_argument("--dump-h264", metavar="FILE",
                    help="append every H.264 access unit to FILE as raw Annex-B, "
                         "and log per-unit size/flags to FILE.log. Lets the stream "
                         "the server produced be checked offline - the client "
                         "reports nothing when it fails to decode one")
    ap.add_argument("--dump-jpeg", metavar="DIR",
                    help="write Tight JPEG payloads to DIR as NNNN.jpg. Lets you inspect "
                         "what the server's encoder actually produced - chroma subsampling "
                         "in particular, which is invisible from the protocol")
    ap.add_argument("--tolerance", type=float, default=0.98,
                    help="fraction of pixels that must match (default 0.98)")
    ap.add_argument("--wait", type=float, default=0.0, metavar="SECS",
                    help="poll the rect until it matches --expect, up to SECS. "
                         "Updates are asynchronous, so a single cold read can "
                         "legitimately still show the previous contents.")
    args = ap.parse_args()

    if args.tight and (args.rect or args.expect or args.absent):
        sys.exit("rfbcheck.py: --tight cannot verify pixels (Tight payloads are "
                 "sized, not decoded); drop --tight, or drop --rect/--expect/--absent")
    if (args.compress is not None or args.quality is not None) and not args.tight:
        print("rfbcheck.py: --compress/--quality only apply to Tight; "
              "add --tight or they are ignored by the server", file=sys.stderr)

    encs = wanted_encodings(args.tight, args.compress, args.quality, args.h264,
                            args.fence)

    global h264_sink, fence_state
    if args.fence:
        fence_state = {"echoed": 0, "requests": 0}
    if args.dump_h264:
        raw = open(args.dump_h264, "wb")
        meta = open(args.dump_h264 + ".log", "w")
        counter = [0]

        def h264_sink(payload, flags, w, h):
            counter[0] += 1
            raw.write(payload)
            meta.write(f"{counter[0]}\t{len(payload)}\t{flags}\t{w}x{h}\n")
            meta.flush()

    s = socket.create_connection((args.host, args.port), timeout=15)
    s.settimeout(15)
    w, h, pf, name = handshake(s)
    print(f"ServerInit: {w}x{h} \"{name}\"  bpp={pf[0]} depth={pf[1]} bigendian={pf[2]} truecolour={pf[3]}")
    print(f"            rgb_max=({struct.unpack('>HHH', pf[4:10])})  shifts=({pf[10]},{pf[11]},{pf[12]})")

    if args.stream:
        # Client-side view of delivered frames: the metric that actually
        # matters, independent of anything the server reports about itself.
        set_bgra_format(s)
        set_encodings(s, encs)
        print(f"encodings: {list(encs)}"
              + (f"  (tight compress={args.compress} quality={args.quality})"
                 if args.tight else ""))
        tally = {}
        t_first = [None, None]      # first any-rect, first h264-rect
        dump = None
        if args.dump_jpeg:
            os.makedirs(args.dump_jpeg, exist_ok=True)
            counter = [0]
            def dump(payload):
                counter[0] += 1
                if counter[0] <= 20:          # a handful is plenty to inspect
                    with open(os.path.join(args.dump_jpeg,
                                           f"{counter[0]:04d}.jpg"), "wb") as fh:
                        fh.write(payload)
        t0 = time.time()
        updates = 0
        while time.time() - t0 < args.stream:
            request_update(s, 0, 0, w, h, incremental=1)
            before_h = tally.get("h264", 0)
            read_update(s, tally, dump)
            updates += 1
            if t_first[0] is None and tally.get("rects", 0) > 0:
                t_first[0] = time.time() - t0
            if t_first[1] is None and tally.get("h264", 0) > before_h:
                t_first[1] = time.time() - t0
            if args.slow > 0:
                time.sleep(args.slow / 1000.0)
        dt = time.time() - t0
        nbytes = tally.get("bytes", 0)
        rects_n = tally.get("rects", 0)
        print("  first rect: " +
              (f"{t_first[0]*1000:.0f} ms" if t_first[0] is not None else "never") +
              "   first h264: " +
              (f"{t_first[1]*1000:.0f} ms" if t_first[1] is not None else "never"))
        print(f"stream: {updates/dt:.1f} updates/s, {rects_n/dt:.1f} rects/s, "
              f"{nbytes/dt/1048576:.2f} MB/s on the wire over {dt:.1f}s")
        if fence_state is not None:
            print(f"  fences: {fence_state['echoed']} echoed "
                  f"({fence_state['echoed']/dt:.1f}/s) of "
                  f"{fence_state['requests']} received")
        bykind = {k[2:]: v for k, v in tally.items() if k.startswith("B_")}
        if bykind:
            tot = sum(bykind.values()) or 1
            print("  wire bytes: " + ", ".join(
                f"{k}={v/dt/1048576:.2f} MB/s ({100.0*v/tot:.1f}%)"
                for k, v in sorted(bykind.items())))
        geo = sorted(((k[2:], v) for k, v in tally.items() if k.startswith("G_")),
                     key=lambda kv: -kv[1])[:5]
        if geo:
            print("  tight rect sizes (top): " +
                  ", ".join(f"{k} x{v}" for k, v in geo))
        kinds = sorted((k, v) for k, v in tally.items()
                       if k not in ("bytes", "rects") and not k.startswith(("B_", "G_")))
        if kinds:
            total = sum(v for _, v in kinds)
            print("  sub-encodings: " + ", ".join(
                f"{k}={v} ({100.0*v/total:.1f}%)" for k, v in kinds))
        return 0

    if args.info or not args.rect:
        return 0

    set_bgra_format(s)
    set_encodings(s, (0,))

    x, y, rw, rh = (int(v) for v in args.rect.split(","))
    want = int(args.expect, 16) if args.expect else None
    avoid = int(args.absent, 16) if args.absent else None
    deadline = time.time() + args.wait
    attempts = 0

    while True:
        attempts += 1
        request_update(s, x, y, rw, rh, incremental=0)
        rects = read_update(s)
        if not rects:
            print("FAIL: server sent no rectangles")
            return 1

        seen = {}
        total = 0
        for data in rects.values():
            for i in range(0, len(data), 4):
                b, g, r = data[i], data[i + 1], data[i + 2]
                key = (r << 16) | (g << 8) | b
                seen[key] = seen.get(key, 0) + 1
                total += 1

        frac = (seen.get(want, 0) / total) if (want is not None and total) else 0.0
        gone = (seen.get(avoid, 0) / total) if (avoid is not None and total) else 1.0

        if time.time() >= deadline:
            break
        if want is not None and frac >= args.tolerance:
            break
        if avoid is not None and gone < 0.02:
            break
        if want is None and avoid is None:
            break
        time.sleep(0.25)

    top = sorted(seen.items(), key=lambda kv: -kv[1])[:3]
    print(f"got {len(rects)} rect(s), {total} px in {attempts} read(s); dominant colours: "
          + ", ".join(f"{c:06x}={n}({100.0*n/total:.1f}%)" for c, n in top))

    if avoid is not None:
        ok = gone < 0.02
        print(f"{'PASS' if ok else 'FAIL'}: {100.0*gone:.2f}% of pixels are still "
              f"{avoid:06x} (need <2%)")
        return 0 if ok else 1

    if want is None:
        return 0

    ok = frac >= args.tolerance
    print(f"{'PASS' if ok else 'FAIL'}: {100.0*frac:.2f}% of pixels are {want:06x} "
          f"(need {100.0*args.tolerance:.0f}%)")
    return 0 if ok else 1


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"ERROR: {type(e).__name__}: {e}")
        sys.exit(1)

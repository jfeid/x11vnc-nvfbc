#!/usr/bin/env python3
"""
h264serve.py - serve a pre-encoded H.264 file as RFB encoding 50.

Phase 0 step 2 of nvfbc-docs/NVENC-H264-PLAN.md: prove the open H.264 encoding wire
format against a real client before entangling any of it with x11vnc. If a
viewer shows moving video from this, the format, the flags and the client's
decoder are all confirmed, and Phase 1 only has to worry about x11vnc.

Rect payload, per TigerVNC common/rfb/H264Decoder.cxx:

    U32 length     bytes of H.264 that follow
    U32 flags      0x1 resetContext, 0x2 resetAllContexts
    U8  data[len]  Annex-B access unit

One access unit (one whole picture) per FramebufferUpdate. Encode with
-bsf:v h264_metadata=aud=insert and access units are cut at each AUD;
without AUDs they are cut at the first slice of each picture. Multi-slice
pictures stay whole either way.

  ./h264serve.py --file test.h264 --width 1280 --height 720 --port 5906
"""

import argparse
import re
import socket
import struct
import sys

RESET_CONTEXT = 0x1
RESET_ALL_CONTEXTS = 0x2
ENCODING_H264 = 50


def split_nals(data):
    """Return (start, end, nal_type) for every NAL in an Annex-B buffer.

    start includes the start code; the NAL header byte is at the first byte
    after it.
    """
    marks = []
    for m in re.finditer(rb"\x00\x00\x01", data):
        # a 4-byte start code is a 3-byte one with an extra leading zero
        begin = m.start() - 1 if m.start() > 0 and data[m.start() - 1] == 0 else m.start()
        marks.append((begin, data[m.end()] & 0x1F))
    out = []
    for i, (off, t) in enumerate(marks):
        end = marks[i + 1][0] if i + 1 < len(marks) else len(data)
        out.append((off, end, t))
    return out


VCL_TYPES = (1, 5)
AUD, SPS, PPS = 9, 7, 8


def first_slice_of_picture(data, off):
    """True if the slice NAL at off starts a new picture (first_mb_in_slice 0).

    first_mb_in_slice is the first field of the slice header, coded ue(v); the
    value 0 is the single bit '1', so it is the top bit of the byte after the
    NAL header.  Emulation prevention cannot touch that byte.
    """
    hdr = data.index(b"\x00\x00\x01", off) + 3
    return hdr + 1 < len(data) and data[hdr + 1] & 0x80 != 0


def split_access_units(data):
    """Split an Annex-B stream into access units, one per picture.

    With access unit delimiters (encode with -bsf:v h264_metadata=aud=insert)
    each AUD starts a new access unit.  Without them, a new access unit starts
    at the first slice of each picture, together with the SPS/PPS/SEI in front
    of it.

    Cutting at every slice instead would be wrong for multi-slice pictures:
    libx264 with -tune zerolatency, for one, splits each frame into one slice
    per thread, and each update would then carry a fraction of a frame.
    """
    nals = split_nals(data)
    if not nals:
        raise SystemExit("h264serve.py: no NAL start codes found - is this Annex-B?")

    aus, cur = [], []
    if any(t == AUD for _, _, t in nals):
        for nal in nals:
            if nal[2] == AUD and cur:
                aus.append(cur)
                cur = []
            cur.append(nal)
    else:
        cur_has_vcl = False
        for nal in nals:
            off, _, t = nal
            if t in VCL_TYPES and cur_has_vcl and first_slice_of_picture(data, off):
                # the picture's leading non-VCL NALs came after the last slice
                i = len(cur)
                while i > 0 and cur[i - 1][2] not in VCL_TYPES:
                    i -= 1
                aus.append(cur[:i])
                cur = cur[i:]
            cur.append(nal)
            if t in VCL_TYPES:
                cur_has_vcl = True
    if cur:
        aus.append(cur)
    return [[(data[a:b], t) for a, b, t in au] for au in aus]


def normalise_aus(aus):
    """Rebuild every access unit as SPS + PPS + <slice data>.

    TigerVNC's Windows decoder requires the FIRST NAL of each buffer to be the
    SPS: ParseSPS() checks buffer[0..3] for a start code, then demands NAL type
    7. It does not scan. Anything in front of the SPS - an access unit
    delimiter, an SEI - makes it return early, leaving full_width/full_height
    at zero, and the blit is then skipped by a bounds check. The picture stays
    black with no error reported anywhere, because ProcessInput failures are
    swallowed deliberately ("hoping its a temporary encoding glitch").

    Repeating the parameter sets on every access unit is what a real
    implementation wants regardless - it is what lets a decoder join or resync
    mid-stream rather than only at the start.
    """
    sps = next((b for au in aus for b, t in au if t == SPS), None)
    pps = next((b for au in aus for b, t in au if t == PPS), None)
    if sps is None or pps is None:
        raise SystemExit("h264serve.py: stream carries no SPS/PPS - cannot build headers")

    out = []
    for au in aus:
        body = b"".join(b for b, t in au if t not in (AUD, SPS, PPS))
        out.append(sps + pps + body)
    return out, sps, pps


class Peer:
    def __init__(self, sock):
        self.sock, self.buf = sock, b""

    def recv_exact(self, n):
        while len(self.buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise EOFError("client closed the connection")
            self.buf += chunk
        out, self.buf = self.buf[:n], self.buf[n:]
        return out

    def send(self, data):
        self.sock.sendall(data)


def handshake(p, width, height, name):
    p.send(b"RFB 003.008\n")
    ver = p.recv_exact(12).decode("ascii", "replace").strip()
    print(f"  client version : {ver}")

    p.send(struct.pack(">BB", 1, 1))                   # offer only "None"
    chosen = p.recv_exact(1)[0]
    if chosen != 1:
        raise RuntimeError(f"client chose security {chosen}, this server only offers None(1)")
    p.send(struct.pack(">I", 0))                       # SecurityResult OK
    shared = p.recv_exact(1)[0]
    print(f"  shared flag    : {shared}")

    n = name.encode()
    p.send(struct.pack(">HH", width, height)
           + struct.pack(">BBBBHHHBBB3x", 32, 24, 0, 1, 255, 255, 255, 16, 8, 0)
           + struct.pack(">I", len(n)) + n)


def send_au(p, width, height, payload, flags):
    """One FramebufferUpdate carrying a single encoding-50 rect."""
    p.send(struct.pack(">BxH", 0, 1)                    # msg 0, 1 rectangle
           + struct.pack(">HHHHi", 0, 0, width, height, ENCODING_H264)
           + struct.pack(">II", len(payload), flags)
           + payload)


def send_tiles(p, tiles, idx, flags):
    """One FramebufferUpdate carrying several encoding-50 rects, one per tile.

    Why this exists: a 2560x1440 rect renders BLACK on TigerVNC's Windows
    decoder, silently, at any frame rate and from any encoder (plan §22). The
    cutoff measured against the real client is 2048x1152 NV12 = 3,538,944 bytes
    - the size H264WinDecoderContext gives `decoded_buffer` at construction and
    never resizes. Splitting the screen into bands that each fit keeps every
    rect decodable.

    Each tile is a FIXED geometry, so it maps to one stable decoder context.
    Contexts are keyed by rect (isEqualRect) and capped at MAX_H264_INSTANCES
    = 64, so a handful of fixed bands is nothing like the churn §3 rejected.
    """
    body = b"".join(
        struct.pack(">HHHHi", t["x"], t["y"], t["w"], t["h"], ENCODING_H264)
        + struct.pack(">II", len(t["aus"][idx % len(t["aus"])]), flags)
        + t["aus"][idx % len(t["aus"])]
        for t in tiles)
    p.send(struct.pack(">BxH", 0, len(tiles)) + body)


# Bytes remaining AFTER the one-byte message type: SetPixelFormat 3 pad + 16
# format, KeyEvent 7, PointerEvent 5, msg 150 (EnableContinuousUpdates) 9.
FIXED_LEN = {0: 19, 4: 7, 5: 5, 150: 9}


def serve_tiles(args, tiles):
    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((args.bind, args.port))
    srv.listen(1)
    srv.settimeout(args.timeout)
    print(f"serving {args.width}x{args.height} as {len(tiles)} tiles on "
          f"{args.bind}:{args.port} (display :{args.port - 5900})")
    try:
        conn, addr = srv.accept()
    except socket.timeout:
        print(f"no client within {args.timeout}s", file=sys.stderr)
        return 2
    finally:
        srv.close()
    print(f"connection from {addr[0]}:{addr[1]}")
    conn.settimeout(60)
    p = Peer(conn)
    idx, sent = 0, 0
    try:
        handshake(p, args.width, args.height, "h264serve-tiled")
        while True:
            msg = p.recv_exact(1)[0]
            if msg == 2:
                _pad, count = struct.unpack(">BH", p.recv_exact(3))
                raw = p.recv_exact(4 * count)
                encs = [v - 0x100000000 if v >= 0x80000000 else v
                        for v in struct.unpack(f">{count}I", raw)]
                print(f"  encoding 50    : "
                      + (f"offered at position {encs.index(ENCODING_H264)+1} of {len(encs)}"
                         if ENCODING_H264 in encs else "NOT OFFERED"))
            elif msg == 3:
                incremental = p.recv_exact(9)[0]
                if not incremental:
                    idx = 0
                send_tiles(p, tiles, idx, RESET_ALL_CONTEXTS if idx == 0 else 0)
                idx += 1
                sent += 1
                if sent % 30 == 0:
                    print(f"  sent {sent} frames")
            elif msg in FIXED_LEN:
                p.recv_exact(FIXED_LEN[msg])
            elif msg == 6:
                _pad, ln = struct.unpack(">3sI", p.recv_exact(7))
                p.recv_exact(ln)
            else:
                print(f"  unknown client message {msg}", file=sys.stderr)
                break
    except (EOFError, socket.timeout, BrokenPipeError, ConnectionResetError) as e:
        print(f"ended after {sent} frames: {e}")
    conn.close()
    return 0


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--file", required=True, help="Annex-B .h264 stream")
    ap.add_argument("--width", type=int, required=True)
    ap.add_argument("--height", type=int, required=True)
    ap.add_argument("--port", type=int, default=5906)
    ap.add_argument("--bind", default="127.0.0.1")
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--loop", action="store_true", help="restart the stream when it ends")
    ap.add_argument("--tile", action="append", metavar="FILE:X:Y:W:H", default=[],
                    help="serve several stacked H.264 rects instead of one, each "
                         "its own stream and its own decoder context. Repeatable. "
                         "Use when one full-screen rect exceeds the client's decode "
                         "buffer and renders black (plan §22).")
    args = ap.parse_args()

    if args.tile:
        tiles = []
        for spec in args.tile:
            fn, x, y, w, h = spec.rsplit(":", 4)
            t_aus, t_sps, _ = normalise_aus(
                split_access_units(open(fn, "rb").read()))
            tiles.append({"x": int(x), "y": int(y), "w": int(w), "h": int(h),
                          "aus": t_aus})
            nv12 = int(w) * ((int(h) + 15) // 16 * 16) * 3 // 2
            print(f"  tile {w}x{h}+{x}+{y}: {len(t_aus)} AUs, "
                  f"NV12 {nv12:,} B {'OK' if nv12 <= 3538944 else 'OVER THE 3,538,944 B LIMIT'}")
        return serve_tiles(args, tiles)

    raw_aus = split_access_units(open(args.file, "rb").read())
    aus, sps, pps = normalise_aus(raw_aus)
    print(f"{args.file}: {len(aus)} access units; "
          f"SPS {len(sps)}B + PPS {len(pps)}B prepended to each "
          f"(TigerVNC's ParseSPS needs the SPS first)")

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((args.bind, args.port))
    srv.listen(1)
    srv.settimeout(args.timeout)
    print(f"serving {args.width}x{args.height} on {args.bind}:{args.port} "
          f"(display :{args.port - 5900}); security None")

    try:
        conn, addr = srv.accept()
    except socket.timeout:
        print(f"no client within {args.timeout}s", file=sys.stderr)
        return 2
    finally:
        srv.close()

    print(f"connection from {addr[0]}:{addr[1]}")
    conn.settimeout(60)
    p = Peer(conn)
    idx, sent = 0, 0
    try:
        handshake(p, args.width, args.height, "h264serve")
        while True:
            msg = p.recv_exact(1)[0]

            if msg == 2:                                        # SetEncodings
                _pad, count = struct.unpack(">BH", p.recv_exact(3))
                raw = p.recv_exact(4 * count)
                encs = [v - 0x100000000 if v >= 0x80000000 else v
                        for v in struct.unpack(f">{count}I", raw)]
                if ENCODING_H264 in encs:
                    print(f"  encoding 50    : offered at position "
                          f"{encs.index(ENCODING_H264) + 1} of {len(encs)}")
                else:
                    print("  encoding 50    : NOT OFFERED - this client cannot decode "
                          "H.264; nothing will render", file=sys.stderr)

            elif msg == 3:                                      # FramebufferUpdateRequest
                incremental = p.recv_exact(9)[0]
                if not incremental:
                    idx = 0                                     # full update: restart at the IDR
                if idx >= len(aus):
                    if not args.loop:
                        print(f"  stream exhausted after {sent} frames")
                        break
                    idx = 0
                flags = RESET_ALL_CONTEXTS if idx == 0 else 0
                send_au(p, args.width, args.height, aus[idx], flags)
                idx += 1
                sent += 1
                if sent % 30 == 0:
                    print(f"  sent {sent} frames", flush=True)

            elif msg == 6:                                      # ClientCutText
                p.recv_exact(3)
                (length,) = struct.unpack(">I", p.recv_exact(4))
                p.recv_exact(length)
            elif msg in FIXED_LEN:
                p.recv_exact(FIXED_LEN[msg])
            else:
                raise RuntimeError(f"unhandled client message type {msg}")

    except (EOFError, RuntimeError, socket.timeout) as exc:
        print(f"\nended after {sent} frames: {exc}")
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

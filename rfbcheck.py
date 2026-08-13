#!/usr/bin/env python3
"""
rfbcheck.py - fetch a rectangle over RFB and verify its pixels.

End-to-end check that what a VNC client receives actually corresponds to the
right part of the screen. Catches capture coordinate-mapping bugs that compile
cleanly and look fine in a log.

  ./rfbcheck.py --port 5901 --rect 100,100,256,256 --expect ff8000
  ./rfbcheck.py --port 5901 --info

Assumes the server allows the "None" security type (start it with -nopw).
"""

import argparse
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


def set_encodings(s, encodings=(0,)):
    s.sendall(struct.pack(">BBH", 2, 0, len(encodings))
              + b"".join(struct.pack(">i", e) for e in encodings))


def request_update(s, x, y, w, h, incremental=0):
    s.sendall(struct.pack(">BBHHHH", 3, incremental, x, y, w, h))


def read_update(s):
    """Read one FramebufferUpdate; returns {(x,y,w,h): raw_bgra_bytes}."""
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
        else:
            raise RuntimeError(f"unexpected server message type {msg}")

    recv_exact(s, 1)
    nrects = struct.unpack(">H", recv_exact(s, 2))[0]
    rects = {}
    for _ in range(nrects):
        x, y, w, h, enc = struct.unpack(">HHHHi", recv_exact(s, 12))
        if enc != 0:
            raise RuntimeError(f"server used encoding {enc}, expected raw(0)")
        rects[(x, y, w, h)] = recv_exact(s, w * h * 4)
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
    ap.add_argument("--tolerance", type=float, default=0.98,
                    help="fraction of pixels that must match (default 0.98)")
    ap.add_argument("--wait", type=float, default=0.0, metavar="SECS",
                    help="poll the rect until it matches --expect, up to SECS. "
                         "Updates are asynchronous, so a single cold read can "
                         "legitimately still show the previous contents.")
    args = ap.parse_args()

    s = socket.create_connection((args.host, args.port), timeout=15)
    s.settimeout(15)
    w, h, pf, name = handshake(s)
    print(f"ServerInit: {w}x{h} \"{name}\"  bpp={pf[0]} depth={pf[1]} bigendian={pf[2]} truecolour={pf[3]}")
    print(f"            rgb_max=({struct.unpack('>HHH', pf[4:10])})  shifts=({pf[10]},{pf[11]},{pf[12]})")

    if args.stream:
        # Client-side view of delivered frames: the metric that actually
        # matters, independent of anything the server reports about itself.
        set_bgra_format(s)
        set_encodings(s, (0,))
        t0 = time.time()
        updates = rects_n = nbytes = 0
        while time.time() - t0 < args.stream:
            request_update(s, 0, 0, w, h, incremental=1)
            got = read_update(s)
            updates += 1
            rects_n += len(got)
            nbytes += sum(len(v) for v in got.values())
        dt = time.time() - t0
        print(f"stream: {updates/dt:.1f} updates/s, {rects_n/dt:.1f} rects/s, "
              f"{nbytes/dt/1048576:.1f} MB/s of pixels over {dt:.1f}s")
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

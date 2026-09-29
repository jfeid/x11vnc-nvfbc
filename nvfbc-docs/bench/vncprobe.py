#!/usr/bin/env python3
"""vncprobe.py - server-side twin of vncprobe.ps1.

Same protocol sequence as the PowerShell probe (RFB 3.8, security type None,
keypress -> first FramebufferUpdate carrying rects, scoped to a rectangle),
but run on the server over loopback. That separates two things the .ps1
measures together:

    server half   = capture + encode + delivery      <- this script
    full round trip = server half + transport RTT    <- vncprobe.ps1

Run it against the same throwaway server the .ps1 uses, with keytarget up:

    ./keytarget -g 400x300+64+64 -d 600 &
    ../../src/x11vnc -display :1 -auth /run/user/1000/gdm/Xauthority \\
        -forever -shared -nopw -localhost -rfbport 5918 -repeat -noipv6 \\
        -clip 2560x1440+0+0 -threads -nonap -nocursor
    ./vncprobe.py --port 5918 --rect 64,64,400,300 --trials 10

--keysym must match keytarget's -k (both default to Page Down, 0xFF56).
"""
import argparse
import socket
import statistics
import struct
import sys
import time

QUIET = 0.25            # stream-quiet window for drains, seconds


def parse_args():
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=5918)
    p.add_argument("--rect", default="64,64,400,300", help="x,y,w,h")
    p.add_argument("--trials", type=int, default=10)
    p.add_argument("--keysym", default="0xFF56",
                   help="probe keysym, must match keytarget -k")
    p.add_argument("--keydown-ms", type=int, default=60)
    p.add_argument("--trial-timeout", type=float, default=3.0)
    return p.parse_args()


class Probe:
    def __init__(self, sock):
        self.s = sock

    def rd(self, n):
        b = b""
        while len(b) < n:
            c = self.s.recv(n - len(b))
            if not c:
                raise EOFError(f"server closed after {len(b)}/{n} bytes")
            b += c
        return b

    def handshake(self):
        banner = self.rd(12)
        if not banner.startswith(b"RFB "):
            sys.exit(f"not an RFB server: {banner!r}")
        self.s.sendall(b"RFB 003.008\n")
        ntypes = self.rd(1)[0]
        if ntypes == 0:
            reason = self.rd(struct.unpack(">I", self.rd(4))[0])
            sys.exit(f"server refused connection: {reason.decode(errors='replace')}")
        types = self.rd(ntypes)
        if 1 not in types:
            sys.exit(f"server requires auth (types: {list(types)}); start it with -nopw")
        self.s.sendall(b"\x01")                       # security: None
        if struct.unpack(">I", self.rd(4))[0] != 0:
            sys.exit("security handshake failed")
        self.s.sendall(b"\x01")                       # ClientInit: shared
        w, h = struct.unpack(">HH", self.rd(4))
        self.rd(16)                                   # pixel format
        name = self.rd(struct.unpack(">I", self.rd(4))[0]).decode(errors="replace")
        return w, h, name

    def set_encodings(self, encodings=(7, 5, 0)):     # Tight, Hextile, Raw
        self.s.sendall(b"\x02\x00" + struct.pack(">H", len(encodings)) +
                       b"".join(struct.pack(">i", e) for e in encodings))

    def fbur(self, rect, incremental):
        self.s.sendall(struct.pack(">BBHHHH", 3, incremental, *rect))

    def key(self, keysym, down):
        self.s.sendall(struct.pack(">BBHI", 4, down, 0, keysym))

    def drain(self, quiet=QUIET, cap=1.0):
        """Read until the stream stays quiet for `quiet` (or `cap` total)."""
        end = time.time() + cap
        self.s.settimeout(quiet)
        total = 0
        while time.time() < end:
            try:
                b = self.s.recv(65536)
            except socket.timeout:
                break
            if not b:
                raise EOFError("server closed during drain")
            total += len(b)
        return total

    def trial(self, rect, keysym, keydown_ms, timeout):
        """One keypress -> first-update measurement. Returns ms or None."""
        self.fbur(rect, 1)
        self.drain(QUIET, 1.0)          # settle: absorb the previous release
        self.fbur(rect, 1)              # leave a request pending
        t0 = time.time()
        self.key(keysym, 1)
        sent_up = False
        latency = None
        while time.time() - t0 < timeout:
            if not sent_up and (time.time() - t0) * 1000 >= keydown_ms:
                self.key(keysym, 0)
                sent_up = True
            self.s.settimeout(0.1)
            try:
                m = self.s.recv(1)
            except socket.timeout:
                continue
            if not m:
                sys.exit("connection closed mid-trial")
            # the rest of the message may straggle in a later segment
            self.s.settimeout(2.0)
            m = m[0]
            if m == 0:                                  # FramebufferUpdate
                self.rd(1)
                if struct.unpack(">H", self.rd(2))[0] >= 1:
                    latency = (time.time() - t0) * 1000
                    break                               # payload drained below
            elif m == 1:                                # SetColourMapEntries
                hdr = self.rd(5)
                self.rd(struct.unpack(">H", hdr[3:5])[0] * 6)
            elif m == 2:                                # Bell
                self.rd(3)
            elif m == 3:                                # ServerCutText
                self.rd(3)
                self.rd(struct.unpack(">I", self.rd(4))[0])
            else:
                sys.exit(f"unknown server message type {m}")
        if not sent_up:
            self.key(keysym, 0)
        self.drain(QUIET, 1.0)
        return latency


def main():
    a = parse_args()
    keysym = int(a.keysym, 0)
    parts = [int(v) for v in a.rect.replace(" ", ",").split(",") if v != ""]
    if len(parts) != 4:
        sys.exit("--rect must be x,y,w,h")
    rect = tuple(parts)

    s = socket.create_connection((a.host, a.port), 5)
    s.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
    s.settimeout(10)
    p = Probe(s)

    w, h, name = p.handshake()
    print(f"desktop: {w}x{h} name={name}")
    p.set_encodings()
    print(f"probing rect {rect[0]},{rect[1]} {rect[2]}x{rect[3]}, "
          f"keysym 0x{keysym:X}, {a.trials} trials")

    p.fbur(rect, 0)
    p.drain(QUIET, 5.0)

    results = []
    for t in range(1, a.trials + 1):
        ms = p.trial(rect, keysym, a.keydown_ms, a.trial_timeout)
        if ms is None:
            print(f"trial {t:2d}: FAIL (no update within {a.trial_timeout:.0f} s)")
        else:
            print(f"trial {t:2d}: {ms:7.1f} ms")
        results.append(ms)

    ok = sorted(x for x in results if x is not None)
    if not ok:
        sys.exit("no successful trials - is keytarget running and visible? "
                 "(check its keypress count on exit)")
    print()
    print(f"n={len(ok)}  min={ok[0]:.1f}  median={statistics.median(ok):.1f}  "
          f"avg={statistics.mean(ok):.1f}  max={ok[-1]:.1f} ms")
    s.close()


if __name__ == "__main__":
    main()

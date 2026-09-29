#!/usr/bin/env python3
"""
rfb-probe.py -- dump the encoding list an RFB client advertises.

Listens on a TCP port, performs the RFB server handshake with security type
"None", then decodes client->server messages and prints the SetEncodings list
in the order the client sent it. That order is the client's preference order,
most-preferred first.

Point a viewer at it:  vncviewer <host>:<port>       (port 5905 -> display :5)

The probe drops the connection once it has the encoding list, so the viewer
will report a disconnect. That is expected -- it has already told us
everything we need.

Touches no X server and grabs no input; it is a plain TCP listener.
"""

import argparse
import socket
import struct
import sys

# ---------------------------------------------------------------------------
# Encoding names. Values are the signed 32-bit numbers sent on the wire.
# ---------------------------------------------------------------------------

ENCODINGS = {
    0: "Raw",
    1: "CopyRect",
    2: "RRE",
    4: "CoRRE",
    5: "Hextile",
    6: "zlib",
    7: "Tight",
    8: "zlibhex",
    9: "Ultra",
    15: "TRLE",
    16: "ZRLE",
    17: "ZYWRLE",
    20: "H.264 (registry-listed variant)",
    21: "JPEG",
    22: "JRLE",
    50: "H.264 (open H.264 encoding)",
    -23: "pseudo: JPEG quality level 9",
    -24: "pseudo: JPEG quality level 8",
    -25: "pseudo: JPEG quality level 7",
    -26: "pseudo: JPEG quality level 6",
    -27: "pseudo: JPEG quality level 5",
    -28: "pseudo: JPEG quality level 4",
    -29: "pseudo: JPEG quality level 3",
    -30: "pseudo: JPEG quality level 2",
    -31: "pseudo: JPEG quality level 1",
    -32: "pseudo: JPEG quality level 0",
    -223: "pseudo: DesktopSize",
    -224: "pseudo: LastRect",
    -225: "pseudo: PointerPos",
    -239: "pseudo: Cursor",
    -240: "pseudo: XCursor",
    -247: "pseudo: Tight compression level 9",
    -248: "pseudo: Tight compression level 8",
    -249: "pseudo: Tight compression level 7",
    -250: "pseudo: Tight compression level 6",
    -251: "pseudo: Tight compression level 5",
    -252: "pseudo: Tight compression level 4",
    -253: "pseudo: Tight compression level 3",
    -254: "pseudo: Tight compression level 2",
    -255: "pseudo: Tight compression level 1",
    -256: "pseudo: Tight compression level 0",
    -260: "TightPng",
    -305: "pseudo: gii",
    -307: "pseudo: DesktopName",
    -308: "pseudo: ExtendedDesktopSize",
    -309: "pseudo: xvp",
    -312: "pseudo: Fence",
    -313: "pseudo: ContinuousUpdates",
    -314: "pseudo: clientRedirect",
    -412: "pseudo: JPEG fine-grained quality",
    -763: "pseudo: JPEG subsampling",
    -1063131698: "pseudo: ExtendedClipboard",
}

# Every value we would consider "this client can take an H.264 stream".
H264_CANDIDATES = {
    50: "open H.264 encoding -- the one TigerVNC implements",
    20: "registry-listed H.264 variant",
    0x48323634: 'LibVNCServer\'s rfbEncodingH264 (ASCII "H264")',
}


def signed32(v):
    return v - 0x100000000 if v >= 0x80000000 else v


def describe(enc):
    name = ENCODINGS.get(enc)
    if name:
        return name
    if enc == signed32(0x48323634):
        return 'LibVNCServer rfbEncodingH264 (ASCII "H264")'
    return "UNKNOWN"


# ---------------------------------------------------------------------------
# Wire helpers
# ---------------------------------------------------------------------------


class Peer:
    def __init__(self, sock):
        self.sock = sock
        self.buf = b""

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


def pixel_format(bpp, depth, big_endian, true_colour, rmax, gmax, bmax, rs, gs, bs):
    return struct.pack(
        ">BBBBHHHBBB3x", bpp, depth, big_endian, true_colour,
        rmax, gmax, bmax, rs, gs, bs,
    )


def parse_pixel_format(raw):
    (bpp, depth, be, tc, rmax, gmax, bmax, rs, gs, bs) = struct.unpack(">BBBBHHHBBB3x", raw)
    return (
        f"{bpp} bpp, depth {depth}, {'big' if be else 'little'}-endian, "
        f"{'true colour' if tc else 'colour map'}, "
        f"max r/g/b {rmax}/{gmax}/{bmax}, shift r/g/b {rs}/{gs}/{bs}"
    )


# ---------------------------------------------------------------------------
# Handshake
# ---------------------------------------------------------------------------


def handshake(p, width, height, name):
    p.send(b"RFB 003.008\n")
    ver = p.recv_exact(12)
    try:
        text = ver.decode("ascii").strip()
        major, minor = int(text[4:7]), int(text[8:11])
    except Exception:
        raise RuntimeError(f"unintelligible ProtocolVersion from client: {ver!r}")
    print(f"  client protocol version : {text}")

    if (major, minor) >= (3, 7):
        p.send(struct.pack(">BB", 1, 1))               # 1 type on offer: None
        chosen = struct.unpack(">B", p.recv_exact(1))[0]
        print(f"  security type chosen    : {chosen} ({'None' if chosen == 1 else 'unexpected'})")
        if chosen != 1:
            raise RuntimeError("client declined security type None; probe needs it")
        if (major, minor) >= (3, 8):
            p.send(struct.pack(">I", 0))               # SecurityResult: OK
    else:
        p.send(struct.pack(">I", 1))                   # 3.3: server dictates None
        print("  security type           : None (RFB 3.3, server-dictated)")

    shared = struct.unpack(">B", p.recv_exact(1))[0]   # ClientInit
    print(f"  shared-desktop flag     : {shared}")

    encoded = name.encode("ascii")
    p.send(
        struct.pack(">HH", width, height)
        + pixel_format(32, 24, 0, 1, 255, 255, 255, 16, 8, 0)
        + struct.pack(">I", len(encoded))
        + encoded
    )


# ---------------------------------------------------------------------------
# Client message loop
# ---------------------------------------------------------------------------

# message type -> fixed number of bytes following the type byte, or None for
# variable-length messages we handle explicitly.
FIXED_LEN = {
    0: 19,    # SetPixelFormat
    3: 9,     # FramebufferUpdateRequest
    4: 7,     # KeyEvent
    5: 5,     # PointerEvent
    150: 9,   # EnableContinuousUpdates
}


def read_messages(p, want=1):
    """Read client messages until we have `want` SetEncodings lists."""
    seen = []
    while len(seen) < want:
        msg = struct.unpack(">B", p.recv_exact(1))[0]

        if msg == 2:  # SetEncodings
            _pad, count = struct.unpack(">BH", p.recv_exact(3))
            raw = p.recv_exact(4 * count)
            encs = [signed32(v) for v in struct.unpack(f">{count}I", raw)]
            seen.append(encs)

        elif msg == 0:  # SetPixelFormat
            body = p.recv_exact(19)
            print(f"  client pixel format     : {parse_pixel_format(body[3:])}")

        elif msg == 6:  # ClientCutText
            _pad = p.recv_exact(3)
            (length,) = struct.unpack(">I", p.recv_exact(4))
            p.recv_exact(length)

        elif msg == 1:  # FixColourMapEntries
            _pad = p.recv_exact(1)
            _first, count = struct.unpack(">HH", p.recv_exact(4))
            p.recv_exact(6 * count)

        elif msg in FIXED_LEN:
            p.recv_exact(FIXED_LEN[msg])

        else:
            raise RuntimeError(
                f"client message type {msg} is not one this probe knows how to "
                f"skip, so the stream can no longer be resynchronised"
            )
    return seen


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def report(encs):
    print()
    print(f"SetEncodings: {len(encs)} entries, in the client's preference order")
    print("-" * 72)
    print(f"{'#':>3}  {'decimal':>12}  {'hex':>10}  name")
    print("-" * 72)
    for i, e in enumerate(encs, 1):
        print(f"{i:>3}  {e:>12}  {e & 0xFFFFFFFF:>#10x}  {describe(e)}")
    print("-" * 72)

    found = []
    for value, note in H264_CANDIDATES.items():
        v = signed32(value) if value >= 0x80000000 else value
        if v in encs:
            found.append((v, note, encs.index(v) + 1))

    print()
    if found:
        print("H.264: ADVERTISED")
        for v, note, pos in found:
            print(f"  encoding {v} ({v & 0xFFFFFFFF:#x}) -- {note}")
            print(f"    preference position {pos} of {len(encs)}")
    else:
        print("H.264: NOT ADVERTISED")
        print("  None of the known H.264 encoding numbers appeared in the list.")
        print("  Either this build has no H.264 decoder compiled in, or it uses")
        print("  a number not in this probe's table -- check the UNKNOWN rows.")

    unknown = [e for e in encs if describe(e) == "UNKNOWN"]
    if unknown:
        print()
        print("Unrecognised encodings (worth checking by hand):")
        for e in unknown:
            print(f"  {e}  ({e & 0xFFFFFFFF:#x})")


# ---------------------------------------------------------------------------


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-p", "--port", type=int, default=5905,
                    help="TCP port to listen on (default: 5905, i.e. display :5)")
    ap.add_argument("-b", "--bind", default="0.0.0.0", help="address to bind (default: all)")
    ap.add_argument("-t", "--timeout", type=int, default=120,
                    help="seconds to wait for a client (default: 120)")
    ap.add_argument("-w", "--width", type=int, default=1920)
    ap.add_argument("-H", "--height", type=int, default=1080)
    ap.add_argument("-n", "--name", default="rfb-probe", help="desktop name to advertise")
    args = ap.parse_args()

    srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    srv.bind((args.bind, args.port))
    srv.listen(1)
    srv.settimeout(args.timeout)

    print(f"rfb-probe listening on {args.bind}:{args.port} "
          f"(VNC display :{args.port - 5900 if args.port >= 5900 else '?'})")
    print(f"connect a viewer within {args.timeout}s; security type offered is None")
    print()

    try:
        conn, addr = srv.accept()
    except socket.timeout:
        print(f"no client connected within {args.timeout}s", file=sys.stderr)
        return 2
    finally:
        srv.close()

    print(f"connection from {addr[0]}:{addr[1]}")
    conn.settimeout(30)
    p = Peer(conn)
    try:
        handshake(p, args.width, args.height, args.name)
        lists = read_messages(p, want=1)
    except (EOFError, RuntimeError, socket.timeout) as exc:
        print(f"\nprobe failed: {exc}", file=sys.stderr)
        return 1
    finally:
        conn.close()

    report(lists[0])
    return 0


if __name__ == "__main__":
    sys.exit(main())

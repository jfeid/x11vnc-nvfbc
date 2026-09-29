#!/usr/bin/env python3
"""
make-aus.py - pack an Annex-B H.264 file into the X11VNCAU container x11vnc's
-h264_testfile reads.

Exists so the C side does not need a NAL parser just to have a known-good
stream to bisect against. Phase 0 established that a malformed encoding-50
stream renders black with no diagnostic anywhere, so being able to feed the
server a stream already proven against the real client is what separates
"transport is broken" from "encoder is broken".

Each access unit is rebuilt as SPS + PPS + slice, AUDs stripped: TigerVNC's
Windows decoder requires the SPS to be the first NAL of every buffer.

  ./make-aus.py in.h264 out.aus --width 1280 --height 720
"""
import argparse, re, struct, sys

VCL, AUD, SPS, PPS = (1, 5), 9, 7, 8


def split_nals(data):
    marks = []
    for m in re.finditer(rb"\x00\x00\x01", data):
        begin = m.start() - 1 if m.start() > 0 and data[m.start() - 1] == 0 else m.start()
        marks.append((begin, data[m.end()] & 0x1F))
    return [(o, marks[i + 1][0] if i + 1 < len(marks) else len(data), t)
            for i, (o, t) in enumerate(marks)]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("infile"); ap.add_argument("outfile")
    ap.add_argument("--width", type=int, required=True)
    ap.add_argument("--height", type=int, required=True)
    a = ap.parse_args()

    data = open(a.infile, "rb").read()
    nals = split_nals(data)
    if not nals:
        sys.exit("no NAL start codes - not Annex-B?")

    sps = next((data[o:e] for o, e, t in nals if t == SPS), None)
    pps = next((data[o:e] for o, e, t in nals if t == PPS), None)
    if sps is None or pps is None:
        sys.exit("stream carries no SPS/PPS")

    aus, cur, has_vcl = [], [], False
    for o, e, t in nals:
        if has_vcl:
            aus.append(cur); cur, has_vcl = [], False
        cur.append((o, e, t))
        if t in VCL:
            has_vcl = True
    if cur:
        aus.append(cur)

    with open(a.outfile, "wb") as f:
        f.write(b"X11VNCAU" + struct.pack(">II", a.width, a.height))
        for au in aus:
            body = b"".join(data[o:e] for o, e, t in au if t not in (AUD, SPS, PPS))
            payload = sps + pps + body
            f.write(struct.pack(">I", len(payload)) + payload)
    print(f"{a.outfile}: {len(aus)} access units, {a.width}x{a.height}, "
          f"SPS {len(sps)}B + PPS {len(pps)}B on each")


if __name__ == "__main__":
    main()

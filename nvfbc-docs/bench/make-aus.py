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
import argparse, struct, sys

# one splitter for both tools: see split_access_units() for the rules
from h264serve import split_access_units, normalise_aus


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("infile"); ap.add_argument("outfile")
    ap.add_argument("--width", type=int, required=True)
    ap.add_argument("--height", type=int, required=True)
    a = ap.parse_args()

    aus, sps, pps = normalise_aus(split_access_units(open(a.infile, "rb").read()))

    with open(a.outfile, "wb") as f:
        f.write(b"X11VNCAU" + struct.pack(">II", a.width, a.height))
        for payload in aus:
            f.write(struct.pack(">I", len(payload)) + payload)
    print(f"{a.outfile}: {len(aus)} access units, {a.width}x{a.height}, "
          f"SPS {len(sps)}B + PPS {len(pps)}B on each")


if __name__ == "__main__":
    main()

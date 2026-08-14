#!/usr/bin/env python3
"""
latency.py - damage-to-client latency for a running x11vnc.

Flips a small on-screen rectangle between two colours and measures how long
until that change reaches a VNC client. This is the metric push model is
supposed to improve: it removes up to dwSamplingRateMs (16ms) of driver-side
sampling quantisation between an application drawing and NVFBC having a frame.

Protocol ordering matters. The FramebufferUpdateRequest is sent *before* the
flip, so the server already has a pending request and answers as soon as it
notices the change; otherwise a client round trip is folded into the result.

  ./latency.py --port 5901 --n 40

loadgen -pipe supplies the flip and timestamps it after XSync, so t0 means
"the X server has this", not "the request has been written to a socket".
Both processes read CLOCK_MONOTONIC, so the stamps are directly comparable.
"""

import argparse
import os
import statistics
import struct
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from rfbcheck import (handshake, set_bgra_format, set_encodings,      # noqa: E402
                      request_update, read_update, recv_exact)
import socket                                                          # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))


def dominant(data):
    """Most common pixel in a raw BGRA rect, as 0xRRGGBB."""
    counts = {}
    for i in range(0, len(data), 4):
        k = (data[i + 2] << 16) | (data[i + 1] << 8) | data[i]
        counts[k] = counts.get(k, 0) + 1
    return max(counts.items(), key=lambda kv: kv[1])[0] if counts else None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=5901)
    ap.add_argument("--rect", default="400,400,192,192", help="x,y,w,h to flip")
    ap.add_argument("--n", type=int, default=40, help="flips to measure")
    ap.add_argument("--gap", type=float, default=0.25, help="seconds between flips")
    ap.add_argument("--timeout", type=float, default=2.0, help="per-flip give-up")
    args = ap.parse_args()

    x, y, w, h = (int(v) for v in args.rect.split(","))

    gen = subprocess.Popen(
        [os.path.join(HERE, "loadgen"), "-g", f"{w}x{h}+{x}+{y}", "-pipe"],
        stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True, bufsize=1)
    if gen.stdout.readline().strip() != "ready":
        sys.exit("latency.py: loadgen did not come up")
    time.sleep(1.0)

    s = socket.create_connection((args.host, args.port), timeout=10)
    s.settimeout(10)
    fw, fh, _pf, name = handshake(s)
    set_bgra_format(s)
    set_encodings(s, (0,))
    print(f"server {fw}x{fh} \"{name}\"; flipping {w}x{h}+{x}+{y}, {args.n} samples")

    colors = [0x1040C0, 0xC04010]
    samples, misses = [], 0

    # settle: let the current contents reach the client
    request_update(s, x, y, w, h, incremental=0)
    read_update(s)

    # blocking reads must give up inside the per-flip deadline, not after the
    # generous handshake timeout
    s.settimeout(args.timeout)

    for i in range(args.n):
        want = colors[i % 2]

        # pending request FIRST so the server answers the moment it notices
        request_update(s, x, y, w, h, incremental=1)

        gen.stdin.write(f"{want:06x}\n")
        gen.stdin.flush()
        t0 = float(gen.stdout.readline().strip())

        deadline = time.monotonic() + args.timeout
        seen = None
        while time.monotonic() < deadline:
            try:
                rects = read_update(s)
            except (socket.timeout, EOFError):
                break
            t1 = time.monotonic()
            if any(dominant(d) == want for d in rects.values()):
                seen = (t1 - t0) * 1000.0
                break
            # not ours (or a partial paint): ask again and keep waiting
            request_update(s, x, y, w, h, incremental=1)

        if seen is None:
            misses += 1
        else:
            samples.append(seen)
        time.sleep(args.gap)

    gen.stdin.close()
    gen.terminate()
    s.close()

    if not samples:
        print("no samples captured")
        return 1

    samples.sort()
    def pct(p):
        return samples[min(len(samples) - 1, int(len(samples) * p))]
    print(f"  n={len(samples)} misses={misses}  "
          f"min={samples[0]:6.1f}  p50={pct(0.5):6.1f}  p90={pct(0.9):6.1f}  "
          f"max={samples[-1]:6.1f}  mean={statistics.mean(samples):6.1f}  ms")
    return 0


if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Structure check for the H.264 stream x11vnc puts on the wire.

Reads rfbcheck.py's --dump-h264 pair (FILE + FILE.log) and answers the three
questions that matter for TigerVNC's Media Foundation decoder:
  - does every access unit start with an SPS?
  - is the AU carrying H264_RESET_CONTEXT a real IDR, not a plain I slice?
  - how much of the payload is filler?
"""
import sys, collections

NAMES = {1: "non-IDR", 5: "IDR", 6: "SEI", 7: "SPS", 8: "PPS", 9: "AUD", 12: "FILLER"}

def nals(buf):
    i, n = 0, len(buf)
    starts = []
    while i < n - 3:
        if buf[i] == 0 and buf[i+1] == 0:
            if buf[i+2] == 1:
                starts.append((i + 3, 3))
                i += 3
                continue
            if i < n - 4 and buf[i+2] == 0 and buf[i+3] == 1:
                starts.append((i + 4, 4))
                i += 4
                continue
        i += 1
    for k, (s, _) in enumerate(starts):
        e = starts[k+1][0] - starts[k+1][1] if k + 1 < len(starts) else n
        yield buf[s] & 0x1f, s, e - s

def main(path):
    data = open(path, "rb").read()
    units = []
    with open(path + ".log") as f:
        for line in f:
            p = line.split()
            if len(p) >= 3 and p[0].isdigit():
                units.append((int(p[0]), int(p[1]), int(p[2], 0),
                              p[3] if len(p) > 3 else "?"))
    if not units:
        sys.exit("no unit index in %s.log - check its format:\n%s" %
                 (path, open(path + ".log").readline()))

    # The dump is two files and the client can be killed between writing the
    # index line and flushing the payload, so a trailing unit may be short.
    # Drop what is not actually present rather than reporting it as malformed.
    total_logged = sum(u[1] for u in units)
    if total_logged > len(data):
        keep, acc = 0, 0
        for u in units:
            if acc + u[1] > len(data):
                break
            acc += u[1]
            keep += 1
        print("note              : dump truncated %d B short; ignoring the "
              "last %d unit(s)" % (total_logged - len(data), len(units) - keep))
        units = units[:keep]

    off = 0
    kinds = collections.Counter()
    filler = total = 0
    bad_sps = reset_total = reset_idr = 0
    geoms = collections.Counter()
    for _, length, flags, geom in units:
        geoms[geom] += 1
        au = data[off:off+length]
        off += length
        total += length
        seq = [k for k, _, _ in nals(au)]
        for k, _, ln in nals(au):
            kinds[NAMES.get(k, k)] += 1
            if k == 12:
                filler += ln
        if not seq or seq[0] != 7:
            bad_sps += 1
        if flags & 0x1:
            reset_total += 1
            if 5 in seq:
                reset_idr += 1
    print("access units      : %d, %d bytes, mean %.0f B" %
          (len(units), total, total / len(units)))
    print("rect geometries   : %s" % dict(geoms))
    print("NAL types         : %s" % dict(kinds))
    print("SPS-first         : %d of %d AUs OK" % (len(units) - bad_sps, len(units)))
    print("RESET_CONTEXT     : %d, of which real IDR: %d" % (reset_total, reset_idr))
    print("filler            : %d B (%.1f%%)" % (filler, 100.0 * filler / max(total, 1)))
    ok = bad_sps == 0 and reset_idr == reset_total and filler == 0
    print("RESULT            : %s" % ("PASS" if ok else "FAIL"))
    return 0 if ok else 1

if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))

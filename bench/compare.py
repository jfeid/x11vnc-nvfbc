#!/usr/bin/env python3
"""
compare.py - diff two measure.py result sets.

  ./compare.py baseline after           # newest file for each label
  ./compare.py results/a.json results/b.json

Arrows mark the direction that is an improvement for each metric.
"""

import glob
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, "results")

# metric -> (label, lower_is_better)
METRICS = [
    ("cpu_pct",           "cpu%",        True),
    ("new_fps",           "new_fps",     False),
    ("grabs_per_sec",     "grabs/s",     True),
    ("grabs_per_frame",   "grabs/frm",   True),
    ("cpu_ms_per_frame",  "cpu_ms/frm",  True),
    ("est_capture_pct",   "capture%",    True),
    ("kb_per_sec",        "KB/s",        False),
]


def load(arg):
    if os.path.isfile(arg):
        path = arg
    else:
        hits = sorted(glob.glob(os.path.join(RESULTS, f"{arg}-*.json")))
        if not hits:
            sys.exit(f"compare.py: no results matching label {arg!r} in {RESULTS}")
        path = hits[-1]
    with open(path) as f:
        return path, json.load(f)


def fmt_delta(a, b, lower_better):
    if a is None or b is None:
        return "-", ""
    if a == 0:
        return f"{b:+.2f}", ""
    pct = 100.0 * (b - a) / abs(a)
    better = (b < a) if lower_better else (b > a)
    if abs(pct) < 2.0:
        mark = "  ="
    else:
        mark = " ++" if better else " --"
    return f"{pct:+6.1f}%", mark


def main():
    if len(sys.argv) != 3:
        sys.exit(__doc__.strip())
    pa, A = load(sys.argv[1])
    pb, B = load(sys.argv[2])

    print(f"A = {os.path.basename(pa)}   {A['context'].get('git_head','?')}"
          f"{' +dirty' if A['context'].get('git_dirty') else ''}"
          f"   clients={A['context'].get('vnc_clients')}")
    print(f"B = {os.path.basename(pb)}   {B['context'].get('git_head','?')}"
          f"{' +dirty' if B['context'].get('git_dirty') else ''}"
          f"   clients={B['context'].get('vnc_clients')}")

    if A["context"].get("x11vnc_cmdline") != B["context"].get("x11vnc_cmdline"):
        print("\nNOTE: server command lines differ between runs:")
        print(f"  A: {A['context'].get('x11vnc_cmdline')}")
        print(f"  B: {B['context'].get('x11vnc_cmdline')}")
    if A["context"].get("vnc_clients") != B["context"].get("vnc_clients"):
        print("\nWARNING: client count differs; encoding CPU is not comparable.")

    ra = {r["scenario"]: r for r in A["results"]}
    rb = {r["scenario"]: r for r in B["results"]}
    shared = [s for s in ra if s in rb]
    if not shared:
        sys.exit("compare.py: no scenarios in common")

    for scen in shared:
        a, b = ra[scen], rb[scen]
        print(f"\n{scen}" + ("   (uncontrolled - reflects live desktop activity, "
                             "treat as context only)" if scen == "idle" else ""))
        print(f"  {'metric':12s} {'A':>10s} {'B':>10s} {'delta':>9s}")
        for key, label, lower in METRICS:
            va, vb = a.get(key), b.get(key)
            d, mark = fmt_delta(va, vb, lower)
            sa = "-" if va is None else f"{va:.2f}"
            sb = "-" if vb is None else f"{vb:.2f}"
            print(f"  {label:12s} {sa:>10s} {sb:>10s} {d:>9s}{mark}")

    print("\n  ++ improved   -- regressed   = within noise (<2%)")
    print("  grabs/frm is the headline check: ~1.0 means one grab per scan cycle landed.")
    print("\n  CAUTION: new_fps counts frames NVFBC reported as new TO US, which scales")
    print("  with how often we poll - it is NOT the frame rate a client receives. A build")
    print("  that polls thousands of times/sec observes nearly every generated frame while")
    print("  delivering far fewer. To compare delivered frames, use ab.sh (identical client,")
    print("  identical load, both builds) and read updates/s.")


if __name__ == "__main__":
    main()

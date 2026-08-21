#!/usr/bin/env python3
"""
measure.py - reproducible capture-path measurements for x11vnc-nvfbc.

Records, per scenario, what the running x11vnc actually costs and does:

  cpu_pct            x11vnc CPU as % of one core (utime+stime delta)
  grabs_per_sec      NVFBC grab calls issued        (from the server's own log)
  new_fps            NVFBC frames that were actually new
  grabs_per_frame    grab calls per useful frame  <-- headline ratio
  cpu_ms_per_frame   CPU cost amortised per useful frame
  kb_per_sec         bytes actually delivered to VNC clients (from ss)

Run it before a change and again after; diff the two result files.

  ./measure.py --label baseline
  ./measure.py --label baseline --no-load     # passive only, nothing drawn on screen
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime

HERE = os.path.dirname(os.path.abspath(__file__))
RESULTS = os.path.join(HERE, "results")
LOG = "/var/log/x11vnc.log"
CLK_TCK = os.sysconf("SC_CLK_TCK")

# name -> (geometry or None, target fps, dirty fraction)
SCENARIOS = {
    "idle":   (None,                60, 1.0),
    "small":  ("320x240+64+64",     60, 1.0),
    "medium": ("960x540+64+64",     60, 1.0),
    "full":   ("2560x1440+0+0",     60, 1.0),
    "sparse": ("2560x1440+0+0",     60, 0.02),   # scattered small changes
}


def sh(cmd, **kw):
    return subprocess.run(cmd, shell=True, capture_output=True, text=True, **kw).stdout


def find_x11vnc():
    out = sh("pgrep -x x11vnc").split()
    if not out:
        sys.exit("measure.py: no running x11vnc process found")
    return int(out[0])


def cmdline(pid):
    try:
        with open(f"/proc/{pid}/cmdline", "rb") as f:
            return " ".join(f.read().decode(errors="replace").split("\0")).strip()
    except OSError:
        return "?"


def binary_path(pid):
    """/proc/PID/exe needs root when x11vnc runs as root, so use argv[0].

    Identifying the build is the whole point of recording this, so falling
    back to an empty string would quietly break before/after attribution.
    """
    exe = sh(f"readlink -f /proc/{pid}/exe 2>/dev/null").strip()
    if exe:
        return exe
    argv0 = cmdline(pid).split(" ")[0]
    if argv0.startswith("/") and os.path.exists(argv0):
        return argv0
    return shutil.which(os.path.basename(argv0) or "x11vnc") or "?"


def sha256(path):
    if not path or not os.path.exists(path):
        return None
    out = sh(f"sha256sum {path} 2>/dev/null").split()
    return out[0] if out else None


def cpu_ticks(pid):
    """utime+stime in clock ticks. comm may contain spaces, so parse past ')'."""
    with open(f"/proc/{pid}/stat") as f:
        rest = f.read().rsplit(")", 1)[1].split()
    return int(rest[11]) + int(rest[12])       # utime, stime (0-indexed after state)


def vnc_bytes():
    """Total bytes sent on all established connections from the VNC port."""
    out = sh("ss -tin state established '( sport = :5900 )' 2>/dev/null")
    return sum(int(m) for m in re.findall(r"bytes_sent:(\d+)", out))


def vnc_clients():
    out = sh("ss -tn state established '( sport = :5900 )' 2>/dev/null")
    return max(0, len([l for l in out.splitlines() if ":5900" in l]))


CLIENT_COMPRESS_RE = re.compile(r"Using compression level (\d+) for client (\S+)")
CLIENT_QUALITY_RE = re.compile(r"Using image quality level (\d+) for client (\S+)")
H264_CLIENT_RE = re.compile(
    r"h264: client (\S+) offers encoding 50 \((\w+)")
H264_MODE_RE = re.compile(r"h264: (motion|quiet) ([\d.]+) screens/s")

CLIENT_ENCODING_RE = re.compile(
    r"(?:Using (\S+) encoding|Switching from \S+ to (\S+) Encoding) for client (\S+)")


def read_client_encoding():
    """Per-client Tight settings, as libvncserver last logged them.

    Compression level and JPEG quality are chosen by the *client* in
    SetEncodings, and they drive server encode cost directly - a result set is
    not interpretable without them.  The server logs them on every SetEncodings,
    so the last value seen for a client is the one in force.  Recording it here
    means no run is ever ambiguous about which client settings produced it.
    """
    latest = {}
    try:
        with open(LOG, "r", errors="replace") as fh:
            for line in fh:
                m = CLIENT_COMPRESS_RE.search(line)
                if m:
                    latest.setdefault(m.group(2), {})["compress_level"] = int(m.group(1))
                    continue
                m = CLIENT_QUALITY_RE.search(line)
                if m:
                    latest.setdefault(m.group(2), {})["quality_level"] = int(m.group(1))
                    continue
                m = CLIENT_ENCODING_RE.search(line)
                if m:
                    latest.setdefault(m.group(3), {})["encoding"] = m.group(1) or m.group(2)
                    continue
                m = H264_CLIENT_RE.search(line)
                if m:
                    # "preferred" means the viewer asked for H.264, so the
                    # hybrid is live for this client; "not" means pure Tight.
                    latest.setdefault(m.group(1), {})["h264"] = (
                        m.group(2) == "preferred")
    except OSError as e:
        return {"error": f"cannot read {LOG}: {e}"}
    return latest


STAT_RE = re.compile(
    r"NVFBC stats: ([\d.]+) new fps, ([\d.]+) grabs/sec, (\d+) new frames / (\d+) total grabs"
)


def read_log_stats(fh, t_start):
    """Consume log lines appended since fh's offset.

    Each stats line summarises the interval [T-elapsed, T]; drop any whose
    interval began before the scenario did, so pre-scenario activity does not
    leak into the numbers.
    """
    rows = []
    now = time.time()
    for line in fh:
        m = STAT_RE.search(line)
        if not m:
            continue
        new_fps, gps, frames, grabs = (
            float(m.group(1)), float(m.group(2)), int(m.group(3)), int(m.group(4))
        )
        elapsed = grabs / gps if gps > 0 else 0.0
        # lines are appended in real time, so "now" approximates T for the last
        # one; walk backwards using cumulative elapsed for earlier ones instead
        rows.append({"new_fps": new_fps, "grabs_per_sec": gps,
                     "frames": frames, "grabs": grabs, "elapsed": elapsed})
    # drop a leading partial interval that predates the scenario
    total = sum(r["elapsed"] for r in rows)
    if rows and total > (now - t_start) + 1.0:
        rows = rows[1:]
    return rows


def run_nvfloor(path):
    """Measure this machine's NVFBC cost floor; returns the #KV dict."""
    if not os.path.exists(path):
        return {}
    out = sh(f"timeout 180 {path} 2>/dev/null")
    kv = {}
    for m in re.finditer(r"^#KV (\S+)=(\S+)$", out, re.M):
        k, v = m.group(1), m.group(2)
        try:
            kv[k] = float(v)
        except ValueError:
            kv[k] = v
    return kv


def pick_target_output(floor, clip):
    """Which output does x11vnc's served region correspond to, if any?

    That output's grab cost is the floor the recommended fixes converge to.
    """
    if not clip:
        return None, None
    for k, v in floor.items():
        if k.startswith("output_geom_") and v == clip:
            name = k[len("output_geom_"):]
            return name, floor.get(f"grab_output_{name}_us")
    return None, None


def derive_capture_model(r, floor, target_us):
    """Split measured CPU into estimated capture cost vs everything else.

    Capture is modelled from the machine's own floor:
      one real grab per new frame, plus a cheap poll for every redundant one.
    The projection assumes the two headline fixes: one grab per scan cycle,
    and capturing only the served region.
    """
    full_us = floor.get("grab_full_us")
    nonew_us = floor.get("grab_nonew_us")
    fps, gps, cpu = r.get("new_fps"), r.get("grabs_per_sec"), r.get("cpu_pct")
    if not (full_us and nonew_us and fps and gps and cpu):
        return
    redundant = max(0.0, gps - fps)
    capture_ms_s = fps * full_us / 1000.0 + redundant * nonew_us / 1000.0
    r["est_capture_pct"] = round(capture_ms_s / 10.0, 1)
    r["est_capture_share"] = round(100.0 * (capture_ms_s / 10.0) / cpu, 1)
    if target_us:
        proj_ms_s = fps * target_us / 1000.0          # one grab/frame, cropped
        r["proj_capture_pct"] = round(proj_ms_s / 10.0, 1)
        r["proj_cpu_pct"] = round(cpu - (capture_ms_s - proj_ms_s) / 10.0, 1)


def run_scenario(name, pid, duration, use_load, loadgen, blit=False):
    geom, fps, frac = SCENARIOS[name]
    label = f"{name:7s}"
    if geom and use_load:
        how = "blit" if blit else f"frac={frac}"
        print(f"  {label} load {geom} @{fps}fps {how} for {duration}s ...", flush=True)
    else:
        print(f"  {label} passive for {duration}s ...", flush=True)

    try:
        fh = open(LOG, "r", errors="replace")
        fh.seek(0, os.SEEK_END)
    except OSError as e:
        print(f"    (cannot read {LOG}: {e})")
        fh = None

    time.sleep(1.0)                                   # settle
    t0, c0, b0 = time.time(), cpu_ticks(pid), vnc_bytes()

    proc = None
    if geom and use_load:
        cmd = [loadgen, "-g", geom, "-r", str(fps), "-d", str(duration),
               "-f", str(frac), "-t", f"loadgen-{name}"]
        if blit:
            cmd.append("-blit")       # note: blit redraws the whole surface, so -f is ignored
        proc = subprocess.Popen(
            cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True)
        out, _ = proc.communicate(timeout=duration + 30)
        achieved = None
        for line in (out or "").splitlines():
            m = re.search(r"= ([\d.]+) achieved fps", line)
            if m:
                achieved = float(m.group(1))
    else:
        time.sleep(duration)
        achieved = None

    t1, c1, b1 = time.time(), cpu_ticks(pid), vnc_bytes()
    wall = t1 - t0
    cpu_s = (c1 - c0) / CLK_TCK

    rows = read_log_stats(fh, t0) if fh else []
    if fh:
        fh.close()

    tot_elapsed = sum(r["elapsed"] for r in rows)
    tot_frames = sum(r["frames"] for r in rows)
    tot_grabs = sum(r["grabs"] for r in rows)

    r = {
        "scenario": name,
        "geometry": geom if use_load else None,
        "target_fps": fps if (geom and use_load) else None,
        "dirty_frac": frac if (geom and use_load) else None,
        "loadgen_achieved_fps": achieved,
        "wall_s": round(wall, 2),
        "cpu_pct": round(100.0 * cpu_s / wall, 1) if wall else None,
        "cpu_s": round(cpu_s, 3),
        "kb_per_sec": round((b1 - b0) / 1024.0 / wall, 1) if wall else None,
        "log_intervals": len(rows),
        "log_covered_s": round(tot_elapsed, 1),
        "new_fps": round(tot_frames / tot_elapsed, 2) if tot_elapsed else None,
        "grabs_per_sec": round(tot_grabs / tot_elapsed, 1) if tot_elapsed else None,
        "grabs_per_frame": round(tot_grabs / tot_frames, 1) if tot_frames else None,
        "cpu_ms_per_frame": round(1000.0 * cpu_s / tot_frames, 2) if tot_frames else None,
    }
    print("    " + " ".join(
        f"{k}={r[k]}" for k in
        ("cpu_pct", "new_fps", "grabs_per_sec", "grabs_per_frame",
         "cpu_ms_per_frame", "kb_per_sec") if r[k] is not None))
    return r


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--label", default="run", help="name for this result set")
    ap.add_argument("--duration", type=int, default=30, help="seconds per scenario")
    ap.add_argument("--scenarios", default="idle,small,medium,full",
                    help="comma-separated: " + ",".join(SCENARIOS))
    ap.add_argument("--no-load", action="store_true",
                    help="passive only; draws nothing on the user's screen")
    ap.add_argument("--no-floor", action="store_true",
                    help="skip the NVFBC cost-floor probe (no second capture session)")
    ap.add_argument("--blit", action="store_true",
                    help="loadgen draws one full-surface image per frame (video-like) "
                         "instead of solid cells; the default cell load is encoded as "
                         "Tight fills/palettes and never reaches JPEG, so use this to "
                         "measure anything involving JPEG quality. Ignores each "
                         "scenario's dirty fraction, which makes 'sparse' meaningless.")
    args = ap.parse_args()

    pid = find_x11vnc()
    loadgen = os.path.join(HERE, "loadgen")
    use_load = not args.no_load
    if use_load and not os.path.exists(loadgen):
        sys.exit(f"measure.py: {loadgen} not built (see bench/README.md)")

    names = [s.strip() for s in args.scenarios.split(",") if s.strip()]
    for n in names:
        if n not in SCENARIOS:
            sys.exit(f"measure.py: unknown scenario {n!r}")
    if not use_load:
        names = ["idle"]

    ctx = {
        "label": args.label,
        "timestamp": datetime.now().isoformat(timespec="seconds"),
        "pid": pid,
        "x11vnc_cmdline": cmdline(pid),
        "x11vnc_binary": binary_path(pid),
        "binary_sha256": sha256(binary_path(pid)),
        "binary_mtime": sh("stat -c %%y %s 2>/dev/null" % binary_path(pid)).strip(),
        "git_head": sh("git -C %s/../x11vnc rev-parse --short HEAD 2>/dev/null" % HERE).strip(),
        "git_dirty": bool(sh("git -C %s/../x11vnc status --porcelain -- src 2>/dev/null" % HERE).strip()),
        "gpu": sh("nvidia-smi --query-gpu=name,driver_version --format=csv,noheader").strip(),
        "x_screen": sh("DISPLAY=%s xdpyinfo 2>/dev/null | awk '/dimensions/{print $2}'"
                       % os.environ.get("DISPLAY", ":1")).strip(),
        "vnc_clients": vnc_clients(),
        "client_encoding": read_client_encoding(),
        "duration_s": args.duration,
        "load_enabled": use_load,
        "blit": args.blit,
    }

    print(f"\nx11vnc pid {pid}  clients={ctx['vnc_clients']}  head={ctx['git_head']}"
          f"{' +dirty' if ctx['git_dirty'] else ''}")
    print(f"cmdline: {ctx['x11vnc_cmdline']}")
    ce = ctx["client_encoding"]
    if not ce:
        print("client encoding: not found in %s - settings unknown for this run" % LOG)
    elif "error" in ce:
        print("client encoding: %s" % ce["error"])
    else:
        for who, st in ce.items():
            print(f"client encoding: {who} {st.get('encoding','?')} "
                  f"compress={st.get('compress_level','?')} "
                  f"quality={st.get('quality_level','?')}")
    print(f"load shape: {'blit (full-surface image/frame)' if args.blit else 'cells (solid fills)'}\n")
    if ctx["vnc_clients"] == 0:
        print("WARNING: no VNC client connected - the server does far less work.\n"
              "         Connect a client for numbers comparable to real use.\n")

    floor = {} if args.no_floor else run_nvfloor(os.path.join(HERE, "nvfloor"))
    ctx["nvfbc_floor"] = floor
    m = re.search(r"-clip\s+(\S+)", ctx["x11vnc_cmdline"])
    clip = m.group(1) if m else None
    ctx["clip"] = clip
    tgt_name, tgt_us = pick_target_output(floor, clip)
    ctx["clip_matches_output"] = tgt_name
    if floor:
        print(f"NVFBC floor: redundant grab {floor.get('grab_nonew_us','?')} us, "
              f"full-screen grab {floor.get('grab_full_us','?')} us")
        if tgt_name:
            print(f"             -clip {clip} == output {tgt_name}; "
                  f"output-tracked grab {tgt_us} us  <- what the fixes converge to")
        elif clip:
            print(f"             -clip {clip} matches no single output; "
                  f"needs captureBox+frameSize rather than output tracking")
        print()

    results = [run_scenario(n, pid, args.duration, use_load, loadgen, args.blit)
               for n in names]
    for r in results:
        derive_capture_model(r, floor, tgt_us)

    os.makedirs(RESULTS, exist_ok=True)
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    path = os.path.join(RESULTS, f"{args.label}-{stamp}.json")
    with open(path, "w") as f:
        json.dump({"context": ctx, "results": results}, f, indent=2)

    def g(r, k):
        v = r.get(k)
        return "-" if v is None else str(v)

    print(f"\n{'scenario':9s} {'cpu%':>6s} {'new_fps':>8s} {'grabs/s':>9s} "
          f"{'grabs/frm':>10s} {'cpu_ms/frm':>11s} {'KB/s':>8s}")
    for r in results:
        print(f"{r['scenario']:9s} {g(r,'cpu_pct'):>6s} {g(r,'new_fps'):>8s} "
              f"{g(r,'grabs_per_sec'):>9s} {g(r,'grabs_per_frame'):>10s} "
              f"{g(r,'cpu_ms_per_frame'):>11s} {g(r,'kb_per_sec'):>8s}")

    if any("est_capture_pct" in r for r in results):
        print(f"\ncapture cost modelled from this machine's NVFBC floor:")
        print(f"{'scenario':9s} {'cpu%':>6s} {'capture%':>9s} {'of cpu':>7s} "
              f"{'proj capture%':>14s} {'proj cpu%':>10s}")
        for r in results:
            print(f"{r['scenario']:9s} {g(r,'cpu_pct'):>6s} {g(r,'est_capture_pct'):>9s} "
                  f"{g(r,'est_capture_share')+'%':>7s} {g(r,'proj_capture_pct'):>14s} "
                  f"{g(r,'proj_cpu_pct'):>10s}")
        print("  proj* = one grab per scan cycle + capture only the served region.\n"
              "  Everything above proj cpu% is scan/compare/copy and libvncserver encoding.")

    print(f"\nwrote {path}")


if __name__ == "__main__":
    main()

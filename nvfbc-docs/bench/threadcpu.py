#!/usr/bin/env python3
"""Per-thread CPU split of a running x11vnc, over a sampling window.

Answers "which thread is the 118%?" without a profiler: watch_loop (scan,
capture, encode) and libvncserver's per-client threads are separate tasks, so
their utime/stime deltas attribute the cost to one side or the other.
"""
import os, sys, time

def snap(pid):
    out = {}
    base = "/proc/%d/task" % pid
    for tid in os.listdir(base):
        try:
            with open("%s/%s/stat" % (base, tid)) as f:
                p = f.read().rsplit(") ", 1)[1].split()
            with open("%s/%s/comm" % (base, tid)) as f:
                comm = f.read().strip()
            vol = nonvol = 0
            with open("%s/%s/status" % (base, tid)) as f:
                for line in f:
                    if line.startswith("voluntary_ctxt_switches"):
                        vol = int(line.split()[1])
                    elif line.startswith("nonvoluntary_ctxt_switches"):
                        nonvol = int(line.split()[1])
            # utime, stime (ticks), and context switches: a userspace spin loop
            # is almost all utime with almost no voluntary switches; polling
            # through an ioctl shows up as stime and a switch per call.
            out[tid] = (comm, int(p[11]), int(p[12]), vol, nonvol)
        except (IOError, IndexError):
            pass
    return out

pid = int(sys.argv[1])
dur = float(sys.argv[2]) if len(sys.argv) > 2 else 15.0
hz = os.sysconf("SC_CLK_TCK")
a = snap(pid)
time.sleep(dur)
b = snap(pid)

rows = []
for tid, (comm, u1, s1, v1, n1) in b.items():
    _, u0, s0, v0, n0 = a.get(tid, (comm, u1, s1, v1, n1))
    up = 100.0 * (u1 - u0) / hz / dur
    sp = 100.0 * (s1 - s0) / hz / dur
    if up + sp >= 0.05:
        rows.append((up + sp, up, sp, (v1 - v0) / dur, (n1 - n0) / dur, tid, comm))
rows.sort(reverse=True)
print("thread CPU over %.0f s (pid %d)" % (dur, pid))
print("     tot     user      sys    volsw/s  nonvol/s  thread")
for tot, up, sp, v, n, tid, comm in rows:
    print("  %6.1f%%  %6.1f%%  %6.1f%%  %9.1f %9.1f  %s (%s)" % (tot, up, sp, v, n, comm, tid))
print("  %6.1f%%  TOTAL" % sum(r[0] for r in rows))

#!/usr/bin/env bash
# h264-testserver.sh - throwaway H.264 server on a spare port for testing the
# fence flow-control fix (plan §18) WITHOUT touching production on 5900.
#
#   ./h264-testserver.sh [PORT]        # default 5906; runs from any directory
#
# It runs the live -h264 path with production-like gate settings.
#
#   NVFBC=1 ./h264-testserver.sh 5906   <- USE THIS to reproduce the freeze.
#
# Capture method is not a detail here: the gate's metric is dirty area per
# second, which scales with how often the server scans.  Under X11 capture the
# scan rate is low and damage coalesces (one dirty region per scan instead of
# 60/s), so a 960x540@60 load measures far below the 8 screens/s threshold and
# H.264 NEVER ENGAGES - the bench then runs pure Tight and proves nothing.
# With -nvfbc_push (20-60 grabs/s, as production runs) the same load measures
# ~8 screens/s and trips the gate, which is the condition the freeze needs.
# Default stays X11 capture because production normally holds an NVFBC session;
# a second one is safe in practice (bench/nvfloor opens one alongside the live
# server) and falls back to X11 capture if the driver refuses.
#   - -repeat: without it x11vnc leaves the user's desktop auto-repeat off on a
#     kill -9 (see README "Hazard"). -noipv6: dodge the IPv6 accept hang.
#
# To reproduce/observe: connect the REAL client to PORT (tunnel -L PORT), or a
# synthetic consumer:
#   python3 rfbcheck.py --host 127.0.0.1 --port PORT --h264 --fence --stream 30
#   python3 rfbcheck.py --host 127.0.0.1 --port PORT --h264 --fence --dump-h264 /tmp/au
# then check the IDR ratio (an all-IDR stream means the hold logic regressed):
#   awk '{n++; if ($3) idr++} END {print idr"/"n" access units carry a reset flag"}' /tmp/au.log
#
# A/B the fix: start it once each way.  Do NOT reach for -R here - remote
# control goes through a single X11VNC_REMOTE property on the display and needs
# exactly ONE x11vnc running, and production on 5900 is normally up, so -R would
# aim at the wrong server (this already invalidated one test - see the plan).
#   NOFENCE=1 ./h264-testserver.sh 5906    # fences off -> should be able to freeze
#            ./h264-testserver.sh 5906    # fences on  -> paced
# (-R h264_fence:0|1 is only safe once production on 5900 is stopped.)
#
# Stop it: Ctrl-C, or kill -9 the PID it prints (pkill is unreliable in some
# sandboxes). -repeat means a kill -9 cannot strand the desktop's auto-repeat.
set -euo pipefail

# Resolve everything against the script's own directory: this has to work when
# invoked as nvfbc-docs/bench/h264-testserver.sh from the repo root, not just from bench/.
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PORT="${1:-5906}"
BIN="${BIN:-$HERE/../../src/x11vnc}"
: "${DISPLAY:=:1}"
: "${XAUTHORITY:=/run/user/1000/gdm/Xauthority}"
export DISPLAY XAUTHORITY
LOG="/tmp/x11vnc-h264-test-$PORT.log"
# NOFENCE=1 runs the "before" leg: the unpaced push that the freeze came from.
CAP_ARGS=()
CAP_DESC="X11 capture (gate may never trip - see NVFBC=1 above)"
if [ -n "${NVFBC:-}" ]; then
	CAP_ARGS=(-nvfbc -nvfbc_nocursor -nvfbc_push)
	CAP_DESC="NVFBC push (matches production; what the gate needs)"
fi
FENCE_ARGS=()
FENCE_DESC="on"
if [ -n "${NOFENCE:-}" ]; then
	FENCE_ARGS=(-h264_nofence)
	FENCE_DESC="OFF (unpaced - the pre-fix behaviour)"
fi

die() { echo "h264-testserver: $*" >&2; exit 1; }

# Never aim this at the live service. x11vnc would fail to bind anyway, but an
# explicit refusal beats finding out from a confusing error - or from the
# operator losing the session they are judging with.
[ "$PORT" = "5900" ] && die "port 5900 is the production service; pick a spare port (default 5906)"

[ -x "$BIN" ] || die "no x11vnc binary at $BIN
  build it:  (cd $HERE/../.. && make -j\$(nproc) -C src x11vnc)
  or point BIN at one:  BIN=/path/to/x11vnc $0 $PORT"

if command -v ss >/dev/null 2>&1 && ss -tlnH 2>/dev/null | awk -v p=":$PORT\$" '$4 ~ p {found=1} END {exit !found}'; then
	die "port $PORT is already in use - stop that server first, or pass another port"
fi

if command -v xdpyinfo >/dev/null 2>&1 && ! xdpyinfo >/dev/null 2>&1; then
	die "cannot open display '$DISPLAY' with XAUTHORITY='$XAUTHORITY'
  the captured GDM session is usually :1 with /run/user/1000/gdm/Xauthority
  override with:  DISPLAY=:N XAUTHORITY=/path/to/Xauthority $0 $PORT"
fi

echo "h264-testserver: $BIN"
echo "  port $PORT (localhost only), display $DISPLAY, $CAP_DESC
  H.264 fences: $FENCE_DESC, ${FPS:-30} fps cap, gate enters at ${ENTER:-8} screens/s"
echo "  log  $LOG"
echo "  production on 5900 is untouched; Ctrl-C or kill -9 to stop"
exec "$BIN" \
  -rfbport "$PORT" -localhost -noipv6 -nopw -forever -shared -threads -repeat \
  -clip 2560x1440+0+0 -nocursor \
  "${CAP_ARGS[@]}" \
  -h264 -h264_bitrate 20000 -h264_fps "${FPS:-30}" -h264_enter "${ENTER:-8}" \
  "${FENCE_ARGS[@]}" \
  -o "$LOG"

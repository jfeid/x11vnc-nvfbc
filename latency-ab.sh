#!/bin/bash
# latency-ab.sh - damage-to-client latency per capture configuration.
#
# Push model is supposed to buy latency, not throughput: it removes up to
# dwSamplingRateMs (16ms) of driver-side sampling between an application
# drawing and NVFBC having a frame.  ab.sh cannot see that, so this drives
# latency.py against each configuration in turn.
#
#   ./latency-ab.sh            # small flip rect, direct capture cannot engage
#   FULLSCREEN=1 ./latency-ab.sh   # fullscreen rect, so direct capture can
set -u
BENCH="$(cd "$(dirname "$0")" && pwd)"
BIN="${BIN:-$BENCH/../x11vnc/src/x11vnc}"
N="${N:-40}"
CLIP="${CLIP:-2560x1440+0+0}"
export DISPLAY="${DISPLAY:-:1}"
export XAUTHORITY="${XAUTHORITY:-/run/user/1000/gdm/Xauthority}"

if [ "${FULLSCREEN:-0}" = 1 ]; then
    RECT="0,0,2560,1440"
    echo "flip rect: fullscreen (direct capture can engage)"
else
    RECT="400,400,192,192"
    # NB: direct capture still engages here. On a compositing desktop the
    # fullscreen unoccluded application is the compositor itself (gnome-shell),
    # which is what NvFBC attaches to - the flip rect size is irrelevant.
    echo "flip rect: small (compositor is still the fullscreen app for direct capture)"
fi
echo "binary: $BIN   samples: $N"
echo

port=5941
run(){
    local label="$1"; shift
    local log="/tmp/lat-$(id -u)-$port.log"
    rm -f "$log"
    "$BIN" -display "$DISPLAY" -auth "$XAUTHORITY" -forever -shared -nopw -localhost \
        -rfbport "$port" "$@" -clip "$CLIP" -threads -wait 5 -defer 10 \
        -o "$log" >/dev/null 2>&1 &
    sleep 6
    local srv; srv=$(pgrep -f "rfbport $port" | head -1)
    if [ -z "$srv" ]; then echo "  $label: FAILED TO START"; port=$((port+1)); return 1; fi

    printf "  %-26s " "$label"
    "$BENCH/latency.py" --port "$port" --rect "$RECT" --n "$N" --gap 0.2 2>&1 \
        | tail -1 | sed 's/^ *//'

    # did NVFBC actually engage, and did direct capture?
    local mode=""
    grep -q "NVFBC: Capture initialized" "$log" && mode="nvfbc"
    grep -q "direct capture active" "$log" && mode="$mode+DIRECT-ENGAGED"
    [ -n "$mode" ] && echo "                             [$mode]"

    kill -9 "$srv" 2>/dev/null
    port=$((port+1))
    sleep 3
}

# REVERSE=1 runs the configurations in the opposite order. Position in the
# sweep is a confound - the first config measured is not obviously comparable
# to the last - so a reversed run is the control that separates a real effect
# from an ordering artefact.
if [ "${REVERSE:-0}" = 1 ]; then
    echo "(reversed order)"
    run "shm (no nvfbc)"         -nonvfbc
    run "nvfbc +direct"          -nvfbc -nvfbc_direct
    run "nvfbc +push"            -nvfbc -nvfbc_nocursor -nvfbc_push
    run "nvfbc (current)"        -nvfbc -nvfbc_nocursor
else
    run "nvfbc (current)"        -nvfbc -nvfbc_nocursor
    run "nvfbc +push"            -nvfbc -nvfbc_nocursor -nvfbc_push
    run "nvfbc +direct"          -nvfbc -nvfbc_direct
    run "shm (no nvfbc)"         -nonvfbc
fi

echo
echo "lower is better; p50 is the number to compare."

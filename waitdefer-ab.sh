#!/bin/bash
# waitdefer-ab.sh - does -wait/-defer still matter once push model is on?
#
# -wait and -defer are x11vnc/libvncserver scheduling, independent of NVFBC's
# driver-side sampling: -wait is the sleep between scan cycles, -defer is
# libvncserver's coalescing delay before sending. Push model does not touch
# either, so they remain in the latency path - the question is how much of it
# they now account for.
#
#   ./waitdefer-ab.sh            # latency
#   MODE=tput ./waitdefer-ab.sh  # CPU and delivered frames
set -u
BENCH="$(cd "$(dirname "$0")" && pwd)"
BIN="${BIN:-$BENCH/../x11vnc/src/x11vnc}"
N="${N:-100}"
MODE="${MODE:-lat}"
GEOM="${GEOM:-960x540+64+64}"
SECS="${SECS:-20}"
CLIP="${CLIP:-2560x1440+0+0}"
export DISPLAY="${DISPLAY:-:1}"
export XAUTHORITY="${XAUTHORITY:-/run/user/1000/gdm/Xauthority}"

# every variant keeps -nvfbc_push, matching production
VARIANTS=(
    "wait5 defer10  (production)|-wait 5 -defer 10"
    "wait1 defer1   (minimum)   |-wait 1 -defer 1"
    "wait10 defer10 (auto-tuned)|-wait 10 -defer 10"
    "wait20 defer20 (upstream)  |-wait 20 -defer 20"
)

echo "mode=$MODE  push enabled in every variant  binary=$(basename "$BIN")"
[ "$MODE" = lat ] && echo "latency: $N samples each" || echo "throughput: $GEOM @60fps, ${SECS}s each"
echo

port=5961
for v in "${VARIANTS[@]}"; do
    label="${v%%|*}"; flags="${v#*|}"
    log="/tmp/wd-$(id -u)-$port.log"; rm -f "$log"
    # shellcheck disable=SC2086
    "$BIN" -display "$DISPLAY" -auth "$XAUTHORITY" -forever -shared -nopw -localhost \
        -rfbport "$port" -nvfbc -nvfbc_nocursor -nvfbc_push -clip "$CLIP" \
        -threads $flags -o "$log" >/dev/null 2>&1 &
    sleep 6
    srv=$(pgrep -f "rfbport $port" | head -1)
    if [ -z "$srv" ]; then echo "  $label FAILED TO START"; port=$((port+1)); continue; fi

    if [ "$MODE" = lat ]; then
        printf "  %-28s " "$label"
        "$BENCH/latency.py" --port "$port" --rect 400,400,192,192 --n "$N" --gap 0.2 2>&1 \
            | tail -1 | sed 's/^ *//'
    else
        "$BENCH/loadgen" -g "$GEOM" -r 60 -d $((SECS+8)) >/dev/null 2>&1 &
        gen=$!
        sleep 2
        c0=$(awk '{r=substr($0,index($0,") ")+2);split(r,f," ");print f[12]+f[13]}' "/proc/$srv/stat")
        out=$("$BENCH/rfbcheck.py" --port "$port" --stream "$SECS" 2>&1 | tail -1)
        c1=$(awk '{r=substr($0,index($0,") ")+2);split(r,f," ");print f[12]+f[13]}' "/proc/$srv/stat")
        LC_ALL=C printf "  %-28s cpu=%5.1f%%  %s\n" "$label" \
            "$(LC_ALL=C echo "scale=1;($c1-$c0)/$SECS"|bc)" "$out"
        kill "$gen" 2>/dev/null
    fi

    kill -9 "$srv" 2>/dev/null
    port=$((port+1))
    sleep 3
done

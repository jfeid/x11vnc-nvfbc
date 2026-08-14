#!/bin/bash
# ab.sh - run N x11vnc builds head-to-head under an identical load and an
# identical client, on throwaway ports.
#
# Raw encoding is used deliberately: delivered bytes are then proportional to
# the screen area each build marks modified, which is the thing worth
# comparing.  updates/s is the delivered frame rate - unlike the server's own
# NVFBC "new fps", which scales with how often it polls and is not a delivery
# metric.
#
# NVFBC flags are added only for builds that support them, so a pre-NVFBC
# build can be compared against the fork directly.
#
# Each argument is a command string: first token is the binary, any remaining
# tokens are extra server flags.
#
#   ./ab.sh /path/stock/x11vnc "/path/stock/x11vnc -noshm" ../x11vnc/src/x11vnc
#   GEOM=960x540+64+64 SECS=25 ./ab.sh ...
set -u
BENCH="$(cd "$(dirname "$0")" && pwd)"
GEOM="${GEOM:-960x540+64+64}"
SECS="${SECS:-20}"
CLIP="${CLIP:-2560x1440+0+0}"
export DISPLAY="${DISPLAY:-:1}" XAUTHORITY="${XAUTHORITY:-/run/user/1000/gdm/Xauthority}"

cpu(){ awk '{r=substr($0,index($0,") ")+2);split(r,f," ");print f[12]+f[13]}' "/proc/$1/stat" 2>/dev/null; }

port=5911
run(){
    # uid in the path: /tmp is sticky, so a root run would otherwise leave
    # logs the session user cannot replace, and every later run fails to start
    local spec="$1" log="/tmp/ab-$(id -u)-$port.log" nvargs=()
    # shellcheck disable=SC2206
    local words=($spec) bin extra
    bin="${words[0]}"; extra=("${words[@]:1}")
    rm -f "$log"

    # only pass NVFBC flags to builds that have them, and let an explicit
    # -nonvfbc in the spec opt out so one binary can be measured both ways
    if [[ " ${extra[*]:-} " != *" -nonvfbc "* ]] \
       && strings "$bin" 2>/dev/null | grep -q -- "-nvfbc_nocursor"; then
        nvargs=(-nvfbc -nvfbc_nocursor)
    fi

    "$bin" -display "$DISPLAY" -auth "$XAUTHORITY" -forever -shared -nopw -localhost \
        -rfbport "$port" "${nvargs[@]}" "${extra[@]+"${extra[@]}"}" -clip "$CLIP" \
        -threads -wait 5 -defer 10 -o "$log" >/dev/null 2>&1 &
    sleep 6
    # x11vnc forks, so $! is not necessarily the surviving pid
    local srv; srv=$(pgrep -f "rfbport $port" | head -1)
    if [ -z "$srv" ]; then echo "  $(basename "$bin"): FAILED TO START"; port=$((port+1)); return 1; fi

    "$BENCH/loadgen" -g "$GEOM" -r 60 -d $((SECS+8)) >/dev/null 2>&1 &
    local gen=$!
    sleep 2
    local c0 c1 out
    c0=$(cpu "$srv")
    out=$("$BENCH/rfbcheck.py" --port "$port" --stream "$SECS" 2>&1 | tail -1)
    c1=$(cpu "$srv")

    local mode="X11/shm"
    grep -q "NVFBC: Capture initialized" "$log" && mode="NVFBC"
    grep -qE "ShmAttach|shm_create|XShmGetImage.*fail" "$log" && mode="$mode(shm-issue)"

    LC_ALL=C printf "  %-34s %-8s cpu=%5.1f%%  %s\n" \
        "$(basename "$bin")${extra[*]+ ${extra[*]}}" "$mode" \
        "$(LC_ALL=C echo "scale=1;($c1-$c0)/($SECS*100)*100"|bc)" "$out"

    kill "$gen" 2>/dev/null
    kill -9 "$srv" 2>/dev/null
    port=$((port+1))
    sleep 3
}

echo "load $GEOM @60fps, ${SECS}s, clip $CLIP, raw encoding"
for b in "$@"; do run "$b"; done

#!/bin/bash
# ab.sh - head-to-head of two x11vnc builds under an identical load and an
# identical client, on throwaway ports.  Raw encoding is used deliberately:
# delivered bytes are then proportional to the screen area each build marks as
# modified, which is the thing worth comparing.
#
#   ./ab.sh /usr/bin/x11vnc.bak-20260813 ../x11vnc/src/x11vnc "2560x1440+0+0"
set -u
BENCH="$(cd "$(dirname "$0")" && pwd)"
A="${1:?old binary}"; B="${2:?new binary}"; GEOM="${3:-960x540+64+64}"
SECS="${SECS:-20}"
export DISPLAY="${DISPLAY:-:1}" XAUTHORITY="${XAUTHORITY:-/run/user/1000/gdm/Xauthority}"

cpu(){ awk '{r=substr($0,index($0,") ")+2);split(r,f," ");print f[12]+f[13]}' "/proc/$1/stat" 2>/dev/null; }

run(){
    local bin="$1" port="$2" log="/tmp/ab-$2.log"
    rm -f "$log"
    "$bin" -display "$DISPLAY" -auth "$XAUTHORITY" -forever -shared -nopw -localhost \
        -rfbport "$port" -nvfbc -nvfbc_nocursor -clip 2560x1440+0+0 \
        -threads -wait 5 -defer 10 -o "$log" >/dev/null 2>&1 &
    sleep 5
    # x11vnc forks, so $! is not necessarily the surviving process - find the
    # one actually bound to this port, and never use a bare `wait` (it blocks
    # on children this shell cannot reap)
    local srv
    srv=$(pgrep -f "rfbport $port" | head -1)
    if [ -z "$srv" ]; then echo "  $(basename "$bin"): failed to start"; return 1; fi

    "$BENCH/loadgen" -g "$GEOM" -r 60 -d $((SECS+6)) >/dev/null 2>&1 &
    local gen=$!
    sleep 2
    local c0 c1 out
    c0=$(cpu "$srv")
    out=$("$BENCH/rfbcheck.py" --port "$port" --stream "$SECS" 2>&1 | tail -1)
    c1=$(cpu "$srv")
    LC_ALL=C printf "  %-34s cpu=%5.1f%%  %s\n" "$(basename "$bin")" \
        "$(LC_ALL=C echo "scale=1;($c1-$c0)/($SECS*100)*100"|bc)" "$out"
    grep "NVFBC stats" "$log" | tail -1 | sed 's/^.*NVFBC/    NVFBC/'
    kill "$gen" 2>/dev/null
    kill -9 "$srv" 2>/dev/null
    sleep 3
}

echo "load $GEOM @60fps, ${SECS}s, raw encoding (bytes ~ marked area)"
run "$A" 5911
run "$B" 5912

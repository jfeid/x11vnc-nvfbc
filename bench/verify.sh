#!/bin/bash
# verify.sh - end-to-end check of a freshly built x11vnc without touching the
# live server: runs it on port 5901, draws a known colour at a known position,
# and confirms a real RFB client receives those exact pixels there.
set -u

BENCH="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(dirname "$BENCH")"
BIN="${BIN:-$ROOT/x11vnc/src/x11vnc}"
PORT="${PORT:-5901}"
LOG="${LOG:-/tmp/x11vnc-verify.log}"
export DISPLAY="${DISPLAY:-:1}"
export XAUTHORITY="${XAUTHORITY:-/run/user/1000/gdm/Xauthority}"

cleanup() {
    [ -n "${SRV:-}" ] && kill "$SRV" 2>/dev/null
    [ -n "${GEN:-}" ] && kill "$GEN" 2>/dev/null
    wait 2>/dev/null
}
trap cleanup EXIT

rm -f "$LOG"
echo "### starting $BIN on :$PORT"
"$BIN" -display "$DISPLAY" -auth "$XAUTHORITY" -forever -shared -nopw -localhost -repeat \
       -rfbport "$PORT" -nvfbc -nvfbc_nocursor -clip 2560x1440+0+0 \
       -threads -wait 5 -defer 10 -o "$LOG" >/dev/null 2>&1 &
SRV=$!
sleep 5

if ! kill -0 "$SRV" 2>/dev/null; then
    echo "FAIL: server exited during startup"; tail -20 "$LOG"; exit 1
fi
grep -E "NVFBC:" "$LOG" | sed 's/^/  /'

fail=0
for color in ff8000 22cc55 0000ff; do
    "$BENCH/loadgen" -g 256x256+300+300 -solid "$color" -d 12 >/dev/null 2>"$LOG.gen" &
    GEN=$!
    # give the server time to actually scan the new content into main_fb:
    # with no client attached it naps hard, so one round trip is not enough
    sleep 3
    out=$("$BENCH/rfbcheck.py" --port "$PORT" --rect 300,300,256,256 --expect "$color" --wait 8 2>&1)
    echo "$out" | grep -E "dominant|PASS|FAIL|ERROR" | sed "s/^/  $color: /"
    case "$out" in *PASS*) ;; *) fail=1; sed 's/^/  loadgen: /' "$LOG.gen" ;; esac
    kill "$GEN" 2>/dev/null; wait "$GEN" 2>/dev/null; GEN=
done

echo "### negative control: last colour must clear once nothing is drawn there"
# x11vnc does not poll the screen while no client is attached, so a cold read
# would legitimately return a stale framebuffer (the pre-change build behaves
# identically).  Hold a client open so scanning continues, then watch it clear.
"$BENCH/rfbcheck.py" --port "$PORT" --stream 12 >/dev/null 2>&1 &
KEEP=$!
sleep 1
neg=$("$BENCH/rfbcheck.py" --port "$PORT" --rect 300,300,64,64 --absent 0000ff --wait 8 2>&1 | tail -1)
echo "  $neg"
case "$neg" in PASS*) ;; *) fail=1 ;; esac
kill "$KEEP" 2>/dev/null; wait "$KEEP" 2>/dev/null

echo "### NVFBC stats"
grep "NVFBC stats" "$LOG" | tail -3 | sed 's/^/  /'

[ "$fail" = 0 ] && echo "### RESULT: pixel/coordinate verification PASSED" \
                || echo "### RESULT: FAILED"
exit "$fail"

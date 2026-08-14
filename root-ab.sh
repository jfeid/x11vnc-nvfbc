#!/bin/bash
# root-ab.sh - must run as root.
#
# Answers two things that cannot be tested as the session user, because the
# MIT-SHM handover in scan.c is a no-op when x11vnc and the X server share a
# uid:
#
#   1. does the fix actually clear X_ShmAttach BadAccess for a root x11vnc?
#   2. how does the now-working shm path compare against NVFBC, as root?
#
#   sudo bench/root-ab.sh
set -u
BENCH="$(cd "$(dirname "$0")" && pwd)"
BIN="${BIN:-$BENCH/../x11vnc/src/x11vnc}"
CLIP="${CLIP:-2560x1440+0+0}"
export DISPLAY="${DISPLAY:-:1}"
export XAUTHORITY="${XAUTHORITY:-/run/user/1000/gdm/Xauthority}"

if [ "$(id -u)" != 0 ]; then
    echo "root-ab.sh: must run as root (sudo $0) - the fix is a no-op otherwise"
    exit 1
fi
[ -x "$BIN" ] || { echo "root-ab.sh: no binary at $BIN"; exit 1; }

echo "binary : $BIN"
echo "running as uid $(id -u), X server on $DISPLAY owned by uid $(stat -c %u /tmp/.X11-unix/X${DISPLAY#:} 2>/dev/null)"
echo

########## 1. does the shm path survive startup as root? ##########
echo "### pre-flight: root + MIT-SHM, no NVFBC"
LOG=/tmp/root-shm-preflight.log
rm -f "$LOG"
"$BIN" -display "$DISPLAY" -auth "$XAUTHORITY" -forever -shared -nopw -localhost \
    -rfbport 5931 -nonvfbc -clip "$CLIP" -threads -wait 5 -defer 10 \
    -o "$LOG" >/dev/null 2>&1 &
sleep 7
SRV=$(pgrep -f "rfbport 5931" | head -1)

if [ -n "$SRV" ] && kill -0 "$SRV" 2>/dev/null; then
    echo "  server is ALIVE with shm enabled"
    grep -iE "MIT-SHM|handing segments" "$LOG" | sed 's/^/    /'
    if grep -q "BadAccess" "$LOG"; then
        echo "    !! BadAccess still present - fix did NOT work"
        VERDICT="FAILED"
    else
        echo "    no BadAccess in the log"
        VERDICT="WORKS"
    fi
    kill -9 "$SRV" 2>/dev/null
else
    echo "  server DIED during startup - fix did not work"
    grep -iE "shm|BadAccess|Error" "$LOG" | head -8 | sed 's/^/    /'
    VERDICT="FAILED"
fi
sleep 3
echo "  => shm-as-root: $VERDICT"
echo

if [ "$VERDICT" != "WORKS" ]; then
    echo "Skipping the A/B: without a working shm path there is nothing to compare."
    exit 2
fi

########## 2. shm vs NVFBC, both as root ##########
for g in 960x540+64+64 2560x1440+0+0; do
    echo "########## $g ##########"
    GEOM="$g" SECS="${SECS:-20}" CLIP="$CLIP" \
        "$BENCH/ab.sh" "$BIN -nonvfbc" "$BIN" 2>&1 | grep -v "Killed"
    echo
done

echo "Reminder: the numbers above are raw encoding with a synthetic client."
echo "Absolute CPU is lower than a real Tight-encoding client; the comparison"
echo "between the two rows is what carries."

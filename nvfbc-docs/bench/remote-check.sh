#!/bin/bash
# remote-check.sh - exercise the NVFBC remote-control interface against the
# running server.
#
# x11vnc's remote control goes through a single X11VNC_REMOTE property on the
# root window with no per-instance targeting, so this only gives a trustworthy
# answer when exactly ONE x11vnc is running on the display. It refuses to run
# otherwise rather than reporting a race between two servers.
#
#   ./remote-check.sh          # queries only - changes nothing
#   ./remote-check.sh --set    # also flips a setting and restores it
set -u
BENCH="$(cd "$(dirname "$0")" && pwd)"
CLIENT="${CLIENT:-$BENCH/../x11vnc/src/x11vnc}"
LOG="${LOG:-/var/log/x11vnc.log}"
export DISPLAY="${DISPLAY:-:1}"
export XAUTHORITY="${XAUTHORITY:-/run/user/1000/gdm/Xauthority}"

n=$(pgrep -cx x11vnc)
if [ "$n" -ne 1 ]; then
    echo "remote-check: $n x11vnc processes on this machine; need exactly 1."
    echo "  Remote commands are broadcast via an X property, so with more than"
    echo "  one server the answer is a race. Stop the extras and retry."
    pgrep -ax x11vnc | sed 's/^/    /'
    exit 1
fi

q(){ timeout 15 "$CLIENT" -query "$1" 2>/dev/null | tail -1; }

echo "### queries (read-only)"
q nvfbc,nvfbc_push,nvfbc_cursor,nvfbc_diffmap,nvfbc_direct | tr ',' '\n' | sed 's/^/  /'

[ "${1:-}" = "--set" ] || { echo; echo "(pass --set to also exercise a setter)"; exit 0; }

# --- mutating section: save, flip, verify, restore -------------------------
before=$(q nvfbc_push | sed 's/.*nvfbc_push://')
echo
echo "### setter test (current nvfbc_push=$before)"
[ "$before" = 1 ] && flip=nvfbc_nopush || flip=nvfbc_push
[ "$before" = 1 ] && back=nvfbc_push   || back=nvfbc_nopush

mark=$(wc -l < "$LOG" 2>/dev/null || echo 0)

echo "  -R $flip"
timeout 15 "$CLIENT" -R "$flip" >/dev/null 2>&1
sleep 2
after=$(q nvfbc_push | sed 's/.*nvfbc_push://')
echo "    nvfbc_push is now: $after  $([ "$after" != "$before" ] && echo '(changed OK)' || echo '*** UNCHANGED ***')"

echo "  did the capture session actually restart?"
tail -n +"$((mark+1))" "$LOG" 2>/dev/null | grep -E "NVFBC: capture session restarted|could not be restarted" \
    | sed 's/^/    /' || echo "    *** no restart logged ***"

echo "  -R $back  (restoring)"
timeout 15 "$CLIENT" -R "$back" >/dev/null 2>&1
sleep 2
final=$(q nvfbc_push | sed 's/.*nvfbc_push://')
echo "    nvfbc_push restored to: $final  $([ "$final" = "$before" ] && echo '(OK)' || echo '*** NOT RESTORED ***')"

echo
echo "### server still healthy?"
tail -n +"$((mark+1))" "$LOG" 2>/dev/null | grep -cE "NVFBC: capture session restarted" \
    | sed 's/^/    session restarts logged: /'
pgrep -ax x11vnc | sed 's/^/    /'

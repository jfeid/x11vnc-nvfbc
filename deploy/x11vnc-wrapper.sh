#!/bin/bash

USER_AUTH="/run/user/1000/gdm/Xauthority"
GDM_AUTH="/run/user/110/gdm/Xauthority"

while true; do
  if [ -f "$USER_AUTH" ] && DISPLAY=:1 XAUTHORITY=$USER_AUTH xdpyinfo >/dev/null 2>&1; then
	  DISPLAY=:1
	  AUTH=$USER_AUTH
  elif [ -f "$GDM_AUTH" ] && DISPLAY=:0 XAUTHORITY=$GDM_AUTH xdpyinfo >/dev/null 2>&1; then
	  DISPLAY=:0
	  AUTH=$GDM_AUTH
  else
	  sleep 2
	  continue
  fi

  export DISPLAY
  export XAUTHORITY=$AUTH

  /usr/bin/x11vnc \
	  -display $DISPLAY \
	  -auth $AUTH \
	  -forever \
	  -shared \
	  -rfbauth /etc/x11vnc.passwd \
          -localhost \
	  -rfbport 5900 \
	  -nvfbc \
	  -nvfbc_nocursor \
	  -nvfbc_push \
	  -repeat \
	  -threads \
	  -wait 5 \
	  -defer 10 \
	  -clip 2560x1440+0+0 \
	  -xkb \
	  -o /var/log/x11vnc.log

  sleep 5
done

#!/bin/sh
set -eu
umask 077

case "${NOVNC_BIND_ADDRESS:-127.0.0.1}" in
  127.0.0.1|::1) ;;
  *) echo "NOVNC_BIND_ADDRESS must remain loopback-only" >&2; exit 1 ;;
esac

# Docker Desktop can leave the X11 lock/socket behind across a fast container
# replacement. No X server exists yet in this fresh PID namespace, so these are
# stale and must be cleared before starting display :99.
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99
mkdir -p /tmp/.X11-unix

Xvfb :99 -screen 0 1440x900x24 -nolisten tcp &
XVFB_PID=$!
for _ in $(seq 1 50); do
  [ -S /tmp/.X11-unix/X99 ] && break
  kill -0 "$XVFB_PID" 2>/dev/null || { echo "Xvfb exited before becoming ready" >&2; exit 1; }
  sleep 0.1
done
[ -S /tmp/.X11-unix/X99 ] || { echo "Xvfb readiness timeout" >&2; exit 1; }

x11vnc -display :99 -forever -shared -nopw -rfbport 5900 -listen 127.0.0.1 -noxdamage &
X11VNC_PID=$!
for _ in $(seq 1 50); do
  python -c "import socket; s=socket.create_connection(('127.0.0.1',5900),.1); s.close()" 2>/dev/null && break
  kill -0 "$X11VNC_PID" 2>/dev/null || { echo "x11vnc exited before becoming ready" >&2; exit 1; }
  sleep 0.1
done

websockify --web /usr/share/novnc 7900 127.0.0.1:5900 &
WEBSOCKIFY_PID=$!

cleanup() {
  [ -z "${API_PID:-}" ] || kill "$API_PID" 2>/dev/null || true
  kill "$WEBSOCKIFY_PID" "$X11VNC_PID" "$XVFB_PID" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

uvicorn app:app --host 0.0.0.0 --port 8090 &
API_PID=$!
wait "$API_PID"

#!/bin/bash
# Keep a single Xvfb on :99. systemd used to start a second copy whenever
# is-active flickered, the new one exited "Server is already active",
# Restart=always looped (1000+ times), and every real restart killed Chrome.
set -euo pipefail
DISPLAY_NUM=99
LOCK="/tmp/.X${DISPLAY_NUM}-lock"
SOCK="/tmp/.X11-unix/X${DISPLAY_NUM}"

lock_pid() {
  if [ -f "$LOCK" ]; then
    tr -d ' ' < "$LOCK"
  fi
}

if [ -S "$SOCK" ]; then
  pid="$(lock_pid || true)"
  if [ -n "${pid:-}" ] && kill -0 "$pid" 2>/dev/null; then
    exec tail --pid="$pid" -f /dev/null
  fi
  rm -f "$LOCK" "$SOCK"
fi

exec /usr/bin/Xvfb ":${DISPLAY_NUM}" -screen 0 1600x900x24 -nolisten tcp -dpi 96

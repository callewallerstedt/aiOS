#!/bin/bash
# Keep a single, systemd-owned Xvfb on :99. Older installs could leave Xvfb
# orphaned and make this unit watch it with `tail --pid`; if that orphan died,
# systemd considered the watcher successful and did not restore the display.
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
    command="$(ps -p "$pid" -o args= 2>/dev/null || true)"
    case "$command" in
      /usr/bin/Xvfb\ :${DISPLAY_NUM}*)
        echo "replacing orphaned Xvfb pid $pid so systemd owns the display" >&2
        kill "$pid"
        for _ in $(seq 1 20); do
          kill -0 "$pid" 2>/dev/null || break
          sleep 0.1
        done
        if kill -0 "$pid" 2>/dev/null; then
          kill -KILL "$pid"
        fi
        ;;
      *)
        echo "display :${DISPLAY_NUM} belongs to unexpected pid $pid: $command" >&2
        exit 1
        ;;
    esac
  fi
  rm -f "$LOCK" "$SOCK"
fi

exec /usr/bin/Xvfb ":${DISPLAY_NUM}" -screen 0 1600x900x24 -nolisten tcp -dpi 96

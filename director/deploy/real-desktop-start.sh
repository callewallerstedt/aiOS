#!/usr/bin/env bash
set -euo pipefail

export DISPLAY="${AIOS_OPERATOR_DISPLAY:-:0}"
export XAUTHORITY="${AIOS_OPERATOR_XAUTHORITY:-/run/user/1000/gdm/Xauthority}"

for _ in $(seq 1 60); do
  if xdpyinfo >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
xdpyinfo >/dev/null

output="${AIOS_OPERATOR_OUTPUT:-}"
if [ -z "$output" ]; then
  output="$(xrandr --query | awk '/ connected primary/{print $1; exit} / connected/{fallback=$1} END{if (!NR) exit; if (!output && fallback) print fallback}')"
fi
if [ -z "$output" ]; then
  echo "no connected desktop output" >&2
  exit 1
fi
if ! xrandr --query | grep -A30 "^${output} connected" | grep -qE '^ *1280x720 '; then
  echo "$output does not expose a 1280x720 mode" >&2
  exit 1
fi

xrandr --output "$output" --mode 1280x720 --scale 1x1 --primary
xset s off
xset -dpms

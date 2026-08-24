#!/usr/bin/env bash
set -euo pipefail

export DISPLAY="${AIOS_OPERATOR_DISPLAY:-:0}"
export XAUTHORITY="${AIOS_OPERATOR_XAUTHORITY:-/run/user/1000/gdm/Xauthority}"
export DBUS_SESSION_BUS_ADDRESS="${DBUS_SESSION_BUS_ADDRESS:-unix:path=/run/user/$(id -u)/bus}"

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

# GNOME/Mutter blanks and locks the seat on idle, which the operator then
# screenshots as a black "Mutter guard" screen. xset above does not stop
# gnome-settings-daemon, so turn off idle blanking, the lock screen and
# auto-suspend at the GNOME level too. Each setting is best effort: older
# GNOME releases do not carry every schema key.
if command -v gsettings >/dev/null 2>&1; then
  gsettings set org.gnome.desktop.session idle-delay 0 || true
  gsettings set org.gnome.desktop.screensaver lock-enabled false || true
  gsettings set org.gnome.desktop.screensaver idle-activation-enabled false || true
  gsettings set org.gnome.settings-daemon.plugins.power sleep-inactive-ac-type 'nothing' || true
fi

# A screen shield that was already active survives changing the preferences.
# Dismiss it on this user's real session so Operator starts from the desktop.
if command -v gdbus >/dev/null 2>&1; then
  gdbus call --session --dest org.gnome.ScreenSaver \
    --object-path /org/gnome/ScreenSaver \
    --method org.gnome.ScreenSaver.SetActive false || true
fi

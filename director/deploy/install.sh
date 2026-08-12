#!/usr/bin/env bash
# Install or update aiOS Director on the Linux box. Idempotent: safe to re-run
# for every deploy.
#
#   ~/aios-director/            the package (rsynced from the repo)
#   ~/aios-director/.venv       its own virtualenv
#   ~/.local/share/aios-director  database, settings, logs, Chrome profile
#
# Everything runs as the `calle` user under systemd --user with lingering on,
# so it comes back after a reboot without anybody logging in.
set -euo pipefail

APP_DIR="$HOME/aios-director"
DATA_DIR="$HOME/.local/share/aios-director"
UNIT_DIR="$HOME/.config/systemd/user"
PORT="${AIOS_DIRECTOR_PORT:-8770}"
FUNNEL_PATH="${AIOS_DIRECTOR_FUNNEL_PATH:-/director}"

say() { printf '\n\033[1m== %s\033[0m\n' "$1"; }

say "system packages"
NEEDED=()
for pkg in xvfb x11vnc scrot xdotool wmctrl openbox novnc python3-venv fonts-liberation; do
  dpkg -s "$pkg" >/dev/null 2>&1 || NEEDED+=("$pkg")
done
if [ ${#NEEDED[@]} -gt 0 ]; then
  echo "installing: ${NEEDED[*]}"
  sudo apt-get update -qq
  sudo DEBIAN_FRONTEND=noninteractive apt-get install -y -qq "${NEEDED[@]}"
else
  echo "all present"
fi

say "directories"
mkdir -p "$APP_DIR" "$DATA_DIR" "$UNIT_DIR"
chmod 700 "$DATA_DIR"

say "virtualenv"
if [ ! -x "$APP_DIR/.venv/bin/python" ]; then
  python3 -m venv "$APP_DIR/.venv"
fi
"$APP_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$APP_DIR/.venv/bin/pip" install --quiet -r "$APP_DIR/director/requirements.txt"
echo "python: $("$APP_DIR/.venv/bin/python" --version)"

say "systemd units"
for unit in aios-director aios-director-xvfb aios-director-wm aios-director-x11vnc aios-director-chrome; do
  install -m 644 "$APP_DIR/director/deploy/$unit.service" "$UNIT_DIR/"
done
systemctl --user daemon-reload
chmod +x "$APP_DIR/director/deploy/xvfb-start.sh"

# Recycle only when Xvfb is storming or still on the old ExecStart. A healthy
# display is left alone — restarting it kills Chrome and dumps the session.
xvfb_pid="$(systemctl --user show aios-director-xvfb.service -p MainPID --value 2>/dev/null || true)"
xvfb_restarts="$(systemctl --user show aios-director-xvfb.service -p NRestarts --value 2>/dev/null || true)"
xvfb_cmd=""
if [ -n "${xvfb_pid:-}" ] && [ "${xvfb_pid}" != "0" ]; then
  xvfb_cmd="$(ps -p "$xvfb_pid" -o args= 2>/dev/null || true)"
fi
recycle_display=0
if [ "${xvfb_restarts:-0}" -gt 5 ]; then
  recycle_display=1
fi
# Migrate the old watcher arrangement once. A healthy current unit has Xvfb
# itself as MainPID; the `tail --pid` watcher is precisely the orphaned model.
if echo "$xvfb_cmd" | grep -q 'tail --pid'; then
  recycle_display=1
fi
if [ "$recycle_display" = 1 ]; then
  say "recycling operator display (Xvfb was looping or still on the old unit)"
  systemctl --user stop aios-director-chrome.service aios-director-wm.service aios-director-x11vnc.service aios-director-xvfb.service || true
  sleep 1
  if ! DISPLAY=:99 xdpyinfo >/dev/null 2>&1; then
    rm -f /tmp/.X99-lock /tmp/.X11-unix/X99
    pkill -f 'Xvfb :99' || true
  fi
fi

# Start, do not restart: bouncing a healthy Xvfb kills Chrome.
systemctl --user reset-failed aios-director-xvfb.service aios-director-wm.service aios-director-x11vnc.service aios-director-chrome.service || true
systemctl --user enable aios-director-xvfb.service
systemctl --user enable aios-director-wm.service
systemctl --user enable aios-director-x11vnc.service
systemctl --user enable aios-director-chrome.service
systemctl --user start aios-director-xvfb.service
systemctl --user start aios-director-wm.service
systemctl --user start aios-director-x11vnc.service
systemctl --user start aios-director-chrome.service
systemctl --user enable aios-director.service
systemctl --user restart aios-director.service

say "waiting for the server"
for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:$PORT/api/health" >/dev/null 2>&1; then
    echo "healthy on 127.0.0.1:$PORT"
    break
  fi
  sleep 0.5
done

say "tailscale funnel"
# The lillebo dashboard already owns "/" on this hostname, so Director mounts
# on a path beside it rather than taking the root.
if tailscale funnel status 2>/dev/null | grep -q "$FUNNEL_PATH"; then
  echo "already published at $FUNNEL_PATH"
else
  tailscale funnel --bg --yes --set-path="$FUNNEL_PATH" "http://127.0.0.1:$PORT" || {
    echo "could not set the funnel automatically; run this yourself:"
    echo "  tailscale funnel --bg --yes --set-path=$FUNNEL_PATH http://127.0.0.1:$PORT"
  }
fi

say "state"
systemctl --user --no-pager status aios-director.service | head -6 || true
tailscale funnel status 2>/dev/null | head -12 || true

cat <<EOF

Director is installed.

  logs      journalctl --user -u aios-director -f
  restart   systemctl --user restart aios-director
  pair      cd ~/aios-director && .venv/bin/python -m director.cli pair
  status    cd ~/aios-director && .venv/bin/python -m director.cli status

EOF

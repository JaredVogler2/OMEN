#!/usr/bin/env bash
# Install or update OMEN as a systemd service (Raspberry Pi OS / any systemd Linux).
#
#   scripts/install-pi.sh                     # install/update, enable and start
#   scripts/install-pi.sh --with-backup-timer # also a nightly ledger backup
#   scripts/install-pi.sh --with-atlas-timer  # also a weekly calibration-atlas rebuild (feeds calibration edges)
#   scripts/install-pi.sh --with-exec         # also install the signing extra (only for demo/live execution)
#   scripts/install-pi.sh --no-start          # render and enable, but do not (re)start
#
# Re-running it is safe: it re-syncs dependencies from uv.lock, re-renders the unit and restarts the service.
set -euo pipefail

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
RUN_USER="${SUDO_USER:-$(id -un)}"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
STATE_DIR="${OMEN_STATE_DIR:-$RUN_HOME/.omen}"
ENV_FILE="${OMEN_ENV_FILE:-$RUN_HOME/.config/omen/omen.env}"
BACKUP=0
ATLAS=0
EXEC=0
START=1

for arg in "$@"; do
  case "$arg" in
    --with-backup-timer) BACKUP=1 ;;
    --with-atlas-timer) ATLAS=1 ;;
    --with-exec) EXEC=1 ;;
    --no-start) START=0 ;;
    -h|--help) sed -n '2,10p' "$0"; exit 0 ;;
    *) echo "unknown option: $arg" >&2; exit 2 ;;
  esac
done

UV="$(command -v uv || true)"
[ -z "$UV" ] && [ -x "$RUN_HOME/.local/bin/uv" ] && UV="$RUN_HOME/.local/bin/uv"
if [ -z "$UV" ]; then
  echo "uv is not installed. Install it first (it is the same tool STONKS/APEX uses):" >&2
  echo "  curl -LsSf https://astral.sh/uv/install.sh | sh" >&2
  exit 1
fi

echo "== dependencies (from uv.lock)"
cd "$APP_DIR"
if [ "$EXEC" = 1 ]; then
  "$UV" sync --frozen --no-dev --extra exec
else
  "$UV" sync --frozen --no-dev
fi

echo "== state and environment"
install -d -m 700 "$STATE_DIR" "$(dirname "$ENV_FILE")"
if [ ! -f "$ENV_FILE" ]; then
  install -m 600 .env.example "$ENV_FILE"
  echo "Created $ENV_FILE from .env.example: add your Discord webhooks (or bot token + channel map), then re-run."
fi
chmod 600 "$ENV_FILE"

render() {
  sed -e "s|@USER@|$RUN_USER|g" -e "s|@APP_DIR@|$APP_DIR|g" -e "s|@ENV_FILE@|$ENV_FILE|g" \
      -e "s|@STATE_DIR@|$STATE_DIR|g" -e "s|@UV@|$UV|g" "$1"
}

echo "== systemd units"
render deploy/systemd/omen.service.in | sudo tee /etc/systemd/system/omen.service >/dev/null
if [ "$BACKUP" = 1 ]; then
  render deploy/systemd/omen-backup.service.in | sudo tee /etc/systemd/system/omen-backup.service >/dev/null
  sudo install -m 644 deploy/systemd/omen-backup.timer /etc/systemd/system/omen-backup.timer
fi
if [ "$ATLAS" = 1 ]; then
  render deploy/systemd/omen-atlas.service.in | sudo tee /etc/systemd/system/omen-atlas.service >/dev/null
  sudo install -m 644 deploy/systemd/omen-atlas.timer /etc/systemd/system/omen-atlas.timer
fi
sudo systemctl daemon-reload
sudo systemctl enable omen.service >/dev/null
[ "$BACKUP" = 1 ] && sudo systemctl enable --now omen-backup.timer >/dev/null
[ "$ATLAS" = 1 ] && sudo systemctl enable --now omen-atlas.timer >/dev/null

echo "== doctor"
set +e
sudo -u "$RUN_USER" env OMEN_STATE_DIR="$STATE_DIR" OMEN_PROJECT_ROOT="$APP_DIR" \
  "$UV" run --frozen --no-sync omen --env-file "$ENV_FILE" doctor
DOCTOR=$?
set -e

if [ "$START" = 1 ]; then
  sudo systemctl restart omen.service
  echo "OMEN is running. Logs: journalctl -u omen -f"
else
  echo "Enabled but not started (--no-start). Start with: sudo systemctl start omen"
fi
[ "$DOCTOR" = 0 ] || echo "doctor reported problems above; fix $ENV_FILE and re-run this script." >&2

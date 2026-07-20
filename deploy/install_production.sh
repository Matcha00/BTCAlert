#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/btc-vol-alert}"
RUN_USER="${RUN_USER:-btcalert}"
RUN_GROUP="${RUN_GROUP:-btcalert}"
STATE_DIR="${STATE_DIR:-/var/lib/btc-vol-alert}"
ENV_FILE="${ENV_FILE:-/etc/btc-vol-alert.env}"
PYTHON_BIN="${PYTHON_BIN:-python3.11}"

if [[ "$(id -u)" != "0" ]]; then
  echo "Error: run this script as root on the server." >&2
  exit 1
fi

if ! id "$RUN_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$STATE_DIR" --create-home --shell /sbin/nologin "$RUN_USER"
fi

install -d -m 0755 -o root -g root "$APP_DIR"
install -d -m 0750 -o "$RUN_USER" -g "$RUN_GROUP" "$STATE_DIR"

if [[ ! -f "$ENV_FILE" ]]; then
  if [[ -f /root/btc-vol-alert/.env ]]; then
    install -m 0640 -o root -g "$RUN_GROUP" /root/btc-vol-alert/.env "$ENV_FILE"
  else
    install -m 0640 -o root -g "$RUN_GROUP" /dev/null "$ENV_FILE"
  fi
fi
chown root:"$RUN_GROUP" "$ENV_FILE"
chmod 0640 "$ENV_FILE"

if [[ -f /root/btc-vol-alert/state.json && ! -f "$STATE_DIR/state.json" ]]; then
  install -m 0640 -o "$RUN_USER" -g "$RUN_GROUP" /root/btc-vol-alert/state.json "$STATE_DIR/state.json"
fi
if [[ ! -f "$STATE_DIR/state.json" ]]; then
  printf '{\n  "last_alert_date": null,\n  "last_alert_keys": {},\n  "monitors": {},\n  "system": {}\n}\n' > "$STATE_DIR/state.json"
  chown "$RUN_USER:$RUN_GROUP" "$STATE_DIR/state.json"
  chmod 0640 "$STATE_DIR/state.json"
fi

if ! grep -q '^STATE_FILE=' "$ENV_FILE"; then
  printf '\nSTATE_FILE=%s/state.json\n' "$STATE_DIR" >> "$ENV_FILE"
else
  sed -i "s#^STATE_FILE=.*#STATE_FILE=$STATE_DIR/state.json#" "$ENV_FILE"
fi
if ! grep -q '^LOG_FILE=' "$ENV_FILE"; then
  printf 'LOG_FILE=\n' >> "$ENV_FILE"
else
  sed -i 's#^LOG_FILE=.*#LOG_FILE=#' "$ENV_FILE"
fi
if ! grep -q '^FAILURE_ALERT_THRESHOLD=' "$ENV_FILE"; then
  printf 'FAILURE_ALERT_THRESHOLD=3\n' >> "$ENV_FILE"
fi

"$PYTHON_BIN" -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/python" -m pip install --upgrade pip
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"

install -m 0644 "$APP_DIR/deploy/systemd/btc-vol-alert.service" /etc/systemd/system/btc-vol-alert.service
install -m 0644 "$APP_DIR/deploy/systemd/btc-vol-alert.timer" /etc/systemd/system/btc-vol-alert.timer
install -m 0644 "$APP_DIR/deploy/systemd/btc-vol-dashboard.service" /etc/systemd/system/btc-vol-dashboard.service
install -m 0644 "$APP_DIR/deploy/nginx/btc.matcha00.xyz.conf" /etc/nginx/conf.d/btc.matcha00.xyz.conf

systemctl daemon-reload
systemctl enable btc-vol-alert.timer btc-vol-dashboard.service
nginx -t

echo "Production install completed."
echo "Start dashboard: systemctl restart btc-vol-dashboard.service"
echo "Start timer: systemctl start btc-vol-alert.timer"

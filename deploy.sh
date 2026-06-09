#!/usr/bin/env bash
set -euo pipefail

LOCAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REMOTE_USER="${REMOTE_USER:-root}"
REMOTE_HOST="${REMOTE_HOST:-}"
REMOTE_DIR="${REMOTE_DIR:-/root/btc-vol-alert}"
SSH_PORT="${SSH_PORT:-22}"
REMOTE_PYTHON_BIN="${REMOTE_PYTHON_BIN:-python3}"

if [[ -z "$REMOTE_HOST" ]]; then
  echo "Usage: REMOTE_HOST=your.server.ip ./deploy.sh"
  echo "Optional: REMOTE_USER=root REMOTE_DIR=/root/btc-vol-alert SSH_PORT=22"
  exit 1
fi

REMOTE="${REMOTE_USER}@${REMOTE_HOST}"
SSH_CMD=(
  ssh
  -p "$SSH_PORT"
  -o ConnectTimeout=15
  -o ServerAliveInterval=10
  -o ServerAliveCountMax=3
)
RSYNC_RSH="ssh -p $SSH_PORT -o ConnectTimeout=15 -o ServerAliveInterval=10 -o ServerAliveCountMax=3"
FILES_FROM="$(mktemp)"
trap 'rm -f "$FILES_FROM"' EXIT

git -C "$LOCAL_DIR" ls-files | grep -v '^state\.json$' > "$FILES_FROM"

"${SSH_CMD[@]}" "$REMOTE" "mkdir -p '$REMOTE_DIR'"

rsync -avz --delete \
  -e "$RSYNC_RSH" \
  --files-from="$FILES_FROM" \
  "$LOCAL_DIR/" "$REMOTE:$REMOTE_DIR/"

"${SSH_CMD[@]}" "$REMOTE" "cd '$REMOTE_DIR' && \
  test -f state.json || printf '{\n  \"last_alert_date\": null\n}\n' > state.json && \
  '$REMOTE_PYTHON_BIN' -m venv .venv && \
  ./.venv/bin/python -m pip install --upgrade pip && \
  ./.venv/bin/pip install -r requirements.txt && \
  mkdir -p logs && \
  touch logs/btc_vol_alert.log logs/cron.log && \
  ./.venv/bin/python main.py --dry-run"

echo
echo "Deploy completed: $REMOTE:$REMOTE_DIR"
echo "Remember to create $REMOTE_DIR/.env on the server before running without --dry-run."

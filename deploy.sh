#!/usr/bin/env bash
set -euo pipefail

LOCAL_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REMOTE_USER="${REMOTE_USER:-root}"
REMOTE_HOST="${REMOTE_HOST:-}"
REMOTE_DIR="${REMOTE_DIR:-/opt/btc-vol-alert}"
SSH_PORT="${SSH_PORT:-22}"
REMOTE_PYTHON_BIN="${REMOTE_PYTHON_BIN:-python3.11}"

if [[ -z "$REMOTE_HOST" ]]; then
  echo "Usage: REMOTE_HOST=your.server.ip ./deploy.sh"
  echo "Optional: REMOTE_USER=root REMOTE_DIR=/opt/btc-vol-alert SSH_PORT=22"
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
REMOTE_DRY_RUN_CMD="set -a; source /etc/btc-vol-alert.env; set +a; cd '$REMOTE_DIR'; ./.venv/bin/python main.py --dry-run"
REMOTE_DRY_RUN_QUOTED="$(printf '%q' "$REMOTE_DRY_RUN_CMD")"

"${SSH_CMD[@]}" "$REMOTE" "mkdir -p '$REMOTE_DIR'"

rsync -avz --delete \
  -e "$RSYNC_RSH" \
  --exclude '.venv' \
  --exclude '.git' \
  --exclude 'logs' \
  --exclude '__pycache__' \
  --exclude '.env' \
  --files-from="$FILES_FROM" \
  "$LOCAL_DIR/" "$REMOTE:$REMOTE_DIR/"

"${SSH_CMD[@]}" "$REMOTE" "cd '$REMOTE_DIR' && \
  APP_DIR='$REMOTE_DIR' PYTHON_BIN='$REMOTE_PYTHON_BIN' bash deploy/install_production.sh && \
  runuser -u btcalert -- bash -lc $REMOTE_DRY_RUN_QUOTED"

echo
echo "Deploy completed: $REMOTE:$REMOTE_DIR"
echo "Production env file: /etc/btc-vol-alert.env"

#!/usr/bin/env bash
set -euo pipefail

PROJECT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$PROJECT_DIR"

PYTHON_BIN="${PYTHON_BIN:-python3}"

if ! command -v "$PYTHON_BIN" >/dev/null 2>&1; then
  echo "Error: $PYTHON_BIN not found. Please install Python 3.11+."
  exit 1
fi

"$PYTHON_BIN" - <<'PY'
import sys

required = (3, 11)
if sys.version_info < required:
    raise SystemExit(
        f"Python 3.11+ is required, current version is {sys.version.split()[0]}"
    )
print(f"Python version OK: {sys.version.split()[0]}")
PY

"$PYTHON_BIN" -m venv .venv
./.venv/bin/python -m pip install --upgrade pip
./.venv/bin/pip install -r requirements.txt

mkdir -p logs
touch logs/btc_vol_alert.log logs/cron.log

echo
echo "Setup completed."
echo
echo "Next steps:"
echo "1. cp .env.example .env"
echo "2. Edit .env and set TELEGRAM_BOT_TOKEN / TELEGRAM_CHAT_ID"
echo "3. Run a test: ./.venv/bin/python main.py --dry-run"
echo "4. Run normally: ./.venv/bin/python main.py"

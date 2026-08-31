#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"

cd "$SCRIPT_DIR"

# Create virtualenv if missing
if [[ ! -d "$VENV_DIR" ]]; then
    echo "Creating virtualenv..."
    python3 -m venv "$VENV_DIR"
fi

# Install / sync dependencies
"$VENV_DIR/bin/pip" install -q -r requirements.txt

# Load .env
if [[ -f "$SCRIPT_DIR/.env" ]]; then
    set -o allexport
    # shellcheck disable=SC1091
    source <(grep -v '^\s*#' "$SCRIPT_DIR/.env" | grep -v '^\s*$' | sed 's/[[:space:]]*#.*//')
    set +o allexport
fi

HOST="${HOST:-0.0.0.0}"
PORT="${PORT:-5001}"
DEBUG="${DEBUG:-false}"

echo "Starting PengyShare on $HOST:$PORT"

exec "$VENV_DIR/bin/python" app.py

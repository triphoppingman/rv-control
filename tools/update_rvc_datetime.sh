#!/usr/bin/env bash

set -eu

SCRIPT_DIR="$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_DIR="$(CDPATH= cd -- "$SCRIPT_DIR/.." && pwd)"
PYTHON="${PYTHON:-}"

if [[ -z "$PYTHON" ]]; then
    if [[ -x "$PROJECT_DIR/.venv/bin/python" ]]; then
        PYTHON="$PROJECT_DIR/.venv/bin/python"
    else
        PYTHON="python3"
    fi
fi

PYTHONPATH="$PROJECT_DIR/src${PYTHONPATH:+:$PYTHONPATH}" \
    exec "$PYTHON" "$SCRIPT_DIR/rvc_datetime.py" -s

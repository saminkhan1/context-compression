#!/bin/sh
set -eu

DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
PY="$DIR/.venv/bin/python"
[ -x "$PY" ] || PY=python3
exec "$PY" "$DIR/hook.py"

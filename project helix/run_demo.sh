#!/bin/sh
# Helix demo replay: saved investigations load instantly. Run from the repository root.
# Use intake  Los Angeles, CA · $900,000 · Cash  (1250 S Broadway)
#        or   Houston, TX · $900,000 · Cash      (15603 W Hardy Rd)
# Any other intake runs the normal live pipeline.
set -a; [ -f .env ] && . ./.env; set +a
export HELIX_DEMO_REPLAY=1
PY="${PYTHON:-.venv/bin/python}"; [ -x "$PY" ] || PY=python3
exec "$PY" "project helix/app.py" --port "${PORT:-8127}" --no-browser

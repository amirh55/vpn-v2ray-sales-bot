#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")/.."
source .venv/bin/activate
exec gunicorn vpnshop.wsgi:application \
  --bind "${BIND_HOST:-0.0.0.0}:${PORT:-8000}" \
  --workers "${WEB_WORKERS:-2}" \
  --timeout "${WEB_TIMEOUT:-120}"

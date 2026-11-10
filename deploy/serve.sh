#!/usr/bin/env bash
# Gunicorn launcher.
#
# The bind address comes from PANEL_BIND in /etc/nft-forward-panel.env so the
# same unit serves both access modes:
#   https  -> 127.0.0.1:8108   (Caddy terminates TLS in front of it)
#   http   -> 0.0.0.0:<port>   (panel reached directly on a public port)
set -Eeuo pipefail

ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
exec "$ROOT/.venv/bin/gunicorn" \
    --workers "${PANEL_WORKERS:-1}" \
    --threads "${PANEL_THREADS:-8}" \
    --bind "${PANEL_BIND:-127.0.0.1:8108}" \
    --access-logfile - \
    --error-logfile - \
    wsgi:app

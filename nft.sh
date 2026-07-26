#!/usr/bin/env bash
# Mochi Forward SSH fallback wrapper.
# This intentionally delegates to nfpctl.py so SSH and WebUI share one database
# and one nftables renderer.
set -euo pipefail

SCRIPT_PATH=$(readlink -f -- "$0")
SCRIPT_DIR=$(CDPATH= cd -- "$(dirname -- "$SCRIPT_PATH")" && pwd)
PYTHON_BIN="${NFPCTL_PYTHON:-${SCRIPT_DIR}/.venv/bin/python3}"
if [[ ! -x "$PYTHON_BIN" ]]; then
    PYTHON_BIN="${NFPCTL_PYTHON:-python3}"
fi

exec "$PYTHON_BIN" "${SCRIPT_DIR}/nfpctl.py" "$@"

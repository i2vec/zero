#!/usr/bin/env bash
set -euo pipefail

install_url="${ZERO_TRISOL_INSTALL_URL:-https://trisol.dp.tech/install.sh}"

if ! command -v trisol >/dev/null 2>&1; then
  curl -fsSL "$install_url" | bash
fi

trisol version

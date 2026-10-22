#!/bin/bash
# Installs On the Record into the folder above this script. Run:  bash install/linux-install.sh   See INSTALL.md.
set -e
cd "$(dirname "$0")/.."
echo "Installing On the Record in $(pwd)"
if ! command -v uv >/dev/null 2>&1; then
  echo "Installing uv, the Python installer (https://docs.astral.sh/uv/)..."
  curl -LsSf https://astral.sh/uv/install.sh | sh
  export PATH="$HOME/.local/bin:$PATH"
fi
echo "Installing Python and the packages (a few GB the first time)..."
uv sync --extra all --frozen
.venv/bin/ontherecord setup --install

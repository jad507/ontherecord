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
echo "Looking at the graphics card, to pick the right PyTorch build..."
FLAVOR=$(uv run --no-project --python 3.12 src/ontherecord/hwdetect.py --torch-flavor) || FLAVOR=cu128
echo "Installing Python and the packages (a few GB the first time)..."
uv sync --extra all --frozen --no-default-groups --group "$FLAVOR"
.venv/bin/ontherecord setup --install

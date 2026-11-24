#!/usr/bin/env bash
set -euo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO_ROOT"

echo "==> [1/2] Installing Python virtual environment and dependencies..."
if command -v uv >/dev/null 2>&1; then
  uv sync --python 3.12
else
  python3 -m venv .venv
  ./.venv/bin/pip install --upgrade pip
  ./.venv/bin/pip install -e .
fi

echo "==> [2/2] Running non-interactive setup & health check..."
./.venv/bin/python -X utf8 -m qq_agy_bridge.setup_wizard --non-interactive --with-tunnel --doctor "$@"
echo "==> Done! Start the server anytime with: ./.venv/bin/python -m qq_agy_bridge.main"

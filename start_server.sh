#!/usr/bin/env bash
set -euo pipefail

# 始终以脚本所在仓库为工作目录，不依赖某位开发者机器上的绝对路径。
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$SCRIPT_DIR"
exec .venv/bin/python -m uvicorn backend.app:app --host "${HOST:-127.0.0.1}" --port "${PORT:-8000}"

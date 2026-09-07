#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

uv export --no-dev --locked --no-emit-project --no-hashes --no-header -o requirements.txt
echo "✓ Successfully exported pinned runtime requirements to requirements.txt from uv.lock"

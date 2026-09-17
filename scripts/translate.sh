#!/usr/bin/env bash
set -euo pipefail
translation_repo_dir="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$translation_repo_dir"
exec uv run python -m lncrawl.translation "$@"

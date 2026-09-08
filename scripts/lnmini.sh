#!/usr/bin/env bash
# lnmini — start the lncrawl-mini dev stack.
#
# Usage:
#   lnmini run dev        # FastAPI backend (:8000) + Next.js frontend (:3000)
#   lnmini run backend    # backend only
#   lnmini run frontend   # frontend only
#
# Ctrl+C stops everything it started. Set LNCRAWL_MINI_ROOT when invoking
# from outside the repository.

set -u

# ----- locate the repository ------------------------------------------------
_root_from_script="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." 2>/dev/null && pwd)"
DEFAULT_ROOT=""  # installed copies embed the repo path here (see scripts/lnmini.sh)
ROOT=""

if [[ -n "${LNCRAWL_MINI_ROOT:-}" && -f "$LNCRAWL_MINI_ROOT/pyproject.toml" ]]; then
  ROOT="$LNCRAWL_MINI_ROOT"
elif [[ -f "$_root_from_script/pyproject.toml" ]] &&
  grep -q '^name = "lncrawl-mini"' "$_root_from_script/pyproject.toml" 2>/dev/null; then
  ROOT="$_root_from_script"
elif [[ -f "pyproject.toml" ]] && grep -q '^name = "lncrawl-mini"' pyproject.toml 2>/dev/null; then
  ROOT="$PWD"
elif [[ -n "$DEFAULT_ROOT" && -f "$DEFAULT_ROOT/pyproject.toml" ]]; then
  ROOT="$DEFAULT_ROOT"
fi

if [[ -z "$ROOT" ]]; then
  echo "lnmini: cannot locate the lncrawl-mini repository." >&2
  echo "Run from inside the repo, or set LNCRAWL_MINI_ROOT=/path/to/lncrawl-mini." >&2
  exit 1
fi

# Keep `sources.*` imports resolvable regardless of how the venv was synced.
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"

usage() {
  cat >&2 <<EOF
Usage: lnmini run <target>

Targets:
  dev        backend (uvicorn :8000) + frontend (next :3000)
  backend    backend only
  frontend   frontend only
EOF
  exit 2
}

prefix() {
  # Prefix every line of stdin with the given label.
  local label="$1" line
  while IFS= read -r line; do
    printf '[%s] %s\n' "$label" "$line"
  done
}

pids=()
cleanup() {
  trap - INT TERM EXIT
  local pid
  for pid in "${pids[@]:-}"; do
    pkill -P "$pid" 2>/dev/null
    kill "$pid" 2>/dev/null
  done
  wait 2>/dev/null
  exit 0
}

run_dev() {
  trap cleanup INT TERM
  cd "$ROOT"
  uv run uvicorn lncrawl.server:app --reload --host 127.0.0.1 --port 8000 \
    > >(prefix backend) 2>&1 &
  pids+=("$!")
  ( cd "$ROOT/frontend" && bun run dev ) > >(prefix frontend) 2>&1 &
  pids+=("$!")
  echo "Backend : http://127.0.0.1:8000  (API docs at /docs)"
  echo "Frontend: http://localhost:3000"
  echo "Press Ctrl+C to stop both."
  wait -n
  cleanup
}

run_backend() {
  cd "$ROOT"
  exec uv run uvicorn lncrawl.server:app --reload --host 127.0.0.1 --port 8000
}

run_frontend() {
  cd "$ROOT/frontend"
  exec bun run dev
}

case "${1:-}" in
  run)
    case "${2:-}" in
      dev) run_dev ;;
      backend) run_backend ;;
      frontend) run_frontend ;;
      *) usage ;;
    esac
    ;;
  *)
    usage
    ;;
esac

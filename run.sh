#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"
if [ -f "$HOME/myenv/bin/activate" ]; then
  # shellcheck disable=SC1091
  source "$HOME/myenv/bin/activate"
fi
export PYTHONPATH="$(pwd)${PYTHONPATH:+:$PYTHONPATH}"
exec python3 -m reddit_joiner "$@"

#!/usr/bin/env bash
set -euo pipefail
MODE="${1:-full}"
[[ "$MODE" = full || "$MODE" = quick ]] || { echo "usage: $0 [full|quick]" >&2; exit 2; }
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"; cd "$REPO"
bash eval/scripts/run_eval.sh "$MODE"
bash eval/scripts/run_project_eval.sh matrixmul
bash eval/scripts/run_project_eval.sh libcrc

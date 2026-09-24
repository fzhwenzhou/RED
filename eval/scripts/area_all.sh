#!/usr/bin/env bash
set -euo pipefail
REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"; cd "$REPO"
bash eval/scripts/area.sh eval/build/micro-ecc/area eval/rtl/red_accel.v
bash eval/scripts/area.sh eval/build/libcrc/area eval/projects/libcrc/red_accel.v
printf '\nmatrixmul: area unavailable (behavioral shortreal/DPI model is not synthesizable)\n'

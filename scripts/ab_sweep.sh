#!/usr/bin/env bash
# A/B sweep: every target project, on every backend, resumable.
#
#   bash scripts/ab_sweep.sh              # run (or resume) the whole matrix
#   bash scripts/ab_sweep.sh --status     # what is done and what is not
#
# Six runs at roughly half an hour each, so it gets interrupted. Every run is
# skipped when its summary.md already exists (RED_SKIP_DONE=1), which is the
# file the loop writes last -- so re-running this script continues the sweep
# instead of starting it again, and a run killed halfway is redone rather than
# counted. Backends are run one at a time on purpose: Gate 0/1 profiling and
# Gate 2's Spike runs are CPU-bound, and overlapping two batches would make
# the wall-clock and agent-time numbers meaningless for comparison.
set -u
cd "$(dirname "$0")/.."

PROJECTS=(micro-ecc libcrc matrixmul)
# label : routing file
BACKENDS=("claude:models.toml" "gpt6:models.gpt6.toml")
DB="${RED_DB_ROOT:-output/db}"
# Same test run_projects.sh uses to decide a run must be redone.
BACKEND_FAILED_RE="LLM backend failing on every turn|the agent.s turns did not complete"

if [ "${1:-}" = "--status" ]; then
    printf '%-12s' "project"
    for b in "${BACKENDS[@]}"; do printf '%-10s' "${b%%:*}"; done; echo
    echo "  (done = produced an ISASpec; - = no usable run yet)"
    for p in "${PROJECTS[@]}"; do
        printf '%-12s' "$p"
        for b in "${BACKENDS[@]}"; do
            lbl="${b%%:*}"
            found=""
            for d in "$DB/$p/${lbl}-"*/; do
                [ -f "$d/ISASpec.yaml" ] && found="$d"
            done
            if [ -n "$found" ]; then printf '%-10s' "done"
            else printf '%-10s' "-"; fi
        done; echo
    done
    exit 0
fi

for entry in "${BACKENDS[@]}"; do
    label="${entry%%:*}"; cfg="${entry#*:}"
    if [ ! -f "$cfg" ]; then
        echo "=== SKIP backend $label: no routing file $cfg"; continue
    fi
    echo "########## BACKEND $label  ($cfg)  $(date -Is)"
    RED_RUN_PREFIX="$label" \
    RED_MODELS_CONFIG="$cfg" \
    RED_SKIP_DONE=1 \
        bash scripts/run_projects.sh "${PROJECTS[@]}"
done
echo "########## SWEEP COMPLETE $(date -Is)"
bash "$0" --status

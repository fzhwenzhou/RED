#!/usr/bin/env bash
# Run the RED loop across every project under target_project/.
#
# RED takes ONE project and ONE core per run; this iterates. For each project it
# passes the three things that are genuinely per-project inputs and cannot be
# guessed from the sources:
#
#   harness  the translation unit holding the workload's main(). LEAVE IT EMPTY
#            and the AW agent reads the repository and writes one, with Gate 0
#            measuring whether what it wrote is a workload (red/workload.py).
#            Set it only when the repository already ships a representative
#            one -- micro-ecc's ECDH test does.
#   cflags   compile flags that define the workload's configuration.
#   exclude  path fragments whose .c files belong to a DIFFERENT program in the
#            same repository (a code generator, a second CLI). They stay
#            readable by the agents; they are just not linked.
#
# Projects are configured below by directory name. A project with no entry here
# runs with everything empty, which is the fully automatic path: AW writes the
# workload and Gate 0 decides.
#
# Everything runs on this machine. Projects run one after another because each
# one saturates the box with callgrind and Spike; RED's own fan-out inside a
# run is what uses the cores.
#
#   bash scripts/run_projects.sh                 # every project, once each
#   bash scripts/run_projects.sh libcrc          # just these
#   RED_RUNS=3 bash scripts/run_projects.sh      # three runs of each
#   RED_MODEL=... bash scripts/run_projects.sh   # model for unrouted stages
#   RED_EFFORT=high bash scripts/run_projects.sh # effort for unrouted stages
#   RED_MODELS_CONFIG=other.toml ...             # a different per-stage routing
#   RED_MODELS_CONFIG=/dev/null ...              # no routing: RED_MODEL everywhere
#
# Per-stage routing comes from models.toml (see red/modelcfg.py). RED_MODEL
# and RED_EFFORT are the fallback for stages that file does not name, so to
# compare two models fairly, disable the routing rather than setting RED_MODEL
# and expecting it to win.
#   RED_RUN_PREFIX=opus-5.5-run ...              # name the run dirs for a sweep
#   RED_SKIP_DONE=1 ...                          # resume: skip finished runs
#
# A batch is long and gets interrupted. RED_SKIP_DONE=1 makes re-running the
# same command finish what is missing instead of redoing what is done.
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$REPO_ROOT/.venv"
PROJECTS_DIR="$REPO_ROOT/target_project"
CORE="${RED_CORE:-$REPO_ROOT/target_cpu/picorv32}"
RUNS="${RED_RUNS:-1}"
LOGDIR="${RED_LOGDIR:-$REPO_ROOT/output/batch}"
RUN_PREFIX="${RED_RUN_PREFIX:-run}"
DB_ROOT="${RED_DB_ROOT:-$REPO_ROOT/output/db}"
SKIP_DONE="${RED_SKIP_DONE:-0}"

# Has <project> already produced a usable run of this sweep?
#
# Keyed on the artifact, not on the run number or the log. Two things made
# the obvious tests wrong:
#
#   * a summary.md is written even when the loop gave up because the backend
#     was rate limited, so its presence does not mean a run happened;
#   * a retry does not reuse the failed run's directory -- db_node.claim_run
#     allocates the next free number -- while the log filename is keyed to
#     the sweep index and gets overwritten. So after one retry the stale
#     failed <prefix>-1 sits next to the real result in <prefix>-2, and a
#     check that reads the first against the second's log reports "done"
#     about the wrong directory.
#
# An ISASpec.yaml is the thing a run exists to produce, it is per-directory,
# and nothing writes one unless an agent turn actually succeeded. A run that
# did not converge still has one and still counts; a run the vendor's API
# killed does not and is redone.
already_done() {
    local proj="$1" sep="-"
    [ "$RUN_PREFIX" = run ] && sep="_"
    local d
    for d in "$DB_ROOT/$proj/${RUN_PREFIX}${sep}"*/; do
        [ -f "$d/ISASpec.yaml" ] && return 0
    done
    return 1
}

# project     | harness (empty => AW writes it) | cflags | exclude
config_for() {
    case "$1" in
        micro-ecc)
            HARNESS="test/test_ecdh.c"
            CFLAGS="-DuECC_WORD_SIZE=4 -DuECC_SUPPORTS_secp160r1=0 -DuECC_SUPPORTS_secp192r1=0 -DuECC_SUPPORTS_secp224r1=0 -DuECC_SUPPORTS_secp256k1=0"
            EXCLUDE="" ;;
        libcrc)
            # src/ is the library, test/ and examples/ are drivers, and precalc/
            # is a table generator with its own main whose objects do not link
            # against anything else. Its generated tables (tab/*.inc) must exist:
            # run `make` in the project once before profiling it.
            HARNESS=""
            CFLAGS=""
            EXCLUDE="precalc" ;;
        *)
            HARNESS="" ; CFLAGS="" ; EXCLUDE="" ;;
    esac
}

# Some projects generate part of their own source before they can be compiled.
# RED compiles the sources itself (that is how it instruments them), so the
# generator has to have run first, and the generated files are sources as far
# as RED is concerned even when the project gitignores them as build output.
prepare_project() {
    case "$1" in
        libcrc)
            # bin/prc generates tab/gentab{32,64}.inc, which src/crc{32,64}.c
            # include. They are gitignored as build output; for RED they are
            # sources, so un-ignore them once and generate them if missing.
            local d="$PROJECTS_DIR/libcrc"
            sed -i '/^tab\/\*\.inc$/d' "$d/.gitignore" 2>/dev/null || true
            if [ ! -f "$d/tab/gentab32.inc" ] || [ ! -f "$d/tab/gentab64.inc" ]; then
                echo "    preparing libcrc: running its own table generator"
                (cd "$d" && make >/dev/null 2>&1) || true
            fi
            [ -f "$d/tab/gentab32.inc" ] || echo "!!  libcrc tables missing"
            ;;
    esac
}

mkdir -p "$LOGDIR"
if [ "$#" -gt 0 ]; then
    PROJECTS=("$@")
else
    mapfile -t PROJECTS < <(cd "$PROJECTS_DIR" && find . -maxdepth 1 -mindepth 1 \
                              -type d -printf '%f\n' | sort)
fi

echo "=== RED multi-project batch $(date -Is)"
echo "    core:     $CORE"
echo "    projects: ${PROJECTS[*]}"
echo "    runs each: $RUNS"
echo "    fallback:  ${RED_MODEL:-<CLI default>}   effort: ${RED_EFFORT:-<CLI default>}"
echo "    routing:   ${RED_MODELS_CONFIG:-$REPO_ROOT/models.toml}"
_sep="-"; [ "$RUN_PREFIX" = run ] && _sep="_"
echo "    run dirs:  $DB_ROOT/<project>/${RUN_PREFIX}${_sep}<N>"
echo

for proj in "${PROJECTS[@]}"; do
    dir="$PROJECTS_DIR/$proj"
    [ -d "$dir" ] || { echo "!! no such project: $proj"; continue; }
    config_for "$proj"
    prepare_project "$proj"
    for n in $(seq 1 "$RUNS"); do
        if [ "$SKIP_DONE" = 1 ] && already_done "$proj"; then
            echo "=== PROJECT $proj RUN $n already complete, skipping"
            continue
        fi
        log="$LOGDIR/${proj}_${RUN_PREFIX}${n}.log"
        echo "=== PROJECT $proj RUN $n starting $(date -Is)"
        echo "    harness: ${HARNESS:-<AW writes it>}   exclude: ${EXCLUDE:-none}"
        start=$(date +%s)
        "$VENV/bin/python" -u -m red.loop \
            --project "$dir" --core "$CORE" --workload "$proj" \
            --harness "$HARNESS" --cflags "$CFLAGS" --exclude "$EXCLUDE" \
            --run-prefix "$RUN_PREFIX" \
            ${RED_MODEL:+--model "$RED_MODEL"} \
            ${RED_EFFORT:+--effort "$RED_EFFORT"} \
            ${RED_MODELS_CONFIG+--models-config "$RED_MODELS_CONFIG"} \
            > "$log" 2>&1
        rc=$?
        echo "=== PROJECT $proj RUN $n finished rc=$rc in $(( $(date +%s) - start ))s"
        grep -E "^- result|^- Gate [0-9]|^- spec review" "$log" | head -10
        echo
    done
done
echo "=== BATCH COMPLETE $(date -Is)"

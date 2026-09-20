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
#   bash scripts/run_projects.sh                 # every project, once each
#   bash scripts/run_projects.sh libcrc          # just these
#   RED_RUNS=3 bash scripts/run_projects.sh      # three runs of each
set -uo pipefail

REPO_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
VENV="$REPO_ROOT/.venv"
PROJECTS_DIR="$REPO_ROOT/target_project"
CORE="${RED_CORE:-$REPO_ROOT/target_cpu/picorv32}"
RUNS="${RED_RUNS:-1}"
LOGDIR="${RED_LOGDIR:-$REPO_ROOT/output/batch}"

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

# ORDERING, and it matters: prepare_project runs on the HEAD, but the profile
# node compiles the project from its own copy of the tree, which is rsynced only
# by a fresh `red_env.sh up` (`up --add` against a live head does not re-sync).
# So generated sources must exist BEFORE the cluster is brought up. This script
# checks and says so rather than letting every build on the worker fail on an
# include the head can resolve perfectly well.
#
# Some projects generate part of their own source before they can be compiled.
# RED compiles the sources itself (that is how it instruments them), so the
# generator has to have run first -- and the generated files have to reach the
# profile node, which they do not if the project's .gitignore excludes them:
# the cluster ships the working directory with gitignored paths removed, so a
# generated *source* silently goes missing on the worker and every build there
# fails on an include the head can resolve perfectly well.
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
            # If the cluster is already up, the workers have the pre-generation
            # copy of the tree and every build there will fail.
            if "$VENV/bin/ray" status >/dev/null 2>&1; then
                echo "    NOTE: if the tables were generated after the cluster"
                echo "          came up, re-run: bash scripts/red_env.sh down && up"
            fi
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
echo

for proj in "${PROJECTS[@]}"; do
    dir="$PROJECTS_DIR/$proj"
    [ -d "$dir" ] || { echo "!! no such project: $proj"; continue; }
    config_for "$proj"
    prepare_project "$proj"
    for n in $(seq 1 "$RUNS"); do
        log="$LOGDIR/${proj}_run${n}.log"
        echo "=== PROJECT $proj RUN $n starting $(date -Is)"
        echo "    harness: ${HARNESS:-<AW writes it>}   exclude: ${EXCLUDE:-none}"
        start=$(date +%s)
        "$VENV/bin/python" -u -m red.loop \
            --project "$dir" --core "$CORE" --workload "$proj" \
            --harness "$HARNESS" --cflags "$CFLAGS" --exclude "$EXCLUDE" \
            > "$log" 2>&1
        rc=$?
        echo "=== PROJECT $proj RUN $n finished rc=$rc in $(( $(date +%s) - start ))s"
        grep -E "^- result|^- Gate [0-9]|^- spec review" "$log" | head -10
        echo
    done
done
echo "=== BATCH COMPLETE $(date -Is)"

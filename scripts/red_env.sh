#!/usr/bin/env bash
# =============================================================================
# red_env.sh — environment manager for RED.
#
#   bash scripts/red_env.sh setup    # one-time: venv, packages, graph, self-test
#   bash scripts/red_env.sh check    # is this machine ready to run the loop?
#   bash scripts/red_env.sh status   # graph + toolchain + backend at a glance
#   bash scripts/red_env.sh stop     # stop the knowledge graph (optional)
#
# RED runs entirely on this machine. There is no cluster to bring up and
# nothing that bills while it is idle: the agent turns are subprocesses of the
# `claude` CLI, the tools are in-process MCP servers on loopback, and the only
# long-lived piece is the Neo4j knowledge graph, which is RED's memory across
# runs and is deliberately left running.
#
# Layout it relies on:
#   $HOME/RED           this repo (venv at .venv/)
#   claude              the Claude Code CLI, on PATH and already authenticated
# =============================================================================

set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
VENV="${RED_VENV:-$REPO_ROOT/.venv}"
CLAUDE_BIN="${RED_CLAUDE_BIN:-claude}"

# ---------------------------------------------------------------------------
# logging
# ---------------------------------------------------------------------------
if [ -t 1 ]; then B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; N=$'\033[0m'
else B=""; G=""; Y=""; R=""; N=""; fi
say()  { echo "${B}==>${N} $*"; }
ok()   { echo "  ${G}ok${N} $*"; }
warn() { echo "  ${Y}warning:${N} $*" >&2; }
die()  { echo "  ${R}error:${N} $*" >&2; exit 1; }
have() { command -v "$1" >/dev/null 2>&1; }
py()   { "$VENV/bin/python" "$@"; }

# ===========================================================================
# setup steps (idempotent)
# ===========================================================================

step_venv() {
    say "Python venv + packages"
    have python3 || die "python3 not found"
    if [ ! -x "$VENV/bin/python" ]; then
        python3 -m venv "$VENV"
        ok "created $VENV"
    fi
    py -c "import red" 2>/dev/null \
        || { py -m pip install -q -e "$REPO_ROOT"; ok "installed red (editable)"; }
    py -c "import neo4j" 2>/dev/null \
        || { py -m pip install -q "neo4j>=5.0"; ok "installed neo4j driver"; }
    py - <<'EOF' || die "venv sanity check failed"
import red, mcp, uvicorn
EOF
    ok "venv imports: red, mcp, uvicorn"
}

# ---------------------------------------------------------------------------
# The agent backend.
#
# Every A1/A2/review/security/workload turn is `claude -p` with a generated
# MCP config and an explicit --allowedTools list (red/backend.py). The CLI
# carries its own authentication, so there is nothing for RED to configure --
# but if it is missing or unauthenticated, every turn fails identically and
# the loop degrades to its mechanical path, which is a confusing way to find
# out. So check it here, once, where the message can say what to do.
# ---------------------------------------------------------------------------
step_backend() {
    say "Agent backend (Claude Code CLI)"
    have "$CLAUDE_BIN" || die "'$CLAUDE_BIN' not on PATH — install the Claude Code CLI, or run with --mechanical"
    ok "$CLAUDE_BIN $("$CLAUDE_BIN" --version 2>/dev/null | head -1)"
    local probe
    probe="$(echo 'reply with the single word: ready' \
             | timeout 120 "$CLAUDE_BIN" -p --output-format text 2>&1)" \
        && ok "a turn round-trips: $(echo "$probe" | head -1 | cut -c1-60)" \
        || warn "the CLI did not answer a probe turn — authenticate it ($CLAUDE_BIN), then re-run"
}

step_toolchain() {
    say "Toolchain"
    py - <<'EOF'
from red.constants import EXPECTED_TOOLS
import shutil
missing = []
for group, tools in EXPECTED_TOOLS.items():
    have = [t for t in tools if shutil.which(t)]
    gone = [t for t in tools if not shutil.which(t)]
    missing += gone
    mark = "ok" if not gone else "  "
    print(f"  {mark} {group:8s} {' '.join(have)}" + (f"   MISSING: {' '.join(gone)}" if gone else ""))
if missing:
    print("     (a missing tool degrades the stage that needs it to a typed")
    print("      'not available' result; spike comes from scripts/setup_spike.sh)")
EOF
    # Gate 4's constant-time evidence is memcheck under the ctgrind technique,
    # and memcheck refuses to start at all without glibc debuginfo. The gate
    # reports that honestly as NOT CHECKED rather than passing on it -- which
    # is correct, and also easy to miss at the bottom of a run summary. Say it
    # here instead, where it is one apt away from being fixed.
    if command -v valgrind >/dev/null 2>&1; then
        if valgrind --tool=memcheck --error-exitcode=0 /bin/true >/dev/null 2>&1; then
            ok "memcheck runs — Gate 4's constant-time check is live"
        else
            warn "memcheck cannot start, so Gate 4 will report its secret-branch"
            warn "check as NOT CHECKED. Fix: sudo apt-get install -y libc6-dbg"
        fi
    fi
}

# ---------------------------------------------------------------------------
# Knowledge graph (Neo4j) — see scripts/setup_neo4j.sh
#
# Its data outlives every run, so setup only has to make sure it is up. It is
# optional: without it RED still runs, it just starts every run with no memory
# of the previous ones.
# ---------------------------------------------------------------------------
graph_up() {
    local s="$SCRIPT_DIR/setup_neo4j.sh"
    local home="${RED_NEO4J_HOME:-$HOME/.local/share/red-neo4j}"
    if [ ! -x "$home/bin/neo4j" ]; then
        warn "no knowledge graph installed — runs will not record their history"
        warn "install it once with:  bash $s install"
        return 0
    fi
    if bash "$s" start >/dev/null 2>&1; then
        ok "knowledge graph up ($(py -m red.graph --stats 2>/dev/null \
             | sed 's/^ *//' | head -1 || echo 'bolt://127.0.0.1:7687'))"
    else
        warn "knowledge graph did not start — the run will proceed without history"
    fi
}

step_tests() {
    say "Environment self-test"
    (cd "$REPO_ROOT" && py tests/test_env.py) && ok "tests/test_env.py passed" \
        || die "tests/test_env.py failed — fix before running the loop"
}

# ===========================================================================
# commands
# ===========================================================================

cmd_setup() {
    step_venv
    step_backend
    step_toolchain
    graph_up
    step_tests
    echo
    ok "setup complete — run the loop with:  python -m red.loop"
}

cmd_check() {
    step_backend
    step_toolchain
    graph_up
    echo
    ok "ready — run the loop with:  python -m red.loop"
}

cmd_status() {
    say "Agent backend"
    have "$CLAUDE_BIN" \
        && ok "$CLAUDE_BIN $("$CLAUDE_BIN" --version 2>/dev/null | head -1)" \
        || warn "'$CLAUDE_BIN' not on PATH"
    echo
    step_toolchain
    echo
    say "Knowledge graph"
    py -m red.graph --stats 2>/dev/null \
        || echo "  (not running — bash $SCRIPT_DIR/setup_neo4j.sh start)"
    echo
    say "Runs on disk"
    local db="${RED_DB_ROOT:-$REPO_ROOT/output/db}"
    [ -d "$db" ] \
        && echo "  $(find "$db" -maxdepth 2 -name 'run_*' -type d | wc -l) run(s) under $db" \
        || echo "  none yet ($db)"
}

cmd_stop() {
    say "Stopping the knowledge graph"
    bash "$SCRIPT_DIR/setup_neo4j.sh" stop \
        && ok "stopped (its data is kept; 'setup_neo4j.sh start' brings it back)" \
        || warn "it was not running"
}

case "${1:-}" in
    setup)   cmd_setup ;;
    check)   cmd_check ;;
    status)  cmd_status ;;
    stop)    cmd_stop ;;
    *) sed -n '2,19p' "$0"; exit "${1:+1}" ;;
esac

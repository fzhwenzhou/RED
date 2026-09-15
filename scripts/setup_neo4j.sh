#!/usr/bin/env bash
# =============================================================================
# setup_neo4j.sh — RED's persistent design-knowledge graph.
#
#   bash scripts/setup_neo4j.sh install   # one-time: JDK + Neo4j into a prefix
#   bash scripts/setup_neo4j.sh start     # start the server (idempotent)
#   bash scripts/setup_neo4j.sh stop      # stop it (the DATA SURVIVES)
#   bash scripts/setup_neo4j.sh status    # is it up, and what is in it
#   bash scripts/setup_neo4j.sh wipe      # delete the graph (asks first)
#
# WHY A USERLAND INSTALL. RED's other native dependency (Spike) installs the
# same way, into an ordinary ~/.local prefix, because this host has no
# passwordless sudo and no docker group — apt and containers are both
# unavailable. Everything here therefore lives under RED_NEO4J_PREFIX and
# needs no privileges.
#
# WHY ON THE HEAD, NOT A WORKER. The graph is RED's memory *across* runs: what
# earlier runs designed for a hot loop, what it was predicted to be worth, and
# what the reviewers said about it. GCP workers are created and destroyed by
# `red_env.sh up`/`down`, so a graph living on one would be erased on every
# teardown, which is the opposite of persistent. It runs on the head, whose
# disk outlives any cluster, and workers reach it over bolt.
#
# WHERE THE DATA LIVES. RED_NEO4J_HOME/data, i.e. ~/.local/share/red-neo4j by
# default — deliberately NOT under the repo's output/, which the operator is
# told to delete between test batches.
# =============================================================================

set -euo pipefail

PREFIX="${RED_NEO4J_PREFIX:-$HOME/.local}"
NEO4J_HOME="${RED_NEO4J_HOME:-$PREFIX/share/red-neo4j}"
SRC="$PREFIX/src"
JDK_DIR="$PREFIX/share/red-jdk21"
NEO4J_VERSION="${RED_NEO4J_VERSION:-5.26.0}"
BOLT_PORT="${RED_NEO4J_BOLT_PORT:-7687}"
HTTP_PORT="${RED_NEO4J_HTTP_PORT:-7474}"
NEO4J_USER="${RED_NEO4J_USER:-neo4j}"
NEO4J_PASSWORD="${RED_NEO4J_PASSWORD:-redgraph1}"

if [ -t 1 ]; then B=$'\033[1m'; G=$'\033[32m'; Y=$'\033[33m'; R=$'\033[31m'; N=$'\033[0m'
else B=""; G=""; Y=""; R=""; N=""; fi
say()  { echo "${B}==>${N} $*"; }
ok()   { echo "  ${G}ok${N} $*"; }
warn() { echo "  ${Y}warning:${N} $*" >&2; }
die()  { echo "  ${R}error:${N} $*" >&2; exit 1; }

export JAVA_HOME="$JDK_DIR"
export PATH="$JDK_DIR/bin:$NEO4J_HOME/bin:$PATH"

# ---------------------------------------------------------------------------

install_jdk() {
    say "JDK 21 (Neo4j 5 requires 17 or 21; this host has no system java)"
    # Verify it RUNS, not merely that the file is there: an x64 tarball
    # unpacked on ARM leaves an executable java that dies with "cannot execute
    # binary file", and a mere -x test would happily keep it.
    if "$JDK_DIR/bin/java" -version >/dev/null 2>&1; then
        ok "already at $JDK_DIR"; return
    fi
    mkdir -p "$SRC" "$JDK_DIR"
    # Adoptium names the ARM build "aarch64" and the Intel one "x64"; this repo
    # has been run on both (the dev host is aarch64 WSL, the GCP nodes are x64),
    # and an x64 tarball on ARM fails as "cannot execute binary file".
    local arch; arch="$(uname -m)"
    case "$arch" in
        x86_64|amd64) arch=x64 ;;
        aarch64|arm64) arch=aarch64 ;;
        *) die "no Temurin build known for $arch" ;;
    esac
    local jdk_tar="$SRC/jdk21-$arch.tar.gz"
    if [ ! -s "$jdk_tar" ]; then
        say "downloading Temurin 21 for $arch"
        curl -fsSL -o "$jdk_tar" \
          "https://api.adoptium.net/v3/binary/latest/21/ga/linux/$arch/jdk/hotspot/normal/eclipse"
    fi
    rm -rf "${JDK_DIR:?}"/* 2>/dev/null || true
    tar -xzf "$jdk_tar" -C "$JDK_DIR" --strip-components=1
    "$JDK_DIR/bin/java" -version 2>&1 | head -1 | sed 's/^/  /'
    ok "installed $JDK_DIR"
}

install_neo4j() {
    say "Neo4j Community $NEO4J_VERSION"
    if [ -x "$NEO4J_HOME/bin/neo4j" ]; then ok "already at $NEO4J_HOME"; return; fi
    mkdir -p "$SRC" "$NEO4J_HOME"
    if [ ! -s "$SRC/neo4j.tar.gz" ]; then
        say "downloading Neo4j"
        curl -fsSL -o "$SRC/neo4j.tar.gz" \
          "https://dist.neo4j.org/neo4j-community-$NEO4J_VERSION-unix.tar.gz"
    fi
    tar -xzf "$SRC/neo4j.tar.gz" -C "$NEO4J_HOME" --strip-components=1
    ok "installed $NEO4J_HOME"
}

configure() {
    say "configuration"
    local conf="$NEO4J_HOME/conf/neo4j.conf"
    [ -f "$conf" ] || die "no $conf — run: bash $0 install"
    # Listen on all interfaces so tunnelled GCP workers can reach the head's
    # graph; the port is not exposed publicly (no firewall rule opens it).
    python3 - "$conf" "$BOLT_PORT" "$HTTP_PORT" <<'PY'
import re, sys
path, bolt, http = sys.argv[1], sys.argv[2], sys.argv[3]
want = {
    "server.default_listen_address": "0.0.0.0",
    "server.bolt.enabled": "true",
    "server.bolt.listen_address": f":{bolt}",
    "server.http.enabled": "true",
    "server.http.listen_address": f":{http}",
    # A single run writes a few hundred nodes; the defaults are sized for far
    # more and would reserve a lot of this laptop's RAM for nothing.
    "server.memory.heap.initial_size": "512m",
    "server.memory.heap.max_size": "1g",
    "server.memory.pagecache.size": "512m",
}
lines = open(path).read().splitlines()
out, seen = [], set()
for line in lines:
    m = re.match(r"^\s*#?\s*([\w.]+)\s*=", line)
    key = m.group(1) if m else None
    if key in want:
        if key not in seen:
            out.append(f"{key}={want[key]}")
            seen.add(key)
        continue           # drop duplicates/commented originals
    out.append(line)
for key, val in want.items():
    if key not in seen:
        out.append(f"{key}={val}")
open(path, "w").write("\n".join(out) + "\n")
PY
    ok "bolt :$BOLT_PORT, http :$HTTP_PORT, heap 1g"
}

set_password() {
    # Neo4j refuses connections until the initial password is changed. Doing it
    # offline (server stopped) is the only way that needs no prior credentials.
    if [ -f "$NEO4J_HOME/data/.red_password_set" ]; then return; fi
    say "initial password"
    mkdir -p "$NEO4J_HOME/data"
    # Only record success. Marking it done regardless would leave a server
    # nothing can authenticate against, and the next `install` would skip the
    # step that fixes it.
    if "$NEO4J_HOME/bin/neo4j-admin" dbms set-initial-password "$NEO4J_PASSWORD" >/dev/null 2>&1; then
        touch "$NEO4J_HOME/data/.red_password_set"
        ok "set"
    else
        warn "could not set the initial password — if this is a fresh install, "
        warn "check $NEO4J_HOME/logs and re-run; if the store already has one, "
        warn "it is unchanged and RED_NEO4J_PASSWORD must match it"
    fi
}

wait_up() {
    local tries="${1:-60}"
    for _ in $(seq "$tries"); do
        if "$NEO4J_HOME/bin/cypher-shell" -a "bolt://localhost:$BOLT_PORT" \
             -u "$NEO4J_USER" -p "$NEO4J_PASSWORD" "RETURN 1;" >/dev/null 2>&1; then
            return 0
        fi
        sleep 2
    done
    return 1
}

cmd_install() {
    install_jdk
    install_neo4j
    configure
    set_password
    echo
    ok "installed. Start it with:  bash $0 start"
}

cmd_start() {
    [ -x "$NEO4J_HOME/bin/neo4j" ] || die "not installed — run: bash $0 install"
    say "starting Neo4j"
    if wait_up 1; then ok "already running"; else
        "$NEO4J_HOME/bin/neo4j" start >/dev/null 2>&1 || true
        wait_up 60 || die "did not come up — see $NEO4J_HOME/logs/neo4j.log"
        ok "up on bolt://localhost:$BOLT_PORT"
    fi
    say "applying schema"
    (cd "${RED_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}" \
        && ./.venv/bin/python -m red.graph --ensure-schema) \
        && ok "constraints and indexes in place" \
        || warn "schema step failed — is the venv set up?"
}

cmd_stop() {
    say "stopping Neo4j (the graph on disk is kept)"
    "$NEO4J_HOME/bin/neo4j" stop >/dev/null 2>&1 || true
    ok "stopped; data remains in $NEO4J_HOME/data"
}

cmd_status() {
    say "Neo4j at $NEO4J_HOME"
    if wait_up 1; then
        ok "running on bolt://localhost:$BOLT_PORT"
        (cd "${RED_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}" \
            && ./.venv/bin/python -m red.graph --stats) || true
    else
        warn "not running (start it with: bash $0 start)"
    fi
    echo "  data: $NEO4J_HOME/data  ($(du -sh "$NEO4J_HOME/data" 2>/dev/null | cut -f1))"
}

cmd_wipe() {
    say "this deletes every run recorded in the graph"
    read -r -p "  type 'wipe' to confirm: " a
    [ "$a" = "wipe" ] || { warn "aborted"; exit 1; }
    (cd "${RED_REPO_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}" \
        && ./.venv/bin/python -m red.graph --wipe)
    ok "graph emptied"
}

case "${1:-}" in
    install) cmd_install ;;
    start)   cmd_start ;;
    stop)    cmd_stop ;;
    status)  cmd_status ;;
    wipe)    cmd_wipe ;;
    *) sed -n '2,12p' "$0"; exit "${1:+1}" ;;
esac

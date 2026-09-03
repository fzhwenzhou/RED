#!/usr/bin/env bash
# =============================================================================
# red_env.sh — one-stop environment manager for the RED cluster.
#
#   bash scripts/red_env.sh setup    # one-time: local head prep + GCP auth + up
#   bash scripts/red_env.sh up       # (re)bring-up the cluster (idempotent)
#   bash scripts/red_env.sh down     # tear everything down, verify nothing bills
#   bash scripts/red_env.sh status   # ray + GCP state at a glance
#
# The head is THIS machine (WSL). Workers are GCP spot/preemptible VMs defined
# in cluster.yaml. Everything here is idempotent — safe to re-run any command.
#
# Layout it relies on:
#   $HOME/RED           this repo (venv at .venv/, config at cluster.yaml)
#   $HOME/chia          CHIA checkout (installed editable into the venv)
#   ~/.ssh/id_ed25519   SSH keypair shared with the GCP workers
#   GCP project         RED_GCP_PROJECT (default: project-160a0199-6b4a-464c-86a)
# =============================================================================

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
CHIA_DIR="${RED_CHIA_DIR:-$HOME/chia}"
VENV="${RED_VENV:-$REPO_ROOT/.venv}"
CLUSTER_YAML="${RED_CLUSTER_YAML:-$REPO_ROOT/cluster.yaml}"
export RED_GCP_PROJECT="${RED_GCP_PROJECT:-project-160a0199-6b4a-464c-86a}"
SSH_KEY="${RED_SSH_KEY:-$HOME/.ssh/id_ed25519}"

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
gcp()  { py "$SCRIPT_DIR/gcp_util.py" "$@"; }

# ---------------------------------------------------------------------------
# Cluster environment — resolved fresh every run (WSL IP changes on reboot).
#   HEAD_IP        this machine's own IP (NOT the public egress IP — CHIA
#                  brings the head up over SSH to this address, and the NAT/
#                  router refuses inbound :22 on the public one)
#   GCP_COMPUTE_SA default compute SA, attached to the llm node so Vertex AI
#                  works via the metadata server (no gcloud login on the node)
# ---------------------------------------------------------------------------
export_cluster_env() {
    export HEAD_IP="${HEAD_IP:-$(hostname -I | awk '{print $1}')}"
    export USER="${USER:-$(whoami)}"
    if [ -z "${GCP_COMPUTE_SA:-}" ]; then
        GCP_COMPUTE_SA="$(gcp default-sa 2>/dev/null)" || warn "could not resolve default compute SA (ADC set up?)"
        export GCP_COMPUTE_SA
    fi
    say "HEAD_IP=$HEAD_IP  USER=$USER  project=$RED_GCP_PROJECT"
}

# ===========================================================================
# setup steps (idempotent)
# ===========================================================================

step_ssh_key() {
    say "SSH keypair"
    if [ ! -f "$SSH_KEY" ]; then
        ssh-keygen -t ed25519 -N "" -f "$SSH_KEY" -q
        ok "generated $SSH_KEY"
    else
        ok "$SSH_KEY exists"
    fi
    # The head must be able to SSH to itself (CHIA brings the head up over SSH).
    touch "$HOME/.ssh/authorized_keys"
    if ! grep -qf "$SSH_KEY.pub" "$HOME/.ssh/authorized_keys" 2>/dev/null; then
        cat "$SSH_KEY.pub" >> "$HOME/.ssh/authorized_keys"
        chmod 600 "$HOME/.ssh/authorized_keys"
        ok "authorized own pubkey"
    else
        ok "pubkey already authorized"
    fi
}

step_sshd() {
    say "SSH server on the head (WSL)"
    if ! have sshd && ! dpkg -s openssh-server >/dev/null 2>&1; then
        sudo apt-get update -qq
        sudo DEBIAN_FRONTEND=noninteractive apt-get install -y openssh-server
        ok "openssh-server installed"
    else
        ok "openssh-server present"
    fi
    if ! pgrep -x sshd >/dev/null; then
        sudo systemctl enable --now ssh
        ok "sshd started"
    else
        ok "sshd running"
    fi
    export HEAD_IP="${HEAD_IP:-$(hostname -I | awk '{print $1}')}"
    timeout 10 ssh -o StrictHostKeyChecking=no -o BatchMode=yes -i "$SSH_KEY" \
        "$USER@$HEAD_IP" true 2>/dev/null \
        && ok "self-SSH to $HEAD_IP works" \
        || die "self-SSH to $HEAD_IP failed — check sshd and authorized_keys"
}

step_venv() {
    say "Python venv + packages"
    have python3 || die "python3 not found"
    if [ ! -x "$VENV/bin/python" ]; then
        python3 -m venv "$VENV"
        ok "created $VENV"
    fi
    py -c "import chia" 2>/dev/null || { py -m pip install -q -e "$CHIA_DIR"; ok "installed chia (editable)"; }
    py -c "import red"  2>/dev/null || { py -m pip install -q -e "$REPO_ROOT"; ok "installed red (editable)"; }
    py - <<'EOF' || die "venv sanity check failed"
import chia, red, ray, google.auth
from google.cloud import compute_v1
EOF
    ok "venv imports: chia, red, ray, google-cloud-compute"
}

step_gcp_auth() {
    say "GCP Application Default Credentials (head)"
    local adc="$HOME/.config/gcloud/application_default_credentials.json"
    if [ ! -f "$adc" ]; then
        have gcloud || die "gcloud CLI not found — install the Google Cloud SDK first"
        warn "no ADC found — starting interactive login (browser)..."
        gcloud auth application-default login
    fi
    ok "ADC present"
    # Silence the 'no quota project' warning and bill API usage correctly.
    gcloud auth application-default set-quota-project "$RED_GCP_PROJECT" >/dev/null 2>&1 \
        && ok "quota project set" || warn "set-quota-project failed (non-fatal)"
    say "GCP APIs (compute, aiplatform)"
    gcp ensure-apis || die "could not enable required GCP APIs"
}

step_tests() {
    say "Environment self-test"
    (cd "$REPO_ROOT" && py tests/test_env.py) && ok "tests/test_env.py passed" \
        || die "tests/test_env.py failed — fix before bringing the cluster up"
}

# ===========================================================================
# cluster commands
# ===========================================================================

cmd_up() {
    export_cluster_env
    [ -f "$HOME/.config/gcloud/application_default_credentials.json" ] \
        || die "no ADC — run: bash $0 setup"
    gcp ensure-apis >/dev/null || die "required GCP APIs not enabled"
    # The llm node authenticates to Vertex as the default compute SA (metadata
    # ADC). That SA ships with NO project roles on fresh projects — grant the
    # predict role or every LLM turn 403s with aiplatform.endpoints.predict
    # denied.
    gcp ensure-iam >/dev/null || die "could not grant aiplatform.user to the default compute SA"

    # The llm node gets its Vertex credentials from its attached service
    # account, which is only settable at instance creation. If a stale llm
    # instance is running without one, say so loudly instead of failing
    # mysteriously inside red.loop later.
    local llm_sa; llm_sa="$(gcp llm-sa)"
    if [ "$llm_sa" = "NONE" ]; then
        warn "running llm node has NO service account — Vertex calls will fail."
        warn "recreate it:  bash $0 down && bash $0 up"
    fi

    say "chia up"
    (cd "$REPO_ROOT" && "$VENV/bin/chia" up -y "$CLUSTER_YAML")

    say "ray status"
    "$VENV/bin/ray" status | sed -n '1,12p'
    local active; active="$("$VENV/bin/ray" status | grep -c '^ 1 node_' || true)"
    [ "$active" -ge 8 ] && ok "cluster up: $active nodes" \
        || warn "only $active nodes active (expected 8) — check the log above"

    [ "$(gcp llm-sa)" != "NONE" ] && [ "$(gcp llm-sa)" != "ABSENT" ] \
        && ok "llm node service account attached (Vertex ADC via metadata)"
    echo
    ok "cluster ready — run the loop with:  python -m red.loop"
}

cmd_down() {
    export_cluster_env
    say "chia down"
    (cd "$REPO_ROOT" && "$VENV/bin/chia" down -y "$CLUSTER_YAML") \
        || warn "chia down reported errors — force-cleaning leftovers directly"
    # Safety net: `chia down` can fail midway (e.g. a preempted spot node
    # breaks its config expansion) and leave instances billing. Delete any
    # remaining cluster resources directly via the compute API.
    say "force-clean any leftovers"
    gcp force-clean
    "$VENV/bin/ray" stop >/dev/null 2>&1 || true
    say "verifying nothing is left billing"
    gcp verify-clean || die "leftover resources above — delete them in the console or re-run down"
    ok "cluster down, local ray stopped, nothing billing"
}

cmd_status() {
    export_cluster_env
    say "ray (local head)"
    "$VENV/bin/ray" status 2>/dev/null | sed -n '1,12p' || warn "no local ray cluster"
    echo
    say "GCP project $RED_GCP_PROJECT"
    gcp list
}

cmd_setup() {
    step_ssh_key
    step_sshd
    step_venv
    step_gcp_auth
    step_tests
    cmd_up
    echo
    ok "setup complete. Daily use:  bash $0 up / down / status"
}

# ===========================================================================

case "${1:-}" in
    setup)        cmd_setup ;;
    up)           cmd_up ;;
    down|clean)   cmd_down ;;
    status)       cmd_status ;;
    *) sed -n '2,16p' "$0"; exit "${1:+1}" ;;
esac

"""Cross-node constants for the RED loop.

RED is **general-purpose**: given ONE application project (a source repo +
representative workloads) and ONE processor core, the A1/A2 agents profile the
project, mine its hot loops, and design a custom-instruction extension for it.
Nothing about any specific workload, core, or "hot function" is hard-coded —
the agents discover hot loops and design instructions from the project's own
source and measured profiles. The platform (core + compiler path) is an input,
not a constant.

This repo bundles ONE worked example so the loop runs out of the box — the
micro-ecc ECDH/ECDSA library accelerated on a PicoRV32 core — under
``target_project/`` and ``target_cpu/``. ``EXAMPLE_PROJECT`` / ``EXAMPLE_CORE``
point at it as *default* inputs, overridable at run time via ``--project`` /
``--core`` / ``--workload``.

Resource tags map 1:1 onto the node types in ``cluster.yaml``.
"""

from __future__ import annotations

import os

# ---------------------------------------------------------------------------
# Repo layout
# ---------------------------------------------------------------------------

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Candidate application projects and cores live here. RED takes one of each.
TARGET_PROJECTS = os.path.join(REPO_ROOT, "target_project")
TARGET_CPUS = os.path.join(REPO_ROOT, "target_cpu")

# The bundled worked example (defaults only — every value below is an INPUT the
# CLI overrides). The harness + cflags together *define* the workload: micro-ecc
# builds every supported curve into its test binaries, so pinning the workload
# to ECDH over secp256r1 means selecting that test's main() and switching the
# other curves off. uECC_WORD_SIZE=4 makes the profiled limb arithmetic 32-bit,
# matching the RISC-V rv32 target the extension is designed for — profiling with
# the host's native 64-bit limbs would mine the wrong operand widths.
EXAMPLE_PROJECT = os.path.join(TARGET_PROJECTS, "micro-ecc")
EXAMPLE_CORE = os.path.join(TARGET_CPUS, "picorv32")
EXAMPLE_WORKLOAD = "ECDH-secp256r1"
EXAMPLE_HARNESS = "test/test_ecdh.c"     # project-relative main() TU
EXAMPLE_CFLAGS = ("-DuECC_WORD_SIZE=4 "
                  "-DuECC_SUPPORTS_secp160r1=0 -DuECC_SUPPORTS_secp192r1=0 "
                  "-DuECC_SUPPORTS_secp224r1=0 -DuECC_SUPPORTS_secp256k1=0")


# ``.inc``/``.inl`` are C sources under another name and must be included:
# micro-ecc keeps its whole curve-specific arithmetic -- including the
# secp256r1 fast reduction that is 43% of this workload's cycles -- in
# ``curve-specific.inc``. Omitting the extension made that code invisible to
# every agent, so the designer wrote a model for the hottest kernel in the
# program without ever being able to read it. ``.sv`` for the same reason on
# the core side.
SOURCE_EXTS = (".c", ".h", ".inc", ".inl", ".S", ".s", ".v", ".sv", ".vh")


def find_sources(root: str, exts=SOURCE_EXTS):
    """Yield (repo-relative path, absolute path) for source files under *root*.

    The A1 mining agent and the sealed SourceTool both locate the project/core
    sources through this — never through a hard-coded file list.
    """
    for dirpath, _dirs, files in os.walk(root):
        if "/.git" in dirpath:
            continue
        for fn in files:
            if fn.endswith(exts):
                full = os.path.join(dirpath, fn)
                yield os.path.relpath(full, REPO_ROOT), full


# ---------------------------------------------------------------------------
# Cluster resources (match cluster.yaml available_node_types)
# ---------------------------------------------------------------------------

RES_LLM = "llm"                  # agent nodes (A1 mining, A2 synthesis)
RES_DATABASE = "database"        # durable artifact store
RES_HEAD_LOCAL = "head_local"    # sealed tools + driver-side gates and links
RES_PROFILE = "profile"          # native harness build + perf/callgrind (A1)
RES_SPIKE = "spike"              # patched Spike ISS (Gate 2)

# ---------------------------------------------------------------------------
# Gates & budgets (RED design §2)
# ---------------------------------------------------------------------------

GATE1_TOLERANCE = 0.05           # independent re-profiles must agree within ±5%
COVERAGE_TARGET = 0.80           # ranked loops must cover >=80% of cycles
LINK1_VECTORS = 100_000          # vectors each instruction's C model is run on
GATE2_VECTORS = 100_000          # vectors the C model and Spike must agree on
GATE2_BLOCK = 1_024              # vectors per checksum in Gate 2's phase 1
# Gate 2's per-instruction tests are subprocess-bound (cross-compile, native
# run, Spike run), so they overlap well on threads. Capped so a large spec
# cannot oversubscribe a small profile/head node.
GATE2_MAX_PARALLEL = int(os.environ.get("RED_GATE2_PARALLEL", "4"))
CRITIC_MAX_ROUNDS = 3            # S2c self-critique cap (the review
                                 # sub-agents in red.review do the rest)
REPAIR_MAX_ROUNDS = 3            # link-1 failure -> agentic repair, then escalation
# review -> designer revision -> re-review, capped. Raise it (RED_REVIEW_ROUNDS)
# when the reviewers are still finding blocking issues at the cap and you want
# the designer to keep going; each extra round costs one review fan-out plus one
# designer turn.
REVIEW_MAX_ROUNDS = int(os.environ.get("RED_REVIEW_ROUNDS", "4"))
REVIEW_MAX_FINDINGS = 12         # per reviewer; keeps a runaway reply bounded

# ---------------------------------------------------------------------------
# Platform cost model (red/cost.py) — every number measured, not assumed
# ---------------------------------------------------------------------------
#
# Fitted to the RTL harness in eval/: mac96 moves 10 words and takes 48 cycles,
# add256 moves 32 and takes 92, so a beat is 2 cycles and the fixed PCPI + FSM
# overhead is 28. CYCLES_PER_IR converts the profiler's host instruction counts
# into PicoRV32 cycles, from the measured 298,502,758 cycles / 35,063,482 Ir of
# one full ECDH exchange. Override for a different core.
FIXED_CYCLES = float(os.environ.get("RED_FIXED_CYCLES", "28"))
CYCLES_PER_BEAT = float(os.environ.get("RED_CYCLES_PER_BEAT", "2"))
MAC_CYCLES = float(os.environ.get("RED_MAC_CYCLES", "1"))
CPU_LOAD_STORE = float(os.environ.get("RED_CPU_LOAD_STORE", "5"))
CYCLES_PER_IR = float(os.environ.get("RED_CYCLES_PER_IR", "8.51"))

# Gate 3 rejects an extension predicted to deliver less than this. The point of
# designing custom instructions is not to break even.
SPEEDUP_TARGET = float(os.environ.get("RED_SPEEDUP_TARGET", "2.0"))
PERF_MAX_ROUNDS = int(os.environ.get("RED_PERF_ROUNDS", "4"))

# ---------------------------------------------------------------------------
# Gate 4 — security bounds (red/security.py)
# ---------------------------------------------------------------------------
#
# An ISA extension is a permanent widening of the machine's attack surface: a
# custom instruction runs at the privilege of whatever calls it, stalls the
# pipeline for its whole latency, and — on the crypto kernels RED mines — its
# operands ARE the secret. None of the other gates ask about that. Gate 4 does,
# and it asks *mechanically* wherever a machine can answer: a sanitizer, a
# simulator or an instruction count settles a security claim far better than a
# model's opinion of one. The security agent (red/prompts/security.md) proposes;
# these numbers dispose.
#
# Vectors the memory-safety battery runs per instruction: every adversarial
# pattern (all-zeros, all-ones, single bits at every word boundary, carry
# ripples) plus pseudo-random fill. Each vector is run twice against two
# different poisoned output buffers, so an unwritten output word — a residue
# leak of whatever the caller left in the destination block — is caught as a
# difference rather than being invisible against zeroed memory.
SECURITY_VECTORS = int(os.environ.get("RED_SECURITY_VECTORS", "512"))
# Distinct operand values the constant-time check counts instructions for. The
# model is run under callgrind with collection toggled on inside the model
# itself, so the count is the instruction's own work and nothing else; if it
# differs between two operand values, the instruction's latency depends on its
# data and it leaks that data through timing.
SECURITY_CT_VECTORS = int(os.environ.get("RED_SECURITY_CT_VECTORS", "6"))
# A coprocessor instruction is not interruptible: the core is stalled for its
# whole latency, so its worst-case cycle count is a lower bound on the machine's
# interrupt latency. Past this, the extension is a denial-of-service risk for
# anything real-time on the same core.
SECURITY_MAX_STALL_CYCLES = float(os.environ.get("RED_SECURITY_MAX_STALL", "4096"))
# How a proven data-dependent latency is treated. RED mines cryptographic
# kernels, where the operands an instruction moves ARE the secret, so a
# variable-latency instruction is a leak by default — and on an in-order core it
# is a scheduling hazard even when the data is public. Set RED_SECURITY_CT to
# "major" for a workload where the operands are genuinely not secret.
SECURITY_CT_SEVERITY = os.environ.get("RED_SECURITY_CT", "blocking")
# security finding -> designer revision -> re-check, capped.
SECURITY_MAX_ROUNDS = int(os.environ.get("RED_SECURITY_ROUNDS", "2"))
SECURITY_MAX_FINDINGS = 12       # per agent reply; keeps a runaway reply bounded
# The agent may ask for at most this many vectors to be executed per turn.
SECURITY_MAX_PROBES = int(os.environ.get("RED_SECURITY_PROBES", "24"))

# ---------------------------------------------------------------------------
# Dispatch watchdog (red.loop._await)
# ---------------------------------------------------------------------------
#
# A plain ray.get() on a task whose resource has left the cluster waits
# forever. When the workers' reverse SSH tunnels dropped mid-run, the driver
# sat on one unschedulable LLM turn for nine hours while the instances billed.
# So the driver polls instead: every STAGE_POLL_SECONDS it re-checks that the
# resource the task needs is still in the cluster, which distinguishes a slow
# stage (callgrind over a whole ECDH exchange takes ~20 min, and must not be
# failed) from a dead one. STAGE_MAX_SECONDS is the backstop for a task that is
# schedulable but wedged.
STAGE_POLL_SECONDS = float(os.environ.get("RED_STAGE_POLL_SECONDS", "120"))
STAGE_MAX_SECONDS = float(os.environ.get("RED_STAGE_MAX_SECONDS", "5400"))

# ---------------------------------------------------------------------------
# Toolchain probing
# ---------------------------------------------------------------------------

# Binaries RED expects on PATH per node group. The nodes probe them and degrade
# to a typed "not available" result rather than crashing. See red.nodes.
#   profile — A1 builds and profiles the target project's own harness
#   cmodel  — link 1 compiles and stress-runs each instruction's C model
#   spike   — Gate 2's C-model <=> patched-Spike differential run
EXPECTED_TOOLS = {
    RES_PROFILE: ["cc", "perf", "valgrind"],
    "cmodel": ["cc"],
    RES_SPIKE: ["spike", "riscv64-unknown-elf-gcc", "dtc"],
}

# ---------------------------------------------------------------------------
# Gate 2's patched-Spike toolchain
# ---------------------------------------------------------------------------
#
# scripts/setup_spike.sh installs into SPIKE_PREFIX, which defaults to the
# user's ~/.local — an ordinary prefix that is already on PATH, so `spike` and
# `dtc` are normal commands rather than something only RED knows how to find.
# (A system-wide /usr/local install works too: set RED_SPIKE_PREFIX=/usr/local.)
#
# Gate 2 also needs spike's *source* headers: `make install` ships some headers
# but not decode_macros.h, and config.h/insn_list.h are generated into the build
# directory. setup_spike.sh keeps a pruned copy of both under
# share/riscv-isa-sim/ (~11 MB — the objects and libraries are thrown away).
SPIKE_PREFIX = os.environ.get(
    "RED_SPIKE_PREFIX", os.path.join(os.path.expanduser("~"), ".local"))
_SPIKE_SHARE = os.path.join(SPIKE_PREFIX, "share", "riscv-isa-sim")
SPIKE_BIN = os.environ.get("RED_SPIKE", os.path.join(SPIKE_PREFIX, "bin", "spike"))
SPIKE_SRC = os.environ.get("RED_SPIKE_SRC", os.path.join(_SPIKE_SHARE, "src"))
SPIKE_BUILD = os.environ.get("RED_SPIKE_BUILD", os.path.join(_SPIKE_SHARE, "build"))
RISCV_GCC = os.environ.get("RED_RISCV_GCC", "riscv64-unknown-elf-gcc")

# ---------------------------------------------------------------------------
# LLM backend — Google Gemini on Vertex AI (GCP)
# ---------------------------------------------------------------------------
#
# RED is model-agnostic in design; this deployment runs the Gemini model named
# by LLM_MODEL below on Vertex AI, billed to the GCP credit. Auth is Application Default
# Credentials (ADC) — set up once on the host that runs the LLM node:
#
#     gcloud auth application-default login
#     gcloud auth application-default set-quota-project <GCP_PROJECT>
#     gcloud services enable compute.googleapis.com --project <GCP_PROJECT>
#
# See the GCP guide PDF and chia/models/vertex.py (VertexGeminiLLM). The
# Vertex backend reads GOOGLE_CLOUD_PROJECT / GOOGLE_CLOUD_LOCATION if
# project/location aren't passed; we pass them explicitly from here.
LLM_BACKEND = "gemini"                        # "gemini" (Vertex AI) — the chosen backend
LLM_MODEL = os.environ.get("RED_LLM_MODEL", "gemini-3.1-pro-preview")
GCP_PROJECT = os.environ.get("RED_GCP_PROJECT", "project-160a0199-6b4a-464c-86a")
# Preview Gemini models are served from the "global" Vertex endpoint, not a
# regional one — a region here 404s the model.
GCP_LOCATION = os.environ.get("RED_GCP_LOCATION", "global")
LLM_RESOURCE = float(os.environ.get("RED_LLM_RESOURCE", "1.0"))   # "llm" token share
LLM_TIMEOUT_SECONDS = int(os.environ.get("RED_LLM_TIMEOUT_SECONDS", "1800"))
# Output budget per turn, passed to the backend in red.loop. The backend's
# 16k default truncates a spec carrying a C model (and a Spike model) for
# several instructions, and a truncated reply arrives as MaxOutputTokensError
# rather than as text -- a failed turn, not a short one.
#
# 64k, not 32k, because the constant-time rewrites Gate 4 asks for make the
# models materially longer: a branch-free modular reduction carries its own
# is_zero/is_greater helpers, and the ISS agent has to emit a Spike model for
# every instruction in one reply.
LLM_MAX_TOKENS = int(os.environ.get("RED_LLM_MAX_TOKENS", "64000"))
LLM_PROMPTS_DIR = os.path.join(REPO_ROOT, "red", "prompts")

# ---------------------------------------------------------------------------
# Neo4j — the persistent design-knowledge graph (red/graph.py)
# ---------------------------------------------------------------------------
#
# Runs on the HEAD, not on a cluster worker: `red_env.sh up`/`down` creates and
# destroys the GCP nodes, so a graph living on one would be erased on every
# teardown. The head's disk outlives any cluster, and tunnelled workers reach it
# over bolt. Install and run it with scripts/setup_neo4j.sh (a userland install
# under ~/.local — this host has neither passwordless sudo nor a docker group).
#
# The data directory is deliberately OUTSIDE the repo's output/, which the
# operator is told to delete between test batches: the whole value of the graph
# is that it remembers earlier runs.
NEO4J_ENABLED = os.environ.get("RED_NEO4J_ENABLED", "1") not in ("0", "false", "")
NEO4J_URI = os.environ.get("RED_NEO4J_URI", "bolt://127.0.0.1:7687")
NEO4J_USER = os.environ.get("RED_NEO4J_USER", "neo4j")
NEO4J_PASSWORD = os.environ.get("RED_NEO4J_PASSWORD", "redgraph1")
NEO4J_DATABASE = os.environ.get("RED_NEO4J_DATABASE", "neo4j")

# ---------------------------------------------------------------------------
# Driver scratch + durable DB
# ---------------------------------------------------------------------------

# Everything a run produces lands under <repo>/output/ (git-ignored): the
# driver's scratch for the in-flight run, and the database node's durable
# store. The repo is rsynced to the same absolute path on every worker, so the
# database node writes to <repo>/output/db on its own disk.
OUTPUT_ROOT = os.environ.get("RED_OUTPUT_ROOT", os.path.join(REPO_ROOT, "output"))
RED_LOG_ROOT = os.path.join(OUTPUT_ROOT, "work")         # driver-side scratch
DB_ROOT = os.environ.get("RED_DB_ROOT", os.path.join(OUTPUT_ROOT, "db"))

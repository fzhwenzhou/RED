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

# The bundled worked example (defaults only — every path below is an INPUT).
EXAMPLE_PROJECT = os.path.join(TARGET_PROJECTS, "micro-ecc")
EXAMPLE_CORE = os.path.join(TARGET_CPUS, "picorv32")
EXAMPLE_WORKLOAD = "ECDH-secp256r1"


def find_sources(root: str, exts=(".c", ".h", ".S", ".v")):
    """Yield (repo-relative path, absolute path) for source files under *root*.

    The A1 mining agent and the A3 RTL node both locate the project/core's
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
RES_HEAD_LOCAL = "head_local"    # read-only sealed tools + driver-side gates
RES_RTL = "rtl"                  # core build + iverilog/verilator sim (A3)
RES_SPIKE = "spike"              # patched Spike ISS (Gate 2, links 2 & 3)
RES_LLVM = "llvm"                # LLVM opt pass (A4)
RES_PROFILE = "profile"          # native harness build + perf/callgrind (A1)
RES_VLSI = "VLSI"                # synthesis (optional, off by default)

# ---------------------------------------------------------------------------
# Gates & budgets (RED design §2)
# ---------------------------------------------------------------------------

GATE1_TOLERANCE = 0.05           # independent re-profiles must agree within ±5%
COVERAGE_TARGET = 0.80           # ranked loops must cover >=80% of cycles
GATE2_VECTORS = 100_000          # C model <=> patched Spike agreement (10^5)
CRITIC_MAX_ROUNDS = 8            # S2c reject/revise cap, then escalation
MAX_REMINE_ROUNDS = 3            # speedup < target -> re-mine, budgeted
SPEEDUP_TARGET = 1.15            # >=15% speedup
INSTR_REDUCTION_TARGET = 0.15    # >=15% dynamic instruction-count reduction

# ---------------------------------------------------------------------------
# Toolchain probing (which links can actually run on this host)
# ---------------------------------------------------------------------------

# Binaries RED expects on PATH for each node. The loop probes them at startup
# and degrades gracefully (a missing tool yields a "not available" result
# rather than a crash). See red.nodes._toolchain.
EXPECTED_TOOLS = {
    RES_RTL: ["iverilog", "vvp", "verilator"],
    RES_SPIKE: ["spike"],
    RES_LLVM: ["clang", "llvm-config", "llc", "opt"],
    RES_PROFILE: ["perf", "valgrind"],
    # riscv64-unknown-elf-gcc cross-compiles rv32 with -march=rv32i -mabi=ilp32.
    "riscv_gcc": ["riscv32-unknown-elf-gcc", "riscv64-unknown-elf-gcc"],
}

# ---------------------------------------------------------------------------
# LLM backend — Google Gemini on Vertex AI (GCP)
# ---------------------------------------------------------------------------
#
# RED is model-agnostic in design; this deployment runs Gemini 2.5 Pro on
# Vertex AI, billed to the GCP free-trial credit. Auth is Application Default
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
LLM_MODEL = os.environ.get("RED_LLM_MODEL", "gemini-2.5-pro")
GCP_PROJECT = os.environ.get("RED_GCP_PROJECT", "project-160a0199-6b4a-464c-86a")
GCP_LOCATION = os.environ.get("RED_GCP_LOCATION", "us-central1")
LLM_RESOURCE = float(os.environ.get("RED_LLM_RESOURCE", "1.0"))   # "llm" token share
LLM_TIMEOUT_SECONDS = int(os.environ.get("RED_LLM_TIMEOUT_SECONDS", "1800"))
LLM_PROMPTS_DIR = os.path.join(REPO_ROOT, "red", "prompts")

# ---------------------------------------------------------------------------
# Driver scratch + durable DB
# ---------------------------------------------------------------------------

RED_LOG_ROOT = "/tmp/red"                                           # process-wide chdir target
DB_ROOT = os.environ.get("RED_DB_ROOT", "/tmp/red-db")             # database node disk
MAX_ITERATIONS = 20               # outer repair-iteration cap

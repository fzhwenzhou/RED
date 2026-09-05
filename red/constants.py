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


def find_sources(root: str, exts=(".c", ".h", ".S", ".v")):
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
CRITIC_MAX_ROUNDS = 8            # S2c reject/revise cap, then escalation
REPAIR_MAX_ROUNDS = 3            # link-1 failure -> agentic repair, then escalation

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
LLM_PROMPTS_DIR = os.path.join(REPO_ROOT, "red", "prompts")

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

# RED — an Agentic RISC-V ISA Extension Designer

RED is an agent loop that, given **one application project** and **one processor
core**, profiles the project, mines its hot loops, and designs a custom
RISC-V instruction extension for them — then verifies the result across a
four-link chain (kernel ⇔ C model ⇔ Spike ⇔ RTL ⇔ end-to-end).

RED runs on [CHIA](https://github.com/ucb-bar/chia) (UC Berkeley's agentic
hardware design framework). Its **LLM backend is Google Gemini on Vertex AI**,
billed to GCP free-trial credit.

## What is agentic vs. fixed

| Stage | What it does | Kind |
|-------|--------------|------|
| **A1 — Kernel mining** | Profile the project, rank hot loops to ≥80% cycle coverage, bounded by Gate 1 (±5% re-profile reproducibility). | **Agentic** (research contribution) |
| **A2 — ISA synthesis** | S2a pattern analyzer → S2b instruction designer → S2c spec critic (≤8 rounds), bounded by Gate 2 (C ⇔ Spike, 10⁵ vectors) + links 1–2. | **Agentic** (research contribution) |
| **A3 — RTL** | Wire the extension into the core's PCPI interface and simulate. | Fixed tool node |
| **A4 — Compiler** | LLVM opt pass lowers hot kernels to the new instructions. | Fixed tool node |
| **A5 — E2E** | Compile, run, measure speedup on the extended core. | Fixed tool node |

A1 and A2 are the research contribution; A3–A5 are bounded, fixed tool nodes.

## Layout

```
red/
  constants.py    general-purpose config (resources, gates, Gemini/GCP backend)
  state_def.py    typed artifacts: HotLoopReport, ISASpec, GateResult, …
  db_node.py      durable artifact store (database node)
  tools.py        sealed MCP tools the agents use (source/profile/report/spec/…)
  nodes.py        programmatic evaluation edges (gates, links, A3–A5)
  loop.py         the orchestration driver (A1 + Gate 1 → A2 + Gate 2 + links)
  prompts/        agent charters (mine.md, system.md, debug.md)
target_project/   one example project (micro-ecc)  — an INPUT, not hard-coded
target_cpu/       one example core (PicoRV32)      — an INPUT, not hard-coded
cluster.yaml      GCP cluster definition
tests/test_env.py environment verification script
```

RED is **general-purpose**: nothing about any workload, core, or hot function is
hard-coded. The agents discover hot loops and design instructions from the
project's own source and measured profiles. The bundled `micro-ecc` /
`PicoRV32` pair is just a worked example — defaults you can override:

```bash
python -m red.loop --project target_project/micro-ecc \
                   --core target_cpu/picorv32 \
                   --workload ECDH-secp256r1 \
                   --cflags "-DuECC_VLI_N_BYTES=32 -DuECC_CURVE=uECC_secp256r1 -DuECC_WORD_SIZE=8"
```

## Setup

Everything is automated by **`scripts/red_env.sh`** (idempotent — safe to re-run):

```bash
bash scripts/red_env.sh setup    # one-time: sshd on the head, venv, GCP auth+APIs,
                                 #           self-test, then brings the cluster up
bash scripts/red_env.sh up       # daily: (re)bring up the cluster
bash scripts/red_env.sh down     # tear down + verify nothing is left billing
bash scripts/red_env.sh status   # ray + GCP state at a glance
```

`setup` asks for interactive input only where unavoidable: your sudo password
(to install `openssh-server` on the head) and one browser login
(`gcloud auth application-default login`) if ADC is missing. After that,
`up`/`down` are fully automatic.

What it does for you:

- **Head (this machine)**: generates the SSH keypair if missing, authorizes it
  for self-SSH, installs + starts `sshd`, creates `.venv` with `chia` and `red`
  installed editable, and resolves `HEAD_IP` to the machine's own address
  (WSL: it changes on every reboot — the script re-resolves it each run).
- **GCP**: sets the ADC quota project, enables the Compute + Vertex AI APIs,
  and discovers the project's default compute service account.
- **Cluster**: runs `chia up`. The `llm` node is created with that service
  account attached, so the Gemini backend authenticates through the GCE
  metadata server — **no `gcloud` login on any worker is ever needed**.
  Workers rsync this repo to the same path and `pip install -e` it, so
  `import red` works on every node.
- **`down`**: `chia down` (terminates all instances, removes the firewall
  rules, stops local ray), then verifies via the Compute API that **0
  instances, 0 disks, and 0 static addresses remain** — nothing keeps billing.

Manual equivalent of the GCP pieces (what the script runs for you):

```bash
gcloud auth application-default login
gcloud auth application-default set-quota-project project-160a0199-6b4a-464c-86a
```

(Project/location/model are env-overridable: `RED_GCP_PROJECT`, `RED_GCP_LOCATION`, `RED_LLM_MODEL`.)

## Run

```bash
# Smoke test the loop on one machine (no cluster, no LLM — mechanical A1 + Gate 1 only)
python -m red.loop --local

# Full loop on the cluster (Gemini backend)
python -m red.loop
```

## Verify the environment

```bash
python tests/test_env.py     # or: pytest -q tests/test_env.py
```

The script probes the toolchain, round-trips every typed artifact, runs the
mechanical nodes (gates, links, A3–A5) directly, and exercises the sealed MCP
tools — confirming the environment matches the design's requirements.

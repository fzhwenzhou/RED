# Running RED

RED mines an application's hot loops (A1) and designs a custom RISC-V
instruction extension for them (A2). The output is `ISASpec.json`.

There are three ways to run it, in increasing order of cost:

| Mode | Command | LLM | GCP VMs | Use it for |
|------|---------|-----|---------|------------|
| Mechanical smoke test | `python -m red.loop --local` | none | none | Checking A1 + Gate 1 work at all |
| Single-machine full loop | `python -m red.loop --local-llm` | Gemini (API only) | none | Iterating on A2 + Gate 2 cheaply |
| Cluster run | `python -m red.loop` | Gemini on the `llm` node | 3 | The real distributed CHIA loop |

---

## 0. Prerequisites (one time)

You need: this repo, a CHIA checkout at `~/chia`, Python 3.12, a GCP project
with billing, and the `gcloud` CLI.

```bash
sudo apt-get install -y build-essential valgrind        # A1 profiling
python3 -m venv .venv && source .venv/bin/activate
pip install -e ~/chia          # CHIA framework
pip install -e .               # RED
gcloud auth application-default login
gcloud auth application-default set-quota-project <YOUR_PROJECT>
```

Point RED at your own project/model if they differ from the defaults
(`red/constants.py`):

```bash
export RED_GCP_PROJECT=<YOUR_PROJECT>
export RED_GCP_LOCATION=global            # preview models are served globally
export RED_LLM_MODEL=gemini-3.1-pro-preview
```

Verify the environment — 19 checks covering imports, the toolchain, artifact
round-trips, every mechanical gate (including that link 1 *rejects* a bad C
model), the durable store, and the sealed MCP tools:

```bash
python tests/test_env.py
```

`cc` and `valgrind` + `callgrind_annotate` must be present, or A1 silently
falls back to a much weaker static estimate — the test fails loudly if so.

---

## 1. Mechanical smoke test (free, ~90 s)

```bash
python -m red.loop --local
```

Builds the workload harness, profiles it twice under callgrind, runs Gate 1,
and writes a `HotLoopReport`. No LLM, so A2 stops with "no ISASpec produced" —
that is the expected outcome for this mode. Expect Gate 1 to pass at ~0.00%
drift and coverage around 99%.

---

## 2. Single-machine full loop (Gemini API cost only, ~5–15 min)

```bash
python -m red.loop --local-llm
```

Runs the complete A1 + A2 loop — mining, synthesis, link 1, the ISS
implementer and Gate 2 — on one machine with the real Gemini backend:
Ray starts locally advertising every cluster resource tag, the seven sealed MCP
tool servers come up on localhost, and the agents drive them. This exercises
exactly the same code path as the cluster run — only the placement differs.
Use it to iterate on prompts without paying for VMs.

---

## 3. Cluster run

### 3.0 Paths in `cluster.yaml`

`file_mounts` rsyncs two directories to every worker at the **same absolute
path** they have on the head:

```yaml
file_mounts:
    /home/naonao/RED:  /home/naonao/RED/     # this repo
    /home/naonao/chia: /home/naonao/chia/    # the head's CHIA checkout
```

Both are absolute and username-specific — change them if your home directory
differs. Mounting CHIA matters: chia's default worker setup clones *upstream*
chia, which may pin a different Ray than your local checkout, and a head/worker
Ray skew makes `ray start` fail on the worker with
`FileNotFoundError: [Errno 2] No such file or directory: ''`. Each worker
reinstalls both mounts (`pip install -e $HOME/chia && pip install -e $HOME/RED`)
so head and workers run one CHIA and one Ray.

### 3.1 Bring the cluster up

```bash
bash scripts/red_env.sh setup     # first time: sshd, venv, GCP auth+APIs+IAM, self-test, up
bash scripts/red_env.sh up        # thereafter
```

`setup` is idempotent and asks for input only where unavoidable: your sudo
password (to install `openssh-server` on the head) and one browser login if ADC
is missing. It then:

- generates an SSH keypair if needed and authorizes the head to SSH to itself
  (CHIA brings the head up over SSH);
- creates `.venv`, installs `chia` + `red` editable;
- enables the Compute + Vertex AI APIs and grants the default compute service
  account `roles/aiplatform.user` — without that role every LLM turn 403s;
- resolves `HEAD_IP` fresh (WSL changes it on every reboot);
- runs `chia up`, which provisions **3 GCP VMs** (`llm`, `database`, `profile`)
  and joins them to the local Ray head.

The `llm` node is created with the default compute service account attached, so
Gemini authenticates through the GCE metadata server — no `gcloud` login on any
worker.

CHIA will warn that your GCP network has a world-open `default-allow-ssh` rule
(tcp:22 from `0.0.0.0/0`). That rule is GCP's default-network default, not
something RED or CHIA adds; CHIA's own firewall rule is narrowed to your egress
IP. Set `CHIA_GCP_LOCKDOWN_DEFAULT_SSH=1` before `up` if you want CHIA to delete
the world-open rule.

**Keep the terminal you run `up` in.** CHIA starts the workers' reverse SSH
tunnels as plain child processes (`subprocess.Popen`, same process group), so
closing that shell — or letting a job-control cleanup reap the group — kills the
tunnels, the workers silently leave the cluster, and the next run hangs forever
on `Pending Demands: {'database': 0.9}`. For an unattended bring-up, detach it:

```bash
setsid bash scripts/red_env.sh up < /dev/null > up.log 2>&1 &
```

Check state at any time:

```bash
bash scripts/red_env.sh status    # ray nodes + GCP instances/disks/addresses
```

### 3.2 Run the loop

```bash
python -m red.loop
```

Options (all optional — the defaults are the bundled micro-ecc example):

```
--project PATH     application project directory
--core PATH        processor core directory (context for the encoding space)
--workload NAME    workload label
--harness FILE     project-relative .c holding the workload's main()
--cflags "..."     compile flags that pin the workload
```

### 3.3 Tear down — always do this

```bash
bash scripts/red_env.sh down
```

Runs `chia down`, force-deletes any cluster-labeled leftovers directly through
the Compute API (in case `chia down` dies midway), stops local Ray, and then
**verifies** 0 instances, 0 disks and 0 static addresses remain. It exits
non-zero if anything is still billing. Confirm independently with:

```bash
gcloud compute instances list --project <YOUR_PROJECT>
```

---

## 4. Reading the results

Everything lands in `output/db/<workload>/run_<N>/` (git-ignored). On a cluster
run the store physically lives on the `database` VM, so the driver tars the run
and unpacks it on the head when the loop finishes — the artifacts survive
`red_env.sh down`. If that fetch fails the driver says so and names the path on
the node.

| File | What it is |
|------|-----------|
| `profile.json` | A1's mechanical profile: per-function instruction counts, method, harness used |
| `HotLoopReport.json` | A1's deliverable: ranked hot loops, cycle share, coverage, Gate-1 status |
| `ISASpec.draft.json` | A2's spec as the agent wrote it, before verification |
| `ISASpec.json` | **RED's deliverable**: the verified spec (encodings, semantics, pseudocode, both executable models, link-1 + Gate-2 verdicts) |
| `gate2/` | Everything Gate 2 built: the generated Spike extension, the rv32 test program, the signature dumps |
| `gates/gate2.json`, `gates/chain.json` | The verification edges' verdicts |
| `summary.md` | Human-readable run summary |

`output/work/<workload>/<run-id>/` holds the driver-side scratch: the LLM
transcripts under `llm_logs/`, link 1's generated C harnesses under `link1/`,
and the Ray profiler trace. On a cluster run the built workload harness and
`callgrind.out` live on the **profile node** under the same path (A1 runs
there); only the resulting `profile.json` comes back to the head. In
`--local`/`--local-llm` runs everything is on one machine.

The loop exits 0 only when Gate 1, link 1 **and** Gate 2 all pass. Gate 2
passing means two independently written models of each instruction agreed on
100,000 vectors with the instruction really executed by Spike — it does *not*
mean the instruction is the right one for the workload. See LIMITATIONS in
`README.md` before quoting an ISASpec as verified.

---

## 4a. What a good run looks like

Run end to end against `gemini-3.1-pro-preview` on the micro-ecc
ECDH-secp256r1 workload, single machine (`--local-llm`):

```
- result: **converged (A1+A2)**
- A2 rounds: 1
- Gate 1 (re-profile ±5%): PASS runs 8882695006 vs 8882810979 cycles (0.00% vs ±5%)
- Gate 2 (C model vs Spike): PASS 2/2 instructions agree on 100,000 random + corner vectors
- coverage: 91.58% (80% target)
- PASS link1: 2/2 C models built and survived 100,000 vectors under ASan+UBSan
- PASS link2: 2/2 instructions: C model and patched Spike agree
- ISASpec: RED_ECC_SECP256R1 (2 instructions)
    vli256_mul      custom-0, 16x32b — 256x256 -> 512-bit multiply
    secp256r1_mmod  custom-0, 16x32b — reduce 512 bits mod the secp256r1 prime
```

Three agent turns total (`llm_logs/transcript.log` records each): **A1 mining**,
**A2 round 0**, **ISS implementer**. A1 mined the two kernels that actually
dominate ECDH — `uECC_vli_mult` (~43%) and `vli_mmod_fast_secp256r1` (~49%) —
and A2 designed one instruction for each. Wall clock ~25 min, nearly all of it
model latency; the mechanical gates are seconds (Gate 2 over 10⁵ vectors takes
about 3 s per instruction).

You can confirm Gate 2 really used the simulator by disassembling the harness it
built and left behind:

```bash
riscv64-unknown-elf-objdump -d output/work/*/*/gate2/*/rv32_blocks.elf | grep insn
#   800000bc:  01cf878b  .insn 4, 0x01cf878b
```

`0x01cf878b` is opcode `0x0b` (custom-0), funct3 `000`, funct7 `0000000` — the
encoding from the ISASpec. objdump prints `.insn` rather than a mnemonic because
the instruction does not exist in the base ISA; Spike ran it through the
extension RED generated.

One honest observation from that run: the ISS agent, which never saw the C
model, wrote schoolbook long multiplication identical to the designer's. See
LIMITATIONS point 2 in `README.md` for what that does and does not buy you.

---

## 5. Troubleshooting

| Symptom | Cause / fix |
|---------|-------------|
| `test_a1_profile_workload` fails with `profile_method='static-asm'` | `valgrind`/`callgrind_annotate` missing → `sudo apt-get install valgrind` |
| Gate 1 fails with "no dynamic cycle counts" | The harness did not build; check `build_log` in `profile.json` and your `--cflags` |
| Every LLM turn fails with 403 | The compute SA lacks `roles/aiplatform.user` → `python scripts/gcp_util.py ensure-iam` |
| Model 404s | A preview model needs `RED_GCP_LOCATION=global` |
| `A2: no ISASpec produced` in `--local`/`--local-llm` | Expected in `--local`. In `--local-llm`, check `output/work/.../llm_logs/` for the transcript |
| link 1 fails with a sanitizer report | Working as intended — the agent's C model has a real bug; the counterexample is in `gates/chain.json` |
| Gate 2 says "needs a patched-Spike toolchain" | `bash scripts/setup_spike.sh`; check with its `check` subcommand |
| Gate 2 fails with "spike extension does not compile" | The ISS agent's C++ is bad; the compiler diagnostic is in the counterexample, and the repair edge feeds it back automatically |
| Gate 2 reports an encoding collision | Two instructions claim the same (opcode, funct3, funct7); the counterexample names them |
| Tool servers fail to bind | A stale run holds ports 8000+; `ray stop` and retry |
| `rsync: command not found` during bring-up | The worker image has no rsync and CHIA needs it to sync the repo. `cluster.yaml` installs it in each `gcp_nodes.*.setup_commands` (which run before the sync); if you add a node type, copy that block into it |
| Every agent turn fails with `LLM turn FAILED (returncode=-1)` / `unhandled errors in a TaskGroup` | The llm node could not reach the head's MCP tools. RED wraps them in `RelayTool` (red/tools.py) so the URL resolves to `CHIA_TOOL_RELAY_HOST` on the worker — CHIA's own backends never apply that rewrite. Check the head tool ports are inside `tunnel_defaults.head_tool_port_max` and forwarded: on the worker, `ss -lnt \| grep 127.200.0.1` should list them |
| The loop hangs; `ray status` shows `Pending Demands: {'database': 0.9}` | The workers' SSH tunnels died (usually the shell that ran `up` was closed). Check with `pgrep -f 'ssh.*-R'`; re-establish with `bash scripts/red_env.sh up` |
| Worker `ray start` dies with `No such file or directory: ''` | Head/worker Ray version skew. Confirm with `python -c "import ray;print(ray.__version__)"` on both; the `file_mounts` CHIA mount plus the worker reinstall is what keeps them equal |
| `409 ... instance already exists` on `up` | Instances from a previous run are still there. `red_env.sh up` detects this and switches to `chia up --add`; a stuck cluster can always be reset with `red_env.sh down` |
| `chia up` reports fewer than 4 nodes | Check `HEAD_IP` is the LAN address (not the public egress IP) and that self-SSH works |

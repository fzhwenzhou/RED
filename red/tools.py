"""Sealed MCP tools for the RED loop.

The destination paths are baked in at construction — the agents only ever see
the payload, never a path they could redirect.

**Two tool servers, not ten.** Every ChiaTool is a separate Ray actor, and each
actor rescans the tool port range from its base port when it binds, so a tool
per artifact cost minutes of startup before the first agent turn. The methods
are grouped onto the two servers whose separation actually matters:

``DesignerTool``   the A1 mining / A2 synthesis agent's whole surface —
                   sources, profile, HotLoopReport, ISASpec, status, notes,
                   finish. Its ``write_spec`` drops any ``spike_model``.
``IssTool``        the ISS implementer's surface — the spec with the reference
                   C models **withheld**, and a ``write_spec`` that accepts
                   only ``spike_model``.

That boundary is what makes Gate 2 mean anything: the two executable models are
written by agents that cannot see each other's work. Everything else was just
extra processes.

Both validate every write against the artifact schema before accepting, so a
malformed artifact is rejected back to the agent rather than reaching a gate.
"""

from __future__ import annotations

import json
import os

from chia.base.tools.ChiaTool import ChiaTool

from red import graph, iss, state_def
from red.constants import REPO_ROOT, find_sources


class _SourceMixin:
    """The agents' read-only view of the project and core sources.

    Paths are resolved against ``source_roots`` -- the absolute project and core
    directories the driver passes in -- and NOT against a repo root computed
    from this module's own location. A ChiaTool is a Ray actor that can be
    scheduled on any node, where ``red`` may be installed somewhere unrelated to
    the sources; deriving paths from the install location made ``list_sources``
    emit things like ``../../../../../../home/naonao/RED/target_project/...``
    and made ``read_source`` reject every one of them. Both agents then designed
    blind: 28-32 failed reads per run, every run, with the designer guessing
    ``uECC.c``, ``/home/naonao/RED/...``, ``~naonao/...`` and never once opening
    the file it was writing a C model for.

    Resolution is index-based, which is also why it is safe: only files found by
    walking ``source_roots`` can ever be opened, so no path a caller supplies
    can escape anywhere -- there is nothing to escape *to*.
    """

    def _source_index(self) -> dict:
        """{display path: absolute path} for every source under the roots.

        Keyed relative to each root's PARENT, so the project and core names stay
        in the path (``micro-ecc/uECC.c``, ``picorv32/picorv32.v``) and two roots
        cannot collide on a bare filename.
        """
        index: dict = {}
        for root in self.source_roots:
            root = os.path.abspath(root)
            base = os.path.dirname(root)
            for _rel, full in find_sources(root):
                index[os.path.relpath(full, base)] = full
        return index

    def list_sources(self) -> str:
        """List the project + core source files (C/H/asm/Verilog). Use one of
        these paths verbatim with `read_source`."""
        index = self._source_index()
        return "\n".join(f"- {p}" for p in sorted(index))

    def read_source(self, rel_path: str) -> str:
        """Return the contents of one source file listed by `list_sources`.

        A path is accepted if it is one of the listed paths, or unambiguously
        identifies one by its trailing components -- so ``uECC.c``,
        ``micro-ecc/uECC.c`` and an absolute path ending in either all resolve
        to the same file. Guessing the prefix is the one thing an agent cannot
        be expected to get right, and it is the one thing that does not matter.
        """
        index = self._source_index()
        want = (rel_path or "").strip().strip('"\'').replace("\\", "/")
        if want in index:
            with open(index[want], "r", errors="replace") as f:
                return f.read()
        # Suffix match on whole path components, longest suffix first: the
        # trailing components are the part an agent knows, the prefix is the
        # part it cannot know, so a junk prefix must not defeat the lookup.
        parts = [p for p in want.split("/") if p not in ("", ".", "..")]
        hits: list = []
        for n in range(len(parts), 0, -1):
            tail = parts[-n:]
            hits = [(key, full) for key, full in index.items()
                    if key.split("/")[-n:] == tail
                    or full.split("/")[-n:] == tail]
            if hits:
                break
        if len(hits) == 1:
            with open(hits[0][1], "r", errors="replace") as f:
                return f.read()
        if len(hits) > 1:
            return ("Error: ambiguous path {!r} — matches {}. Use one of them "
                    "exactly.".format(rel_path, ", ".join(sorted(k for k, _ in hits))))
        # Nothing matched: name the plausible files rather than just refusing,
        # so the agent's next call succeeds instead of starting another guess.
        stem = os.path.basename(want)
        near = sorted(k for k in index if os.path.basename(k) == stem) or \
            sorted(k for k in index if stem and stem in k)
        msg = f"Error: {rel_path!r} is not a listed source file."
        if near:
            msg += " Did you mean: " + ", ".join(near[:8]) + "?"
        else:
            msg += (" Call list_sources for the exact paths (there are "
                    f"{len(index)}).")
        return msg


class _ProfileMixin:

    def read_profile(self) -> str:
        """Return the current mechanical profile (method, per-function counts)."""
        if not os.path.exists(self.profile_path):
            return "No profile has been produced yet."
        with open(self.profile_path) as f:
            return f.read()


class _ReportMixin:

    def read_report(self) -> str:
        """Return the current HotLoopReport JSON (mined loops, cycle share,
        coverage, Gate-1 status)."""
        if not os.path.exists(self.report_path):
            return "No HotLoopReport yet."
        with open(self.report_path) as f:
            return f.read()


    def report_frozen(self) -> bool:
        """True once Gate 1 has accepted A1's report. See :meth:`write_report`."""
        return os.path.exists(getattr(self, "report_path", "") + ".frozen")

    def write_report(self, report_json: str) -> str:
        """Record your ranked loop selection. `report_json` must be a valid
        HotLoopReport (workload + loops[] with loop_id/function); it is
        validated and rejected if not. Dynamic cycle numbers are the loop's to
        fill in from the profile."""
        # A1 owns the report; once Gate 1 has accepted it, it is the measured
        # evidence every later stage is judged against and it stops being
        # writable. The designer used to be able to rewrite it -- and did,
        # inventing call counts to make its own instructions look profitable --
        # while Gate 3 went on scoring against the in-memory original. Two
        # sources of truth, and the run could not converge because the feedback
        # the designer acted on was its own edit.
        if self.report_frozen():
            return ("Rejected: the HotLoopReport is A1's measured evidence and is "
                    "frozen after Gate 1. Design against it as it stands — if a "
                    "loop's numbers look wrong, say so in your rationale rather "
                    "than editing the measurement.")
        try:
            report = state_def.from_json(report_json, state_def.HotLoopReport)
            for loop in report.loops:
                # Agents write operand_stats either as prose or as a small
                # object; normalize to text so the artifact is well-typed
                # either way instead of rejecting a useful report on shape.
                if not isinstance(loop.operand_stats, str):
                    loop.operand_stats = json.dumps(loop.operand_stats)
            state_def.validate_hot_loop_report(report)
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            return f"Rejected: invalid HotLoopReport — {e}"
        with open(self.report_path, "w") as f:
            f.write(state_def.to_json(report))
        return f"Accepted HotLoopReport for '{report.workload}' ({len(report.loops)} loops)."


class _SpecMixin:

    def _mined_loop_ids(self) -> set:
        """The loop ids A1 actually mined, for validating `replaces`."""
        path = getattr(self, "report_path", "")
        if not path or not os.path.exists(path):
            return set()
        try:
            with open(path) as f:
                report = state_def.from_json(f.read(), state_def.HotLoopReport)
        except (json.JSONDecodeError, TypeError, ValueError):
            return set()
        return {l.loop_id for l in report.loops} | {l.function for l in report.loops}


    def read_spec(self) -> str:
        """Return the current ISASpec draft JSON."""
        if not os.path.exists(self.spec_path):
            return "No ISASpec draft yet."
        with open(self.spec_path) as f:
            return f.read()


    def write_spec(self, spec_json: str) -> str:
        """Replace the ISASpec draft. `spec_json` must be valid JSON matching the
        ISASpec schema (name, instructions[] with mnemonic/encoding/semantics/
        pseudocode); it is validated and rejected if not."""
        try:
            spec = state_def.from_json(spec_json, state_def.ISASpec)
            for ins in spec.instructions:
                # Agents commonly nest the encoding fields
                # (``"encoding": {"opcode": ..., "funct3": ...}``) instead of
                # filling the flat ones. That is a shape difference, not a design
                # error, so lift it into the schema rather than bouncing the draft.
                if isinstance(ins.encoding, dict):
                    parts = ins.encoding
                    ins.opcode = ins.opcode or str(parts.get("opcode", ""))
                    ins.funct3 = ins.funct3 or str(parts.get("funct3", ""))
                    ins.funct7 = ins.funct7 or str(parts.get("funct7", ""))
                    ins.encoding = str(parts.get("encoding") or
                                       " ".join(f"{k}={v}" for k, v in parts.items()))
                ins.opcode = state_def.normalize_opcode(ins.opcode) or ins.opcode
            state_def.validate_isa_spec(spec)
            # `replaces` decides how much benefit Gate 3 credits, so it has to
            # name a loop that was actually mined. Catching it here turns a
            # silent overcredit into an immediate, specific rejection.
            known = self._mined_loop_ids()
            if known:
                for ins in spec.instructions:
                    if ins.replaces not in known:
                        raise ValueError(
                            f"instruction {ins.name!r} replaces={ins.replaces!r}, "
                            f"which is not a mined loop. Use one of: "
                            + ", ".join(sorted(known)))
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            return f"Rejected: invalid ISASpec — {e}"

        # spike_model belongs to the ISS implementer alone. Gate 2 only means
        # something if the two executable models were written independently, so
        # a designer-side turn cannot supply or edit one here: whatever the ISS
        # agent last wrote is carried over, and anything sent in is dropped.
        existing = {}
        if os.path.exists(self.spec_path):
            try:
                with open(self.spec_path) as f:
                    prior = state_def.from_json(f.read(), state_def.ISASpec)
                existing = {i.mnemonic: i.spike_model for i in prior.instructions}
            except (json.JSONDecodeError, TypeError, ValueError):
                existing = {}
        for ins in spec.instructions:
            ins.spike_model = existing.get(ins.mnemonic, "")

        with open(self.spec_path, "w") as f:
            f.write(state_def.to_json(spec))
        return f"Accepted ISASpec '{spec.name}' ({len(spec.instructions)} instructions)."


class _CoreMixin:

    def read_core(self) -> str:
        """Return the CoreProfile: the target core's ISA, microarchitecture,
        coprocessor interface, existing functional units and cycle costs. Read
        this before designing — it is what makes an instruction implementable on
        *this* machine rather than on an abstract RISC-V."""
        if not os.path.exists(self.core_path):
            return "No CoreProfile yet."
        with open(self.core_path) as f:
            return f.read()


class _StatusMixin:

    def read_status(self) -> str:
        """Return the per-instruction status from the latest evaluation."""
        if not os.path.exists(self.status_path):
            return "No evaluations have run yet."
        with open(self.status_path) as f:
            return f.read()


class _KnowledgeMixin:

    def read_knowledge(self) -> str:
        """Return accumulated notes from earlier iterations."""
        if not os.path.exists(self.knowledge_path):
            return ""
        with open(self.knowledge_path) as f:
            return f.read()


    def append_knowledge(self, note: str) -> str:
        """Append a durable note (a bug's root cause + fix, a spec gotcha)."""
        with open(self.knowledge_path, "a") as f:
            f.write("\n" + note.rstrip() + "\n")
        return "Noted."


class _FinishMixin:

    def finish(self, summary: str) -> str:
        """Call when you believe the ISASpec is complete and every instruction
        survives Gate 2. `summary`: one paragraph on what you designed."""
        os.makedirs(os.path.dirname(self.sentinel_path) or ".", exist_ok=True)
        with open(self.sentinel_path, "w") as f:
            f.write(summary)
        return "Recorded. The loop will re-verify (Gate 2 + chain) before exiting."


    def was_finished(self) -> bool:
        return os.path.exists(self.sentinel_path)


    def reset(self) -> None:
        if os.path.exists(self.sentinel_path):
            os.remove(self.sentinel_path)


class _IssSpecMixin:

    def _load(self):
        with open(self.spec_path) as f:
            return state_def.from_json(f.read(), state_def.ISASpec)


    def read_spec(self) -> str:
        """Return the ISASpec to implement: encodings, operand widths, prose
        semantics and pseudo-code. The reference C models are withheld on
        purpose — implement what the specification says."""
        if not os.path.exists(self.spec_path):
            return "No ISASpec yet."
        spec = self._load()
        for ins in spec.instructions:
            ins.c_model = "(withheld — implement from the specification above)"
        return state_def.to_json(spec)


    def write_spec(self, spec_json: str) -> str:
        """Record your Spike implementations. Send the spec back with a
        `spike_model` on each instruction; only those fields are taken, every
        other field keeps the designer's value."""
        try:
            incoming = state_def.from_json(spec_json, state_def.ISASpec)
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            return f"Rejected: not a valid ISASpec — {e}"
        if not os.path.exists(self.spec_path):
            return "Rejected: there is no ISASpec on disk to add models to."

        spec = self._load()
        supplied = {i.mnemonic: (i.spike_model or "") for i in incoming.instructions}
        missing, wrong = [], []
        for ins in spec.instructions:
            model = supplied.get(ins.mnemonic, "").strip()
            if not model:
                missing.append(ins.mnemonic)
                continue
            want = iss.iss_function(ins.mnemonic)
            if want not in model:
                wrong.append(f"{ins.mnemonic} (expected a function named {want})")
                continue
            ins.spike_model = model
        if missing or wrong:
            parts = []
            if missing:
                parts.append("no spike_model for: " + ", ".join(missing))
            if wrong:
                parts.append("wrong function name in: " + "; ".join(wrong))
            return "Rejected: " + "; ".join(parts)

        with open(self.spec_path, "w") as f:
            f.write(state_def.to_json(spec))
        return (f"Accepted {len(spec.instructions)} Spike model(s). Gate 2 will "
                "run them against the reference C models.")



class _GraphMixin:
    """The agents' window onto RED's persistent knowledge graph.

    Every other tool here shows the agent *this* run. These show it every run
    that came before: what was designed for the same hot loop, what the
    performance gate predicted for it, and what the reviewers refused. Without
    that, run N+1 re-derives run N's lesson from scratch — and the transcripts
    show exactly that happening, with successive runs proposing the same
    per-iteration instruction and being told again that its operand traffic is
    charged on every invocation.

    Read-only by construction: the graph is the record of what happened, and an
    agent that could edit it could rewrite its own history.
    """

    def _graph_workload(self) -> str:
        return getattr(self, "workload", "") or ""

    @staticmethod
    def _table(rows: list, columns: list) -> str:
        """Rows as a compact markdown table — agents read these far better than
        raw JSON, and it keeps long C models out of the reply."""
        if not rows:
            return "(nothing recorded yet)"
        out = ["| " + " | ".join(columns) + " |",
               "|" + "|".join("---" for _ in columns) + "|"]
        for r in rows:
            cells = []
            for c in columns:
                v = r.get(c)
                if isinstance(v, float):
                    v = f"{v:.2f}"
                elif isinstance(v, list):
                    v = "; ".join(str(x) for x in v)
                cells.append(str(v if v is not None else "")
                             .replace("\n", " ").replace("|", "/")[:160])
            out.append("| " + " | ".join(cells) + " |")
        return "\n".join(out)

    def graph_prior_designs(self, function: str) -> str:
        """What earlier RED runs designed for one hot loop, and what it scored.

        `function` is the loop's function name (the `function` field of a mined
        loop, e.g. `uECC_vli_mult`). Returns one row per instruction: its shape,
        the whole-application speedup the performance gate predicted for the run
        that contained it, and whether that run converged.
        """
        if not graph.available():
            return "The knowledge graph is not running; no history is available."
        try:
            rows = graph.prior_designs(self._graph_workload(), function)
        except Exception as e:                             # noqa: BLE001
            return f"Graph query failed: {e}"
        return ("Earlier designs for `" + function + "` "
                "(work_per_word = mac_ops / operand words moved; the quantity "
                "that decides whether an instruction can pay):\n\n"
                + self._table(rows, ["run", "converged", "mnemonic", "words",
                                     "mac_ops", "invocations", "work_per_word",
                                     "app_speedup", "instruction_speedup"]))

    def graph_prior_findings(self, function: str) -> str:
        """Blocking and major review findings raised against earlier
        instructions for this hot loop — the objections already known."""
        if not graph.available():
            return "The knowledge graph is not running; no history is available."
        try:
            rows = graph.prior_findings(self._graph_workload(), function)
        except Exception as e:                             # noqa: BLE001
            return f"Graph query failed: {e}"
        return ("What reviewers previously refused for `" + function + "`:\n\n"
                + self._table(rows, ["severity", "reviewer", "mnemonic",
                                     "words", "mac_ops", "finding", "fix"]))

    def graph_prior_security(self, function: str = "") -> str:
        """Security defects earlier runs had *proven* against instructions for
        this workload — the ones a sanitizer, a simulator or an instruction
        count established, not the ones somebody worried about.

        Pass a loop's `function` name to narrow it to that loop, or omit it for
        everything recorded on this workload. Reading this before you design is
        the cheapest security work available: a defect that blocked an earlier
        design will block yours.
        """
        if not graph.available():
            return "The knowledge graph is not running; no history is available."
        try:
            rows = graph.prior_security(self._graph_workload(), function)
        except Exception as e:                             # noqa: BLE001
            return f"Graph query failed: {e}"
        return ("Security defects already established on this workload "
                "(confidence `confirmed`/`derived` means a machine settled it):"
                "\n\n" + self._table(rows, ["severity", "confidence", "check",
                                             "mnemonic", "words", "finding",
                                             "fix"]))

    def graph_best_design(self) -> str:
        """The best extension recorded for this workload so far: converged runs
        first, then by predicted speedup."""
        if not graph.available():
            return "The knowledge graph is not running; no history is available."
        try:
            rows = graph.best_design(self._graph_workload())
        except Exception as e:                             # noqa: BLE001
            return f"Graph query failed: {e}"
        return ("Best extensions recorded for this workload:\n\n"
                + self._table(rows, ["run", "converged", "predicted_speedup",
                                     "blocking", "spec", "instructions"]))

    def graph_loops(self) -> str:
        """Every hot loop the graph has seen for this workload, how often it was
        mined and how many instructions have targeted it."""
        if not graph.available():
            return "The knowledge graph is not running; no history is available."
        try:
            rows = graph.loop_history(self._graph_workload())
        except Exception as e:                             # noqa: BLE001
            return f"Graph query failed: {e}"
        return ("Hot loops recorded for this workload:\n\n"
                + self._table(rows, ["function", "runs_mined",
                                     "mean_cycle_share", "calls",
                                     "instructions_designed"]))

    def graph_query(self, cypher: str) -> str:
        """Run your own read-only Cypher against the knowledge graph.

        The schema is:
          (:Project)<-[:IN_PROJECT]-(:Workload)-[:HAS_LOOP]->(:HotLoop)
          (:Run)-[:OF_WORKLOAD]->(:Workload), (:Run)-[:TARGETS]->(:Core)
          (:Run)-[:MINED {loop_id, rank, cycle_share, calls, dynamic_cycles}]->(:HotLoop)
          (:Run)-[:PRODUCED {stage}]->(:Spec)-[:HAS_INSTRUCTION]->(:Instruction)
          (:Instruction)-[:REPLACES]->(:HotLoop)
          (:Run)-[:EVALUATED]->(:PerfEstimate)-[:SCORES {speedup, cycles}]->(:Instruction)
          (:Run)-[:REVIEWED {round}]->(:Finding)-[:ABOUT]->(:Instruction)
          (:Run)-[:CHECKED]->(:Gate {gate, passed, detail})
        Run has: run_id, converged, predicted_speedup, blocking_findings,
        coverage, wallclock_seconds, model, status.
        Instruction has: mnemonic, words, mac_ops, invocations, work_per_word,
        replaces, stage, encoding, semantics.
        Writes are refused. A LIMIT is added if you omit one.
        """
        if not graph.available():
            return "The knowledge graph is not running; no history is available."
        try:
            rows = graph.query(cypher)
        except ValueError as e:
            return f"Rejected: {e}"
        except Exception as e:                             # noqa: BLE001
            return f"Graph query failed: {e}"
        if not rows:
            return "(no rows)"
        return self._table(rows, list(rows[0].keys()))


class DesignerTool(_SourceMixin, _ProfileMixin, _CoreMixin, _ReportMixin,
                   _SpecMixin, _StatusMixin, _KnowledgeMixin, _GraphMixin,
                   _FinishMixin, ChiaTool):
    """Everything the mining and synthesis agents touch, on one server."""

    METHODS = ("list_sources", "read_source", "read_profile", "read_core",
               "read_report", "write_report", "read_spec", "write_spec",
               "read_status", "read_knowledge", "append_knowledge",
               "graph_prior_designs", "graph_prior_findings",
               "graph_prior_security", "graph_best_design", "graph_loops",
               "graph_query", "finish")

    def __init__(self, name, source_roots, profile_path, report_path, spec_path,
                 status_path, knowledge_path, sentinel_path, core_path="",
                 workload="", task_options=None):
        super().__init__(name, task_options=task_options)
        self.source_roots = source_roots
        self.profile_path = profile_path
        self.report_path = report_path
        self.spec_path = spec_path
        self.status_path = status_path
        self.knowledge_path = knowledge_path
        self.sentinel_path = sentinel_path
        self.core_path = core_path
        self.workload = workload
        for m in self.METHODS:
            self.mcp.add_tool(getattr(self, m), name=f"{name}_{m}")
        super().__post_init__()


class IssTool(_IssSpecMixin, _StatusMixin, _KnowledgeMixin, ChiaTool):
    """The ISS implementer's surface: the spec without the C models, the last
    verdict, and the shared notes."""

    METHODS = ("read_spec", "write_spec", "read_status",
               "read_knowledge", "append_knowledge")

    def __init__(self, name, spec_path, status_path, knowledge_path,
                 task_options=None):
        super().__init__(name, task_options=task_options)
        self.spec_path = spec_path
        self.status_path = status_path
        self.knowledge_path = knowledge_path
        for m in self.METHODS:
            self.mcp.add_tool(getattr(self, m), name=f"{name}_{m}")
        super().__post_init__()

class SecurityTool(_CoreMixin, _KnowledgeMixin, ChiaTool):
    """The security sub-agent's surface — and the reason this repo does not
    simply ask a model whether the design looks safe.

    The agent can read the spec and the mechanical security report, and it can
    do one thing no reviewer in :mod:`red.review` can: **run the instruction**.
    ``probe_vector`` takes an operand value the agent believes is dangerous,
    executes the instruction's own C model on exactly that value under
    AddressSanitizer and UndefinedBehaviorSanitizer with the input block
    shadowed and the output block poisoned, and returns what happened. A vector
    that faults becomes a ``confirmed`` finding on the spec — the same standing
    as a finding from the batteries, because it was established the same way.

    That is the whole division of labour. The agent is good at *hypotheses*
    ("this reduction probably mishandles the carry when every limb is 0xFFFFFFFF")
    and unreliable at *verdicts*; a sanitizer is the reverse. So the agent
    proposes and the machine disposes, and what the agent merely asserts is
    recorded at ``major`` and can never fail Gate 4 on its own
    (:func:`red.security.parse_agent_findings`).

    It cannot write the spec: the designer owns that, and a reviewer that can
    edit what it reviews is not a reviewer.
    """

    METHODS = ("read_spec", "read_core", "read_security_report",
               "probe_vector", "report_findings", "read_knowledge",
               "append_knowledge")

    def __init__(self, name, spec_path, core_path, security_path,
                 findings_path, probe_dir, knowledge_path, task_options=None):
        super().__init__(name, task_options=task_options)
        self.spec_path = spec_path
        self.core_path = core_path
        self.security_path = security_path
        self.findings_path = findings_path
        self.probe_dir = probe_dir
        self.knowledge_path = knowledge_path
        self.probes = 0
        for m in self.METHODS:
            self.mcp.add_tool(getattr(self, m), name=f"{name}_{m}")
        super().__post_init__()

    def _spec(self):
        if not os.path.exists(self.spec_path):
            return None
        try:
            with open(self.spec_path) as f:
                return state_def.from_json(f.read(), state_def.ISASpec)
        except (json.JSONDecodeError, TypeError, ValueError):
            return None

    def read_spec(self) -> str:
        """Return the ISASpec under review, including every instruction's C
        reference model — the model is what actually executes, so it is what you
        are auditing."""
        if not os.path.exists(self.spec_path):
            return "No ISASpec yet."
        with open(self.spec_path) as f:
            return f.read()

    def read_security_report(self) -> str:
        """Return the mechanical security report: which checks ran, which could
        NOT run on this host, and what they found. Read it first — do not spend
        a finding on something already established, and do look hard at whatever
        the skipped checks would have covered."""
        if not os.path.exists(self.security_path):
            return "The mechanical checks have not run yet."
        with open(self.security_path) as f:
            return f.read()

    def probe_vector(self, mnemonic: str, vector_hex: str,
                     why: str = "") -> str:
        """Execute one instruction's C model on an operand value **you** choose,
        under AddressSanitizer + UndefinedBehaviorSanitizer.

        `mnemonic`: the instruction. `vector_hex`: the whole input block as hex,
        word 0 first, 8 hex digits per word, `words` words (so a 16-word
        instruction takes 128 hex digits); spaces and `0x` are ignored. `why`:
        one line on what you expect to break, for the record.

        Returns the output words when the model is clean on that vector, or the
        fault when it is not — an out-of-bounds access, undefined behaviour, a
        modified input block, an unwritten output word, or two calls that
        disagree. A fault found this way is recorded as a **confirmed blocking
        finding**: this is how you make a security claim count, rather than
        asserting it.
        """
        from red import security
        from red.constants import SECURITY_MAX_PROBES
        if self.probes >= SECURITY_MAX_PROBES:
            return (f"Probe budget spent ({SECURITY_MAX_PROBES} this round). "
                    "Report what you have.")
        spec = self._spec()
        if spec is None:
            return "No ISASpec to probe."
        ins = next((i for i in spec.instructions
                    if i.mnemonic == (mnemonic or "").strip()), None)
        if ins is None:
            return ("No instruction with mnemonic {!r}. The spec defines: {}."
                    .format(mnemonic, ", ".join(i.mnemonic for i in spec.instructions)))
        self.probes += 1
        text, finding = security.probe(ins, vector_hex,
                                       os.path.join(self.probe_dir, "probes"))
        if finding is not None:
            finding.evidence = ((why.strip() + "\n\n") if why else "") + finding.evidence
            self._append_finding(finding)
        return text

    def _append_finding(self, finding) -> None:
        """Persist a probe-confirmed finding where the loop collects them."""
        from dataclasses import asdict
        items = []
        if os.path.exists(self.findings_path):
            try:
                with open(self.findings_path) as f:
                    items = json.load(f)
            except (json.JSONDecodeError, OSError):
                items = []
        items.append(asdict(finding))
        with open(self.findings_path, "w") as f:
            json.dump(items, f, indent=2)

    def report_findings(self, findings_json: str) -> str:
        """Record your findings as a JSON array and end your turn. One object
        per finding:

            [{"instruction": "<mnemonic or empty for spec-wide>",
              "class": "<short id, e.g. side_channel, residue, privilege>",
              "severity": "major" | "minor",
              "finding": "<one sentence>",
              "evidence": "<why you believe it — cite the spec or a probe>",
              "fix": "<the concrete change>"}]

        Findings you only assert are recorded as advisory: they reach the
        designer but cannot fail the gate. To make one count, break the model
        with `probe_vector` first — that result is recorded for you.
        """
        from red import security
        found = security.parse_agent_findings(findings_json)
        if not found and (findings_json or "").strip() not in ("[]", ""):
            return ("Rejected: could not parse a JSON array of findings out of "
                    "that. Reply with the array itself, nothing else.")
        for f in found:
            self._append_finding(f)
        return (f"Recorded {len(found)} finding(s). The mechanical checks decide "
                "the gate; yours reach the designer as advice.")


class RelayTool:
    """A relay-aware stand-in for a :class:`ChiaTool`, passed to the LLM backend.

    The backends build each tool's MCP endpoint as
    ``http://{tool.hostname}:{tool.port}/{tool.name}/mcp`` from the attributes of
    the tool object they are handed. RED's tools are pinned to ``head_local``, so
    that hostname is the head's own address — which a tunnelled cloud worker
    cannot dial: CHIA reverse-forwards the head's tool ports onto the worker's
    ``CHIA_TOOL_RELAY_HOST`` (same port numbers) instead. CHIA ships
    :func:`chia.base.tools.ChiaTool.resolve_tool_url` for exactly this rewrite,
    but no model backend calls it, so on a tunnelled cluster every agent turn
    dies inside the MCP task group with an unreachable endpoint.

    This proxy carries the tool's identity and resolves the host **lazily, on
    whichever node reads it** — the relay environment variables only exist on the
    worker. On a single machine (no relay vars) it returns the address unchanged,
    so ``--local-llm`` behaves exactly as before.
    """

    __slots__ = ("name", "port", "node_id", "_hostname")

    def __init__(self, tool: ChiaTool):
        self.name = tool.name
        self.port = getattr(tool, "port", 8000)
        self.node_id = getattr(tool, "node_id", None)
        self._hostname = tool.hostname

    @property
    def hostname(self) -> str:
        advertise = os.environ.get("CHIA_TOOL_ADVERTISE_HOST")
        relay = os.environ.get("CHIA_TOOL_RELAY_HOST")
        if advertise and relay and self._hostname == advertise:
            return relay
        return self._hostname

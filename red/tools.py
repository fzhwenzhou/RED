"""Sealed MCP tools for the RED loop.

These pin to ``head_local`` so their files land on the driver's disk and get
archived. The destination paths are baked in at construction — the agents only
see the payload, never a path they could redirect.

RED is general-purpose: ``SourceTool`` serves whatever project/core the loop was
given (never a hard-coded file list). The agents work through typed JSON
artifacts — the hot-loop report and the ISA spec — and every write is validated
against its schema before being accepted, so a malformed artifact is rejected
back to the agent instead of poisoning a downstream node.

Tool surface the agents see:
    list_sources() / read_source(path)   – open the project + core sources
    read_profile()                       – the mechanical profile (A1, ground truth)
    read_report() / write_report(json)   – the HotLoopReport (A1 mining agent writes;
                                           A2 reads it as ground truth)
    read_spec() / write_spec(json)       – the ISASpec draft (A2); the ISS
                                           implementer gets its own pair that
                                           hides the C models and accepts only
                                           spike_model (IssSpecTool)
    read_status()                        – the last verification verdict + counterexample
    append_knowledge(note)               – durable cross-iteration notes
    finish(summary)                      – declare the extension complete
"""

from __future__ import annotations

import json
import os

from chia.base.tools.ChiaTool import ChiaTool

from red import iss, state_def
from red.constants import REPO_ROOT, find_sources


class SourceTool(ChiaTool):
    """Points the agents at the project + core sources to open themselves.
    ``list_sources`` returns repo-relative paths; ``read_source`` returns text."""

    def __init__(self, name, source_roots, task_options=None):
        super().__init__(name, task_options=task_options)
        self.source_roots = source_roots
        self.mcp.add_tool(self.list_sources, name=f"{name}_list_sources")
        self.mcp.add_tool(self.read_source, name=f"{name}_read_source")
        super().__post_init__()

    def list_sources(self) -> str:
        """List the project + core source files (C/H/asm/Verilog). Paths are
        relative to the repo root."""
        rel = sorted({rel for root in self.source_roots
                      for rel, _full in find_sources(root)})
        return "\n".join(f"- {p}" for p in rel)

    def read_source(self, rel_path: str) -> str:
        """Return the contents of a repo-relative source file."""
        full = os.path.join(REPO_ROOT, rel_path)
        if not os.path.abspath(full).startswith(REPO_ROOT + os.sep):
            return "Error: path escapes the repo root."
        if not os.path.isfile(full):
            return f"Error: no such file {rel_path}"
        with open(full, "r", errors="replace") as f:
            return f.read()


class ProfileTool(ChiaTool):
    """READ-ONLY view of the mechanical profile produced by A1's profiling pass
    (per-function cycle / instruction counts). The loop recomputes it — the
    agents cannot self-report it."""

    def __init__(self, name, profile_path, task_options=None):
        super().__init__(name, task_options=task_options)
        self.profile_path = profile_path
        self.mcp.add_tool(self.read_profile, name=f"{name}_read_profile")
        super().__post_init__()

    def read_profile(self) -> str:
        """Return the current mechanical profile (method, per-function counts)."""
        if not os.path.exists(self.profile_path):
            return "No profile has been produced yet."
        with open(self.profile_path) as f:
            return f.read()


class ReportTool(ChiaTool):
    """The HotLoopReport — A1's typed artifact. The mining agent *writes* its
    ranked loop selection (``write_report``, validated); A2 *reads* it as ground
    truth. Dynamic numbers (cycle share, profile runs) are merged mechanically
    by the loop from the profile, not taken on the agent's word."""

    def __init__(self, name, report_path, task_options=None):
        super().__init__(name, task_options=task_options)
        self.report_path = report_path
        self.mcp.add_tool(self.read_report, name=f"{name}_read_report")
        self.mcp.add_tool(self.write_report, name=f"{name}_write_report")
        super().__post_init__()

    def read_report(self) -> str:
        """Return the current HotLoopReport JSON (mined loops, cycle share,
        coverage, Gate-1 status)."""
        if not os.path.exists(self.report_path):
            return "No HotLoopReport yet."
        with open(self.report_path) as f:
            return f.read()

    def write_report(self, report_json: str) -> str:
        """Record your ranked loop selection. `report_json` must be a valid
        HotLoopReport (workload + loops[] with loop_id/function); it is
        validated and rejected if not. Dynamic cycle numbers are the loop's to
        fill in from the profile."""
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


class SpecTool(ChiaTool):
    """The ISASpec draft — A2's working artifact. ``write_spec`` validates the
    JSON against the ISASpec schema before accepting, so a malformed draft is
    rejected back to the agent instead of reaching the verification edges."""

    def __init__(self, name, spec_path, task_options=None):
        super().__init__(name, task_options=task_options)
        self.spec_path = spec_path
        self.mcp.add_tool(self.read_spec, name=f"{name}_read_spec")
        self.mcp.add_tool(self.write_spec, name=f"{name}_write_spec")
        super().__post_init__()

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


class IssSpecTool(ChiaTool):
    """The ISASpec as the **ISS implementer** may see and change it.

    Gate 2 is only worth running if the two executable models were written
    independently, so that is enforced here rather than asked for in a prompt:

    * ``read_spec`` serves the spec with every ``c_model`` stripped out, so the
      ISS agent cannot copy the implementation it is supposed to cross-check.
    * ``write_spec`` takes **only** the ``spike_model`` fields from what the
      agent writes and merges them into the spec on disk, so an ISS turn cannot
      quietly edit the semantics, encodings or C models it disagreed with.
    """

    def __init__(self, name, spec_path, task_options=None):
        super().__init__(name, task_options=task_options)
        self.spec_path = spec_path
        self.mcp.add_tool(self.read_spec, name=f"{name}_read_spec")
        self.mcp.add_tool(self.write_spec, name=f"{name}_write_spec")
        super().__post_init__()

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


class StatusTool(ChiaTool):
    """Per-instruction pass/fail status, READ-ONLY, recomputed by the loop from
    the evals — ground truth, not agent self-report."""

    def __init__(self, name, status_path, task_options=None):
        super().__init__(name, task_options=task_options)
        self.status_path = status_path
        self.mcp.add_tool(self.read_status, name=f"{name}_read_status")
        super().__post_init__()

    def read_status(self) -> str:
        """Return the per-instruction status from the latest evaluation."""
        if not os.path.exists(self.status_path):
            return "No evaluations have run yet."
        with open(self.status_path) as f:
            return f.read()


class KnowledgeTool(ChiaTool):
    """Durable cross-iteration scratch memory the agents append to."""

    def __init__(self, name, knowledge_path, task_options=None):
        super().__init__(name, task_options=task_options)
        self.knowledge_path = knowledge_path
        self.mcp.add_tool(self.read_knowledge, name=f"{name}_read_knowledge")
        self.mcp.add_tool(self.append_knowledge, name=f"{name}_append_knowledge")
        super().__post_init__()

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


class FinishTool(ChiaTool):
    """Sentinel the A2 synthesis agent drops to declare the extension complete;
    the loop verifies against Gate 2 + the verification chain before accepting."""

    def __init__(self, name, sentinel_path, task_options=None):
        super().__init__(name, task_options=task_options)
        self.sentinel_path = sentinel_path
        self.mcp.add_tool(self.finish, name=f"{name}_finish")
        super().__post_init__()

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

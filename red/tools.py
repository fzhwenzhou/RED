"""Sealed MCP tools for the RED loop.

These pin to ``head_local`` so their files land on the driver's disk and get
archived. The destination paths are baked in at construction — the agents only
see the payload. The *editor* (``chia.base.tools.BashTool``, work_dir = project
root) is built in the loop itself.

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
    read_spec() / write_spec(json)       – the ISASpec draft (A2)
    read_status()                        – per-instruction pass/fail (latest evals)
    append_knowledge(note)               – durable cross-iteration notes
    finish(summary)                      – declare the extension complete
"""

from __future__ import annotations

import json
import os

from chia.base.tools.ChiaTool import ChiaTool

from red import state_def
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
            state_def.validate_hot_loop_report(report)
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            return f"Rejected: invalid HotLoopReport — {e}"
        with open(self.report_path, "w") as f:
            f.write(state_def.to_json(report))
        return f"Accepted HotLoopReport for '{report.workload}' ({len(report.loops)} loops)."


class SpecTool(ChiaTool):
    """The ISASpec draft — A2's working artifact. ``write_spec`` validates the
    JSON against the ISASpec schema before accepting, so a malformed draft is
    rejected back to the agent instead of poisoning A3/A4."""

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
            state_def.validate_isa_spec(spec)
        except (json.JSONDecodeError, TypeError, ValueError) as e:
            return f"Rejected: invalid ISASpec — {e}"
        with open(self.spec_path, "w") as f:
            f.write(state_def.to_json(spec))
        return f"Accepted ISASpec '{spec.name}' ({len(spec.instructions)} instructions)."


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

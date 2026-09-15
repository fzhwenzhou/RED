"""Reading callgrind's own output format.

Kept free of every other RED import so both the profiling node (red.nodes) and
the cost model (red.cost) can use one parser, and so it is testable without a
cluster.

RED parses the raw callgrind file rather than ``callgrind_annotate`` text.
Annotate prints a row only above a cost threshold and reformats between
releases; the raw file records every event exactly at every version. That is
not a stylistic preference: on valgrind 3.19 the annotate output showed no
callee row at all for micro-ecc's two hot kernels, because each is reached from
many individually-cold call sites. Their call counts read as zero, a call count
of zero disables all benefit crediting in Gate 3, and three full cluster runs
reported a worthless extension instead of a broken profile.
"""

from __future__ import annotations

import re

_NAME = re.compile(r"^(c?fn)=\((\d+)\)(?:\s+(.*))?$")
_COST = re.compile(r"^(?:[\d+\-*]\S*)\s+(\d+)")
_CALLS = re.compile(r"^calls=(\d+)")


def parse_raw(path: str) -> tuple[dict, dict]:
    """(self Ir per function, call count per function) from a raw callgrind file.

    This is the primary profile source, in preference to ``callgrind_annotate``
    text, because the raw file records every event exactly and identically at
    every valgrind version, while the annotate output is threshold-filtered and
    reformatted between releases.

    Format notes that the parse depends on:
      * Names are compressed: ``fn=(7) foo`` defines id 7 and a later ``fn=(7)``
        refers back to it. ``fn=`` and ``cfn=`` share one namespace.
      * Exactly one cost line follows a ``calls=`` line, and it carries the
        *inclusive* cost of that call -- adding it to the caller would double
        count the callee, so it is skipped.
      * callgrind appends ``'2``/``'3`` to a recursively re-entered function;
        those fold back into the base name so one function is one entry.

    Verified against valgrind 3.19 and 3.27 output: the summed self cost
    reproduces the file's own ``summary:`` line exactly.
    """
    names: dict[str, str] = {}
    self_ir: dict = {}
    calls: dict = {}
    cur = None             # function accumulating self cost
    pending = None         # callee named by the most recent cfn=
    skip_cost = False      # the next cost line belongs to a call, not to cur

    try:
        fh = open(path, errors="replace")
    except OSError:
        return {}, {}
    with fh:
        for line in fh:
            line = line.rstrip("\n")
            m = _NAME.match(line)
            if m:
                kind, ident, name = m.group(1), m.group(2), m.group(3)
                if name:
                    names[ident] = name
                name = names.get(ident, "")
                if not name:
                    continue
                name = name.split("'")[0]
                if kind == "fn":
                    cur, pending, skip_cost = name, None, False
                else:
                    pending = name
                continue
            m = _CALLS.match(line)
            if m:
                # Count entries from OUTSIDE the function only. Folding the
                # ``'2`` suffix merges a recursively re-entered function back
                # into its base, which turns the internal ``foo -> foo'2`` edge
                # into a self-edge; counting it would inflate the call count
                # (and so deflate the measured cost per call that Gate 3
                # compares an instruction against).
                if pending is not None and pending != cur:
                    calls[pending] = calls.get(pending, 0) + int(m.group(1))
                skip_cost = True
                continue
            m = _COST.match(line)
            if m:
                if skip_cost:
                    skip_cost, pending = False, None
                elif cur is not None:
                    self_ir[cur] = self_ir.get(cur, 0) + int(m.group(1))
                continue
    return self_ir, calls



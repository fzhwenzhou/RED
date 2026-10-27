"""RED's durable artifact store.

Every run's typed artifacts are written here as they are produced, so the
record of a run outlives the run directory and a later batch can be compared
against an earlier one. Plain files on local disk: the store used to be a
separate cluster node reached through Ray's object store, which bought nothing
once the loop stopped being distributed.

Layout produced under ``DB_ROOT``::

    <workload>/run_<run_id>/
    ├── profile.yaml              # A1 mechanical profile
    ├── HotLoopReport.yaml        # A1 output (typed)
    ├── ISASpec.draft.yaml        # A2 output before the verification edges
    ├── ISASpec.yaml              # A2 output, final — RED's deliverable
    ├── gates/                    # gate1 / gate2 / link1 / link2 verdicts
    └── summary.md                # human-readable result table
"""

from __future__ import annotations

import io
import os
import re
import tarfile


from red.constants import DB_ROOT


def claim_run(workload: str, db_root: str = "",
              prefix: str = "run") -> tuple[int, str]:
    """Allocate the next free ``<prefix>-<N>`` under <db_root>/<workload>/;
    returns (N, abs_path). Atomic via mkdir; racing callers retry the next
    integer.

    The driver passes *db_root* so the store's location is decided in one
    place and every later ``put`` targets the same absolute path.

    *prefix* exists so a batch can be labelled by what produced it, e.g.
    ``opus-5.5-run-1``. Numbering is per-prefix: a new prefix starts at 1
    beside whatever is already there, which is what makes two model sweeps
    comparable without one renumbering the other. The default keeps the
    historical ``run_<N>`` spelling, underscore and all."""
    root = os.path.join(db_root or DB_ROOT, workload)
    os.makedirs(root, exist_ok=True)
    sep = "_" if prefix == "run" else "-"
    pat = re.compile(re.escape(prefix) + re.escape(sep) + r"(\d+)$")
    while True:
        used = [int(m.group(1)) for d in os.listdir(root)
                if (m := pat.match(d))]
        n = max(used, default=0) + 1
        path = os.path.join(root, f"{prefix}{sep}{n}")
        try:
            os.mkdir(path)
            return n, path
        except FileExistsError:
            continue


def put(dest_dir: str, rel_path: str, content, workload: str = "") -> None:
    """Durably write ONE artifact at dest_dir/rel_path the moment the loop
    produces it (incremental archival — a mid-run kill keeps everything logged
    so far). ``content`` is str or bytes; parent dirs are created."""
    path = os.path.join(dest_dir, rel_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    mode = "wb" if isinstance(content, (bytes, bytearray)) else "w"
    with open(path, mode) as f:
        f.write(content)


def write_text(dest_dir: str, filename: str, content: str, workload: str = "") -> None:
    """Write a text file (summary.md, etc.) at dest_dir/filename."""
    with open(os.path.join(dest_dir, filename), "w") as f:
        f.write(content)


def fetch_run(sweep_path: str) -> bytes:
    """Return a directory as a tar blob, for archiving it under another.

    Used for the agent transcripts: they are written to the driver's scratch
    as the run goes, and folded into the durable store as one blob at the end,
    because that scratch is what the operator deletes between batches."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        tar.add(sweep_path, arcname=".")
    return buf.getvalue()


def archive_dir(dest_dir: str, name: str, tarball: bytes, workload: str = "") -> None:
    """Extract a tar blob into dest_dir/name/."""
    dst = os.path.join(dest_dir, name)
    os.makedirs(dst, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r") as t:
        t.extractall(path=dst)

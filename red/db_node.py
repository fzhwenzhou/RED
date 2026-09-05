"""The ``database`` ray node: RED's durable artifact store.

Every remote is pinned to the ``database`` resource so Ray schedules it on the
one host with local disk for the store. Drivers ship typed artifacts (JSON) and
bytes through the Ray object store; this node writes them to local disk — no
SSH, no rsync (CHIA field-guide pattern).

Layout produced under ``DB_ROOT``::

    <workload>/run_<run_id>/
    ├── profile.json              # A1 mechanical profile
    ├── HotLoopReport.json        # A1 output (typed)
    ├── ISASpec.draft.json        # A2 output before the verification edges
    ├── ISASpec.json              # A2 output, final — RED's deliverable
    ├── gates/                    # gate1 / gate2 / link1 / link2 verdicts
    └── summary.md                # human-readable result table
"""

from __future__ import annotations

import io
import os
import re
import tarfile

from chia.base.ChiaFunction import ChiaFunction
from chia.trace.profiler import get_profiler

from red.constants import DB_ROOT


@ChiaFunction(resources={"database": 0.9})
def claim_run(workload: str, db_root: str = "") -> tuple[int, str]:
    """Allocate the next free ``run_<N>`` under <db_root>/<workload>/; returns
    (N, abs_path). Atomic via mkdir; racing callers retry the next integer.

    The driver passes *db_root* so the store's location is decided in one place
    and every later ``put`` targets the same absolute path. Falling back to this
    node's own ``DB_ROOT`` would resolve against wherever Ray happened to stage
    the ``red`` module on this worker, not the rsynced repo."""
    if workload:
        get_profiler().add_info({"workload": workload})
    root = os.path.join(db_root or DB_ROOT, workload)
    os.makedirs(root, exist_ok=True)
    while True:
        used = [int(m.group(1)) for d in os.listdir(root)
                if (m := re.match(r"run_(\d+)$", d))]
        n = max(used, default=0) + 1
        path = os.path.join(root, f"run_{n}")
        try:
            os.mkdir(path)
            return n, path
        except FileExistsError:
            continue


@ChiaFunction(resources={"database": 0.9})
def put(dest_dir: str, rel_path: str, content, workload: str = "") -> None:
    """Durably write ONE artifact at dest_dir/rel_path the moment the loop
    produces it (incremental archival — a mid-run kill keeps everything logged
    so far). ``content`` is str or bytes; parent dirs are created."""
    if workload:
        get_profiler().add_info({"workload": workload})
    path = os.path.join(dest_dir, rel_path)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    mode = "wb" if isinstance(content, (bytes, bytearray)) else "w"
    with open(path, mode) as f:
        f.write(content)


@ChiaFunction(resources={"database": 0.9})
def write_text(dest_dir: str, filename: str, content: str, workload: str = "") -> None:
    """Write a text file (summary.md, etc.) at dest_dir/filename."""
    if workload:
        get_profiler().add_info({"workload": workload})
    with open(os.path.join(dest_dir, filename), "w") as f:
        f.write(content)


@ChiaFunction(resources={"database": 0.9})
def fetch_run(sweep_path: str) -> bytes:
    """Return the whole run directory as a tar blob.

    The store lives on the database node's own disk, which on a cloud cluster
    is a VM that teardown deletes. The driver calls this at the end of a run and
    unpacks the blob on the head, so the artifacts outlive the cluster."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        tar.add(sweep_path, arcname=".")
    return buf.getvalue()


@ChiaFunction(resources={"database": 0.9})
def archive_dir(dest_dir: str, name: str, tarball: bytes, workload: str = "") -> None:
    """Extract a tar blob into dest_dir/name/."""
    if workload:
        get_profiler().add_info({"workload": workload})
    dst = os.path.join(dest_dir, name)
    os.makedirs(dst, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(tarball), mode="r") as t:
        t.extractall(path=dst)

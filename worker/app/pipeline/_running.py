"""Per-job subprocess registry, used by the worker heartbeat to
implement user-requested job cancellation.

Pipeline steps that shell out to a long-running subprocess (e.g.
``ns-train``, ``ns-export``) register their
``asyncio.subprocess.Process`` here under the running job's id;
the heartbeat task in ``app.jobs.runner._heartbeat`` polls the DB
each cycle, sees ``status == canceled``, reaches in here to send
SIGKILL, and ALSO cancels the dispatch coroutine. The subprocess
dying breaks the step's ``await proc.wait()``, and the asyncio
cancel breaks any pre-subprocess setup; either way the runner's
outer except sees the resulting exception, checks the DB row,
and treats it as a cancellation rather than a crash when the DB
row says so.

Two cancellation modes are supported:

* **Direct PID kill** (default) — ``proc.kill()`` sends SIGKILL
  to the process we directly spawned. Sufficient when the step
  is a single subprocess (e.g. ns-train) that doesn't fork
  long-running grandchildren.
* **Process-group kill** (``register(..., pgid=True)``) — for
  steps that spawn an *orchestrator* subprocess which in turn
  shells out to other binaries (the standard-tier mesh pipeline
  does this: a Python orchestrator that runs
  ``InterfaceCOLMAP → DensifyPointCloud → ReconstructMesh →
  RefineMesh → TextureMesh`` in sequence). The orchestrator MUST
  be spawned with ``start_new_session=True`` so it becomes a new
  process-group leader; the killer then does
  ``os.killpg(proc.pid, SIGKILL)`` which reaches every
  grandchild. Without this a user-cancelled standard-tier mesh
  leaves a multi-GB ``DensifyPointCloud`` chewing CPU/RAM until
  natural completion.

A module-level dict (rather than a ContextVar) on purpose:
heartbeat lives in a different ``asyncio.Task`` than the step
that spawned the subprocess, so a ContextVar set by the step
wouldn't be visible to the heartbeat task. The dict is keyed by
job id (unique per step per scene), and only ever holds running
jobs claimed by THIS worker process, so it stays small.
"""
from __future__ import annotations

import asyncio
import logging
import os
import signal

log = logging.getLogger(__name__)


class _Entry:
    """Registry entry — pairs the subprocess handle with whether
    we should kill its full process group on cancel."""

    __slots__ = ("proc", "pgid")

    def __init__(self, proc: asyncio.subprocess.Process, pgid: bool) -> None:
        self.proc = proc
        self.pgid = pgid


_running_procs: dict[str, _Entry] = {}


def register(
    job_id: str,
    proc: asyncio.subprocess.Process,
    *,
    pgid: bool = False,
) -> None:
    """Register a subprocess for ``job_id``.

    ``pgid=True`` switches the cancel path to ``os.killpg`` so any
    grandchildren spawned by ``proc`` are also killed. The caller
    is responsible for spawning ``proc`` with
    ``start_new_session=True`` (or equivalent) so ``proc.pid`` is
    actually a process-group id.
    """
    _running_procs[job_id] = _Entry(proc=proc, pgid=pgid)


def unregister(job_id: str) -> None:
    _running_procs.pop(job_id, None)


def kill_for_job(job_id: str) -> bool:
    """Send SIGKILL to the registered subprocess (or process group)
    for ``job_id``.

    No-op if no subprocess is registered (job hasn't reached the
    subprocess-spawning step yet, or has already finished).
    Returns True iff a kill was actually attempted.
    """
    entry = _running_procs.get(job_id)
    if entry is None:
        return False
    proc = entry.proc
    try:
        if entry.pgid:
            # ``proc.pid`` is also the pgid because the caller
            # passed start_new_session=True. ``killpg`` reaches
            # every descendant in that group, including any
            # in-flight grandchild (e.g. OpenMVS DensifyPointCloud
            # under the standard-tier mesh orchestrator).
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
        return True
    except ProcessLookupError:
        # Process / group already exited between our get() and
        # kill() — nothing to do.
        return False
    except Exception:  # noqa: BLE001
        log.exception("failed to kill subprocess for job=%s", job_id)
        return False

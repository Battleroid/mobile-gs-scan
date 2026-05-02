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

log = logging.getLogger(__name__)

_running_procs: dict[str, asyncio.subprocess.Process] = {}


def register(job_id: str, proc: asyncio.subprocess.Process) -> None:
    _running_procs[job_id] = proc


def unregister(job_id: str) -> None:
    _running_procs.pop(job_id, None)


def kill_for_job(job_id: str) -> bool:
    """Send SIGKILL to the registered subprocess for ``job_id``.

    No-op if no subprocess is registered (job hasn't reached the
    subprocess-spawning step yet, or has already finished).
    Returns True iff a kill was actually attempted.
    """
    proc = _running_procs.get(job_id)
    if proc is None:
        return False
    try:
        proc.kill()
        return True
    except ProcessLookupError:
        # Process already exited between our get() and kill() —
        # nothing to do.
        return False
    except Exception:  # noqa: BLE001
        log.exception("failed to kill subprocess for job=%s", job_id)
        return False

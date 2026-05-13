"""Job query, log, and cancel endpoints."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from app.config import get_settings
from app.jobs import events, store
from app.jobs.finalize import maybe_finalize_scene
from app.jobs.schema import EditStatus, JobKind, JobStatus, MeshStatus

router = APIRouter(prefix="/api/jobs", tags=["jobs"])


@router.get("/{job_id}")
async def get_job(job_id: str) -> dict:
    job = await store.get_job(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    return {
        "id": job.id,
        "scene_id": job.scene_id,
        "kind": job.kind.value,
        "status": job.status.value,
        "progress": job.progress,
        "progress_msg": job.progress_msg,
        "error": job.error,
        "result": job.result,
        "claimed_by": job.claimed_by,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "completed_at": job.completed_at.isoformat() if job.completed_at else None,
    }


@router.get("/{job_id}/log")
async def get_job_log(
    job_id: str,
    tail_bytes: int = Query(default=8192, ge=0, le=1_000_000),
) -> dict:
    """Read the tail of the subprocess log file for this job.

    Polled by the web UI's collapsible per-step log panel while the
    job is running, so the user gets a live view of glomap /
    splatfacto / ns-export output without `docker exec` into the
    worker container.
    """
    job = await store.get_job(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    scene = await store.get_scene(job.scene_id)
    if scene is None:
        raise HTTPException(404, "scene not found")

    settings = get_settings()
    scene_dir = settings.scenes_dir() / scene.id
    log_path = _log_path_for_kind(job.kind, scene_dir)

    if log_path is None or not log_path.exists():
        return {
            "log": "",
            "size": 0,
            "path": str(log_path) if log_path else None,
            "available": False,
        }

    size = log_path.stat().st_size
    with log_path.open("rb") as f:
        if size > tail_bytes:
            f.seek(-tail_bytes, 2)
        data = f.read()

    return {
        "log": data.decode("utf-8", errors="replace"),
        "size": size,
        "path": str(log_path),
        "available": True,
    }


@router.post("/{job_id}/cancel")
async def cancel_job_endpoint(job_id: str) -> dict:
    """Request cancellation of an in-flight job.

    Marks the row ``status=canceled`` if it's still queued /
    claimed / running. The worker that owns the job notices on its
    next heartbeat (~5 s), SIGKILLs any registered subprocess, and
    cancels the dispatch coroutine. Idempotent; calling again on
    an already-canceled / completed / failed job returns
    ``canceled: false``.

    Cancelling a queued-but-unclaimed mesh / filter job needs to
    cascade into the scene's status column too — the worker's
    ``_run_filter`` / ``_run_mesh`` reset paths only fire on jobs
    they actually claimed, so a job killed before claim would leave
    ``edit_status``/``mesh_status`` stuck at ``queued`` forever.
    Reset here when the worker won't.

    A queued thumbnail cancel needs a different cleanup: the
    thumbnail job is the last step in the pipeline, so by the time
    it's been enqueued every essential upstream job is already
    terminal. If we don't trigger finalize here, no worker will ever
    claim that thumbnail row (since it's already canceled), so
    ``_maybe_finalize_scene`` never fires and the scene + capture
    stay stuck at ``processing`` forever. Filter / mesh queued
    cancels don't share the bug because their scene was already
    ``completed`` before the user triggered them; the finalize call
    is just defensive there.
    """
    job = await store.get_job(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    pre_status = job.status
    canceled = await store.cancel_job(job_id)
    if canceled:
        await events.publish_job(job_id, "job.canceled")
        # Only intervene on jobs the worker is unlikely to clean up:
        # rows that hadn't been claimed yet (queued) get no
        # _run_filter/_run_mesh pass at all. Claimed/running rows
        # are the worker's to reset on its next heartbeat tick.
        if pre_status == JobStatus.queued and job.kind in (
            JobKind.filter,
            JobKind.mesh,
        ):
            await _reset_scene_status_for_canceled_job(
                scene_id=job.scene_id, kind=job.kind,
            )
        if pre_status == JobStatus.queued and job.kind == JobKind.thumbnail:
            await maybe_finalize_scene(job.scene_id)
    refreshed = await store.get_job(job_id)
    return {
        "ok": True,
        "canceled": canceled,
        "status": refreshed.status.value if refreshed else "unknown",
    }


@router.post("/{job_id}/retry")
async def retry_job_endpoint(job_id: str) -> dict:
    """Enqueue a fresh copy of a failed / canceled job.

    The original row stays in the DB unchanged as an audit trail;
    callers (web PipelineJobRow, Android JobDetailScreen) surface
    the retry button only on terminal-non-success rows.

    Only failed and canceled rows are eligible — running / claimed /
    queued / completed all return 409. Retrying a completed job
    would silently double-run; retrying an in-flight job would
    introduce a parallel run of the same kind for the same scene
    (the cancel-then-retry flow does this in two steps, which is
    what the existing UI promises).

    Restoring scene-level status (``scene.status``,
    ``edit_status``, ``mesh_status``) is intentionally left to the
    runner's normal claim path. ``_run_one`` flips
    ``scene.status`` to ``processing`` on every pipeline-kind
    claim; ``_run_filter`` / ``_run_mesh`` flip their own status
    columns the same way. By the time the user sees the retry
    succeed, the scene is back to its expected mid-pipeline state.

    Returns the new job's id + kind so the client can route to its
    detail page (Android) or scroll the pipeline list (web).
    """
    job = await store.get_job(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    if job.status not in (JobStatus.failed, JobStatus.canceled):
        raise HTTPException(
            409,
            f"only failed/canceled jobs can be retried (this one is {job.status.value})",
        )
    new_job = await store.enqueue_job(
        job.scene_id, job.kind, payload=job.payload or {},
    )
    if new_job is None:
        # Scene cascaded away between get_job and enqueue — the
        # capture-delete cascade just won. Surface 404 rather than
        # silently dropping the retry.
        raise HTTPException(404, "scene no longer exists")
    return {
        "ok": True,
        "job_id": new_job.id,
        "kind": new_job.kind.value,
        "status": new_job.status.value,
    }


async def _reset_scene_status_for_canceled_job(
    *, scene_id: str, kind: JobKind,
) -> None:
    """Cascade a job-level cancel into the scene-level status column.

    Race-safe in two dimensions:
      * Only flips when the column is still in an in-flight value
        (queued/running) — never demotes a completed/failed/none
        state.
      * Skips entirely when ANY other non-terminal job of the same
        kind exists for the scene. That covers the common
        cancel-and-immediately-re-POST flow: cancelling the old
        queued job sees the replacement's queued state and would
        otherwise clobber it back to none, hiding the new pending
        extraction. The replacement's own start path will own the
        next status transition.
    Emits the matching ``scene.*_cleared`` event so the web client
    picks up the reset without a refresh.
    """
    in_flight = (JobStatus.queued, JobStatus.claimed, JobStatus.running)
    other_jobs = [
        j
        for j in await store.list_jobs_for_scene(scene_id)
        if j.kind == kind and j.status in in_flight
    ]
    if other_jobs:
        # A replacement (or coexisting) job is in flight; let it
        # drive the next status transition.
        return

    scene = await store.get_scene(scene_id)
    if scene is None:
        return
    if kind == JobKind.filter:
        if scene.edit_status in (EditStatus.queued, EditStatus.running):
            await store.update_scene(scene.id, edit_status=EditStatus.none)
            await events.publish_scene(scene.id, "scene.edit_cleared")
    elif kind == JobKind.mesh:
        if scene.mesh_status in (MeshStatus.queued, MeshStatus.running):
            await store.update_scene(scene.id, mesh_status=MeshStatus.none)
            await events.publish_scene(scene.id, "scene.mesh_cleared")


def _log_path_for_kind(kind: JobKind, scene_dir: Path) -> Path | None:
    """Map a JobKind to the log file the corresponding pipeline step
    writes. SfM has two backends; pick whichever exists, falling
    back to glomap.log if neither does so the caller still sees a
    deterministic path in the response.
    """
    if kind == JobKind.sfm:
        for name in ("glomap.log", "colmap.log"):
            p = scene_dir / "sfm" / name
            if p.exists():
                return p
        return scene_dir / "sfm" / "glomap.log"
    if kind == JobKind.train:
        return scene_dir / "train" / "train.log"
    if kind == JobKind.export:
        return scene_dir / "export" / "export.log"
    if kind == JobKind.filter:
        # filter_splat writes a per-op trace to filter.log (recipe,
        # per-op kept/dropped counts, timings); spz_pack appends its
        # own log next to it. The trace is the more useful one for
        # the JobLogPanel; fall back to spz_pack if it doesn't exist
        # (interrupted before any op ran).
        primary = scene_dir / "edit" / "filter.log"
        if primary.exists():
            return primary
        return scene_dir / "edit" / "spz_pack.log"
    if kind == JobKind.mesh:
        return scene_dir / "mesh" / "mesh.log"
    if kind == JobKind.thumbnail:
        # ``pipeline/thumbnail.py`` writes scene_dir / thumbnail.log
        # (top-level, not nested under a subdir — the step doesn't
        # produce its own artifact dir, just an output PNG next to
        # the rest of the scene's outputs). Without this branch,
        # render failures have no diagnostic path from the UI's
        # JobLogPanel; the user only sees the row flip to
        # completed-with-empty-result and the failure mode is
        # invisible.
        return scene_dir / "thumbnail.log"
    return None

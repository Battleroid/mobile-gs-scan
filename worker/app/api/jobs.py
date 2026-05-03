"""Job query, log, and cancel endpoints."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from app.config import get_settings
from app.jobs import events, store
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
    refreshed = await store.get_job(job_id)
    return {
        "ok": True,
        "canceled": canceled,
        "status": refreshed.status.value if refreshed else "unknown",
    }


async def _reset_scene_status_for_canceled_job(
    *, scene_id: str, kind: JobKind,
) -> None:
    """Cascade a job-level cancel into the scene-level status column.

    Race-safe: only flips when the column is still in an in-flight
    value (queued/running). If a replacement POST has already moved
    it forward, leave it alone so the new pending job stays visible.
    Emits the matching ``scene.*_cleared`` event so the web client
    picks up the reset without a refresh.
    """
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
    return None

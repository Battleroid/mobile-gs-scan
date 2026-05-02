"""Job query + cancel endpoints."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from app.config import get_settings
from app.jobs import events, store
from app.jobs.schema import JobKind

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
    """Read the tail of the subprocess log file for this job."""
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
    """
    job = await store.get_job(job_id)
    if job is None:
        raise HTTPException(404, "job not found")
    canceled = await store.cancel_job(job_id)
    if canceled:
        await events.publish_job(job_id, "job.canceled")
    refreshed = await store.get_job(job_id)
    return {
        "ok": True,
        "canceled": canceled,
        "status": refreshed.status.value if refreshed else "unknown",
    }


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
    return None

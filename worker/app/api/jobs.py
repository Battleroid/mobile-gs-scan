"""Job query endpoints (read-only; the worker uses store directly)."""
from __future__ import annotations

from pathlib import Path

from fastapi import APIRouter, HTTPException, Query

from app.config import get_settings
from app.jobs import store
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
    """Read the tail of the subprocess log file for this job.

    Polled by the web UI's collapsible per-step log panel while the
    job is running, so the user gets a live view of glomap /
    splatfacto / ns-export output without `docker exec` into the
    worker container. Capped at `tail_bytes` (default 8 KB,
    user-overridable up to 1 MB) to keep responses small at the
    fast poll interval the UI uses.

    The mesh job kind doesn't shell out to a subprocess in PR #1
    (it's a no-op stub), so its log path resolves to None and the
    response carries `available: False` so the UI can render a
    "no log" placeholder.
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
    # mesh: no subprocess, no log.
    return None

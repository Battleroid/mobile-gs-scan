"""Job query endpoints (read-only; the worker uses store directly)."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.jobs import store

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

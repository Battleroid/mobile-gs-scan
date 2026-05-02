"""Decide which jobs to enqueue when a capture is finalized.

Mobile native (has_pose=True) → SfM step writes a COLMAP-shaped
  workspace from the phone's poses.jsonl (backend=arcore_native);
  no real Glomap / COLMAP run.
Mobile web / drag-drop (has_pose=False) → real SfM via
  settings.sfm_backend (glomap by default, colmap as fallback).

We always enqueue the SfM job (modulo settings.sfm_backend=='none')
so every downstream step can rely on `scene_dir/sfm/` existing.
Previously the has_pose=True branch skipped SfM entirely — but
nothing else in the pipeline produced the COLMAP workspace train
then needs, so train would crash on a missing directory. Routing
has_pose through the SfM job with a different backend is the
narrowest fix that keeps the dispatch shape (sfm → train → export
→ mesh) consistent across both paths.

PR #1 always enqueues a `mesh` job too, but worker-gs treats it as
a no-op so the pipeline still completes. PR #2 fills it in with the
2DGS + TSDF + Poisson path.
"""
from __future__ import annotations

from app.config import get_settings
from app.jobs import store
from app.jobs.schema import CaptureSource, JobKind


async def enqueue_pipeline(
    scene_id: str,
    *,
    has_pose: bool,
    source: CaptureSource,
) -> list[str]:
    settings = get_settings()
    job_ids: list[str] = []

    backend: str | None
    if has_pose:
        # Phone capture with ARCore poses. The SfM job runs the
        # arcore_native handler in app/pipeline/sfm.py, which
        # converts poses.jsonl into a COLMAP-shaped workspace train
        # can ingest, instead of running a real feature-based
        # solver on a small handful of phone frames (which usually
        # fails).
        backend = "arcore_native"
    elif settings.sfm_backend != "none":
        backend = settings.sfm_backend
    else:
        backend = None

    if backend is not None:
        sfm = await store.enqueue_job(
            scene_id, JobKind.sfm, payload={"backend": backend}
        )
        job_ids.append(sfm.id)

    train = await store.enqueue_job(
        scene_id, JobKind.train, payload={"iters": settings.train_iters}
    )
    job_ids.append(train.id)

    export = await store.enqueue_job(
        scene_id,
        JobKind.export,
        payload={"formats": ["ply", "spz"]},
    )
    job_ids.append(export.id)

    # Always enqueue mesh — runner short-circuits in PR #1.
    mesh = await store.enqueue_job(scene_id, JobKind.mesh, payload={"deferred": True})
    job_ids.append(mesh.id)

    return job_ids

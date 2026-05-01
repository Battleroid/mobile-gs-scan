"""Decide which jobs to enqueue when a capture is finalized.

Mobile native → has ARCore poses → skip SfM, go straight to train.
Mobile web / drag-drop → no poses → SfM first.

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

    if not has_pose and settings.sfm_backend != "none":
        sfm = await store.enqueue_job(
            scene_id, JobKind.sfm, payload={"backend": settings.sfm_backend}
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

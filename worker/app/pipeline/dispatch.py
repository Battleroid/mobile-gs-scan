"""Decide which jobs to enqueue when a capture is finalized.

The pipeline shape today is: extract → sfm → train → export. The
front of the pipeline branches on input shape:

  * Image-set upload — ``capture_dir/frames/`` is already populated
    by the api ``/upload`` route. ``extract`` enqueues but is a
    no-op (it sees no source video and returns immediately).
  * Video upload — ``capture_dir/source/<file>`` carries the raw
    video; ``extract`` runs ffmpeg → frames at the user's chosen
    fps + jpeg quality, then SfM picks up from there.
  * Trusted-pose capture (``has_pose=True``) — SfM uses the
    ``arcore_native`` backend to lift ``poses.jsonl`` into a
    nerfstudio workspace without running real glomap/colmap.
  * Untrusted-pose capture — real SfM via ``settings.sfm_backend``
    (glomap default, colmap fallback).

We always enqueue extract + sfm so downstream steps can rely on
the scene_dir/sfm/ shape regardless of input kind.

Per-capture training-iter override: the train job's iters payload
is taken from ``capture.meta['train_iters']`` when the capture's
meta dict carries a positive int there. The web upload form and
the Android client both populate this. Falls back to
``settings.train_iters`` (env ``GS_TRAIN_ITERS``) otherwise.

Mesh extraction is on-demand (post-export, user-triggered) — see
the api ``/api/scenes/{id}/mesh`` endpoint, not enqueued here.
"""
from __future__ import annotations

import logging

from app.config import get_settings
from app.jobs import store
from app.jobs.schema import CaptureSource, JobKind

log = logging.getLogger(__name__)


async def enqueue_pipeline(
    scene_id: str,
    *,
    has_pose: bool,
    source: CaptureSource,
) -> list[str]:
    """Enqueue the extract → sfm → train → export pipeline for a
    scene. Returns the list of enqueued job ids, or an empty list
    if the scene has been deleted out from under us between the
    caller's reference and the first INSERT (e.g. a concurrent
    ``DELETE /api/captures/{id}`` cascade between ``finalize_capture``'s
    ``create_scene`` and this call).

    Each ``enqueue_job`` is atomic via ``INSERT ... WHERE EXISTS``
    on the scenes table, so a None at any step means the scene is
    definitively gone — bail out, return what we have. The caller
    treats an empty list as "race lost" and surfaces a 404 rather
    than enqueueing orphan jobs that workers would later fail with
    ``scene vanished``.
    """
    settings = get_settings()
    job_ids: list[str] = []

    extract_payload = await _build_extract_payload(scene_id)
    extract = await store.enqueue_job(
        scene_id, JobKind.extract, payload=extract_payload,
    )
    if extract is None:
        log.info("scene %s vanished before pipeline enqueue; bailing", scene_id)
        return job_ids
    job_ids.append(extract.id)

    backend: str | None
    if has_pose:
        backend = "arcore_native"
    elif settings.sfm_backend != "none":
        backend = settings.sfm_backend
    else:
        backend = None

    if backend is not None:
        sfm = await store.enqueue_job(
            scene_id, JobKind.sfm, payload={"backend": backend}
        )
        if sfm is None:
            return job_ids
        job_ids.append(sfm.id)

    train_iters = await _resolve_train_iters(scene_id, settings.train_iters)
    train = await store.enqueue_job(
        scene_id, JobKind.train, payload={"iters": train_iters}
    )
    if train is None:
        return job_ids
    job_ids.append(train.id)

    export = await store.enqueue_job(
        scene_id,
        JobKind.export,
        payload={"formats": ["ply", "spz"]},
    )
    if export is None:
        return job_ids
    job_ids.append(export.id)

    return job_ids


async def _build_extract_payload(scene_id: str) -> dict:
    """Pull the user-supplied extract knobs out of capture.meta.

    Both fields are optional. The extract step has its own defaults
    + clamps; we just shuttle the meta values through so the worker
    job payload is self-contained (no extra DB read at runtime).
    """
    payload: dict = {}
    scene = await store.get_scene(scene_id)
    if scene is None:
        return payload
    cap = await store.get_capture(scene.capture_id)
    if cap is None or not isinstance(cap.meta, dict):
        return payload
    fps = cap.meta.get("extract_fps")
    if isinstance(fps, (int, float)) and fps > 0:
        payload["extract_fps"] = float(fps)
    q = cap.meta.get("jpeg_quality")
    if isinstance(q, int) and 1 <= q <= 100:
        payload["jpeg_quality"] = q
    return payload


async def _resolve_train_iters(scene_id: str, fallback: int) -> int:
    """Look up the capture's meta dict for a per-capture iter
    override. Validates that the value is a positive int; on any
    weirdness (missing, wrong type, non-positive) silently falls
    back to the server-wide default so a malformed meta doesn't
    block training.
    """
    scene = await store.get_scene(scene_id)
    if scene is None:
        return fallback
    cap = await store.get_capture(scene.capture_id)
    if cap is None or not isinstance(cap.meta, dict):
        return fallback
    raw = cap.meta.get("train_iters")
    if not isinstance(raw, int) or raw <= 0:
        return fallback
    log.info(
        "using per-capture train_iters=%d (fallback was %d)", raw, fallback,
    )
    return raw

"""Worker-side job runner.

Polls the api for queued jobs in the kinds it can handle, claims the
oldest, runs the right pipeline step, posts heartbeats + progress
events, and writes the result back. One process per worker container.
"""
from __future__ import annotations

import asyncio
import logging
import os
import socket
import traceback
from pathlib import Path

from app.config import Settings, get_settings
from app.jobs import events, store
from app.jobs.schema import (
    CaptureStatus,
    EditStatus,
    Job,
    JobKind,
    JobStatus,
    Scene,
)
from app.pipeline import export as export_step
from app.pipeline import mesh as mesh_step
from app.pipeline import sfm as sfm_step
from app.pipeline import train as train_step
# filter is imported lazily inside _run_filter — it needs plyfile, which
# only exists in the worker-gs image, not the api image.

log = logging.getLogger(__name__)

POLL_INTERVAL = 1.5
HEARTBEAT_INTERVAL = 5.0
REAP_INTERVAL = 30.0


def _worker_id() -> str:
    return f"{socket.gethostname()}.{os.getpid()}"


async def run_forever(settings: Settings | None = None) -> None:
    settings = settings or get_settings()
    worker = _worker_id()
    log.info("worker %s starting (class=%s)", worker, settings.worker_class)

    kinds_for_class: dict[str, list[JobKind]] = {
        "gs": [
            JobKind.sfm,
            JobKind.train,
            JobKind.export,
            JobKind.mesh,
            JobKind.filter,
        ],
    }
    kinds = kinds_for_class.get(settings.worker_class)
    if not kinds:
        raise RuntimeError(f"unknown worker class {settings.worker_class!r}")

    reaper = asyncio.create_task(_reap_loop())

    try:
        while True:
            job = await store.claim_next_job(worker_id=worker, kinds=kinds)
            if job is None:
                await asyncio.sleep(POLL_INTERVAL)
                continue
            try:
                await _run_one(job, settings)
            except Exception as exc:  # noqa: BLE001
                tb = traceback.format_exc()
                log.exception("worker %s job=%s crashed", worker, job.id)
                await store.update_job(
                    job.id,
                    status=JobStatus.failed,
                    completed=True,
                    error=f"{exc}\n{tb}",
                )
                await events.publish_job(job.id, "job.failed", error=str(exc))
                if job.kind == JobKind.filter:
                    # Filter failures are scoped to the edit — leave the
                    # underlying scene/capture in their (completed) state.
                    await store.update_scene(
                        job.scene_id,
                        edit_status=EditStatus.failed,
                        edit_error=str(exc),
                    )
                    await events.publish_scene(
                        job.scene_id,
                        "scene.edit_failed",
                        error=str(exc),
                    )
                    continue
                scene = await store.get_scene(job.scene_id)
                if scene:
                    await store.update_scene(scene.id, status=CaptureStatus.failed)
                    await events.publish_scene(scene.id, "scene.failed", job_kind=job.kind.value)
                    cap = await store.get_capture(scene.capture_id)
                    if cap:
                        await store.set_capture_status(
                            cap.id, CaptureStatus.failed, error=str(exc)
                        )
    finally:
        reaper.cancel()


async def _reap_loop() -> None:
    while True:
        try:
            n = await store.reap_stale_jobs()
            if n:
                log.warning("reaped %d stale jobs", n)
        except Exception:
            log.exception("reap loop error")
        await asyncio.sleep(REAP_INTERVAL)


async def _run_one(job: Job, settings: Settings) -> None:
    log.info("job %s claimed (kind=%s scene=%s)", job.id, job.kind.value, job.scene_id)
    await store.update_job(
        job.id, status=JobStatus.running, started=True, heartbeat=True
    )
    await events.publish_job(job.id, "job.running")
    scene = await store.get_scene(job.scene_id)
    if scene is None:
        raise RuntimeError("scene vanished")

    capture = await store.get_capture(scene.capture_id)
    if capture is None:
        raise RuntimeError("capture vanished")

    if job.kind == JobKind.filter:
        await _run_filter(job=job, scene=scene, settings=settings)
        return

    await store.update_scene(scene.id, status=CaptureStatus.processing)
    await store.set_capture_status(capture.id, CaptureStatus.processing)
    await events.publish_scene(scene.id, "scene.processing", job_kind=job.kind.value)

    capture_dir = settings.captures_dir() / capture.id
    scene_dir = settings.scenes_dir() / scene.id
    scene_dir.mkdir(parents=True, exist_ok=True)

    # Heartbeat side-channel — keeps `claimed_by` fresh while a long
    # subprocess runs. Cancelled when the job finishes either way.
    hb_task = asyncio.create_task(_heartbeat(job.id))

    async def progress(pct: float, msg: str) -> None:
        await store.update_job(
            job.id, progress=pct, progress_msg=msg, heartbeat=True
        )
        await events.publish_job(job.id, "job.progress", progress=pct, message=msg)

    try:
        result = await _dispatch(
            job=job, capture_dir=capture_dir, scene_dir=scene_dir, progress=progress
        )
    finally:
        hb_task.cancel()

    await store.update_job(
        job.id,
        status=JobStatus.completed,
        progress=1.0,
        progress_msg="done",
        completed=True,
        result=result,
    )
    await events.publish_job(job.id, "job.completed", result=result)

    if job.kind == JobKind.export:
        ply = result.get("ply")
        spz = result.get("spz")
        if ply or spz:
            await store.update_scene(
                scene.id,
                ply_path=ply,
                spz_path=spz,
            )
        await _maybe_finalize_scene(scene)


async def _dispatch(
    *,
    job: Job,
    capture_dir: Path,
    scene_dir: Path,
    progress,
) -> dict:
    if job.kind == JobKind.sfm:
        return await sfm_step.run_sfm(
            capture_dir=capture_dir,
            scene_dir=scene_dir,
            backend=str(job.payload.get("backend", "glomap")),
            progress=progress,
        )
    if job.kind == JobKind.train:
        return await train_step.run_train(
            scene_dir=scene_dir,
            iters=int(job.payload.get("iters", 15000)),
            progress=progress,
        )
    if job.kind == JobKind.export:
        return await export_step.run_export(
            scene_dir=scene_dir,
            formats=list(job.payload.get("formats", ["ply", "spz"])),
            progress=progress,
        )
    if job.kind == JobKind.mesh:
        return await mesh_step.run_mesh(
            scene_dir=scene_dir,
            deferred=bool(job.payload.get("deferred", True)),
            progress=progress,
        )
    raise RuntimeError(f"no handler for {job.kind}")


async def _heartbeat(job_id: str) -> None:
    while True:
        try:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            await store.update_job(job_id, heartbeat=True)
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("heartbeat for %s failed", job_id)


async def _run_filter(*, job: Job, scene: Scene, settings: Settings) -> None:
    """Apply a filter recipe to an existing scene's .ply.

    The scene must already have a baseline ply_path. We don't touch the
    capture / scene status — the underlying scene is still completed,
    only edit_status moves.
    """
    from app.pipeline import filter as filter_step

    if not scene.ply_path:
        raise RuntimeError("scene has no source ply to filter")
    recipe = scene.edit_recipe or job.payload.get("recipe")
    if not recipe:
        raise RuntimeError("filter job has no recipe attached")

    await store.update_scene(scene.id, edit_status=EditStatus.running, edit_error=None)
    await events.publish_scene(scene.id, "scene.edit_running")

    scene_dir = settings.scenes_dir() / scene.id
    out_dir = scene_dir / "edit"
    out_dir.mkdir(parents=True, exist_ok=True)

    hb_task = asyncio.create_task(_heartbeat(job.id))

    async def progress(pct: float, msg: str) -> None:
        await store.update_job(
            job.id, progress=pct, progress_msg=msg, heartbeat=True
        )
        await events.publish_job(job.id, "job.progress", progress=pct, message=msg)

    try:
        result = await filter_step.filter_splat(
            src_ply=Path(scene.ply_path),
            out_dir=out_dir,
            recipe=recipe,
            progress=progress,
        )
    finally:
        hb_task.cancel()

    edited_ply = str(result["ply"])
    edited_spz = str(result["spz"]) if "spz" in result else None
    await store.update_scene(
        scene.id,
        edited_ply_path=edited_ply,
        edited_spz_path=edited_spz,
        edit_status=EditStatus.completed,
        edit_error=None,
    )
    await store.update_job(
        job.id,
        status=JobStatus.completed,
        progress=1.0,
        progress_msg=f"done ({result.get('kept')}/{result.get('total')})",
        completed=True,
        result={
            "ply": edited_ply,
            "spz": edited_spz,
            "kept": int(result.get("kept", 0)),
            "total": int(result.get("total", 0)),
        },
    )
    await events.publish_job(job.id, "job.completed", result=result)
    await events.publish_scene(
        scene.id,
        "scene.edited",
        edited_ply_url=f"/api/scenes/{scene.id}/artifacts/ply?edit=true",
        edited_spz_url=(
            f"/api/scenes/{scene.id}/artifacts/spz?edit=true" if edited_spz else None
        ),
        kept=int(result.get("kept", 0)),
        total=int(result.get("total", 0)),
    )


async def _maybe_finalize_scene(scene: Scene) -> None:
    """Mark scene + capture completed if every job is done."""
    jobs = await store.list_jobs_for_scene(scene.id)
    if any(j.status not in (JobStatus.completed, JobStatus.canceled) for j in jobs):
        return
    if any(j.status == JobStatus.failed for j in jobs):
        return
    await store.update_scene(scene.id, status=CaptureStatus.completed)
    await events.publish_scene(scene.id, "scene.completed")
    cap = await store.get_capture(scene.capture_id)
    if cap:
        await store.set_capture_status(cap.id, CaptureStatus.completed)

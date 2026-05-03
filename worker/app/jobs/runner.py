"""Worker-side job runner.

Polls the api for queued jobs in the kinds it can handle, claims the
oldest, runs the right pipeline step, posts heartbeats + progress
events, and writes the result back. One process per worker container.

Cancellation: when the api flips a job to ``status=canceled`` (via
POST /api/jobs/{id}/cancel or DELETE /api/captures/{id}), the
heartbeat task here notices on its next cycle, calls
``_running.kill_for_job(job_id)`` to SIGKILL the running
subprocess (if the step has spawned one), and cancels the
dispatch coroutine itself. The resulting exception is caught in
the outer try/except in run_forever; we check the DB row and
treat ``status=canceled`` as a clean cancellation rather than a
crash.
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
    MeshStatus,
    Scene,
)
from app.pipeline import _running
from app.pipeline import export as export_step
from app.pipeline import filter as filter_step
from app.pipeline import mesh as mesh_step
from app.pipeline import sfm as sfm_step
from app.pipeline import train as train_step

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
            except asyncio.CancelledError:
                # Heartbeat fired dispatch_task.cancel() because the
                # DB row is canceled. Treat as user-requested cancel
                # and continue. If DB doesn't say canceled then this
                # was a genuine outer-task shutdown — propagate.
                if not await _ack_user_cancel(job):
                    raise
            except Exception as exc:  # noqa: BLE001
                # Subprocess kill from heartbeat surfaces here as
                # RuntimeError("ns-train exited 9, ..."). If the
                # DB row says canceled, that's the same user-cancel
                # path; otherwise it's a real crash.
                if await _ack_user_cancel(job):
                    continue
                tb = traceback.format_exc()
                log.exception("worker %s job=%s crashed", worker, job.id)
                await store.update_job(
                    job.id,
                    status=JobStatus.failed,
                    completed=True,
                    error=f"{exc}\n{tb}",
                )
                await events.publish_job(job.id, "job.failed", error=str(exc))
                # Filter / mesh are post-processing on an already-
                # completed scene; a failure in either should NOT
                # demote the scene / capture back to ``failed``.
                # The respective _run_filter / _run_mesh helpers
                # have already updated edit_/mesh_status + error.
                if job.kind in (JobKind.filter, JobKind.mesh):
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


async def _ack_user_cancel(job: Job) -> bool:
    """If the job's DB row is in canceled state, mark completed_at
    so the reaper doesn't cycle it, publish the canceled event,
    and return True. Otherwise return False so the caller falls
    through to the genuine-failure path.
    """
    refreshed = await store.get_job(job.id)
    if refreshed is None or refreshed.status != JobStatus.canceled:
        return False
    log.info("job %s canceled by user", job.id)
    await store.update_job(job.id, completed=True)
    await events.publish_job(job.id, "job.canceled")
    return True


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
        # Filter jobs are post-processing on an already-completed
        # scene; we don't churn the scene/capture status. Edit
        # progress lives on the dedicated edit_status column.
        await _run_filter(job=job, scene=scene, settings=settings)
        return

    if job.kind == JobKind.mesh:
        # Mesh extraction is on-demand and runs against the trained
        # checkpoint, not the live pipeline. Same independent-status
        # treatment as filter so a failed mesh doesn't demote the
        # scene back to ``failed``.
        await _run_mesh(job=job, scene=scene, settings=settings)
        return

    await store.update_scene(scene.id, status=CaptureStatus.processing)
    await store.set_capture_status(capture.id, CaptureStatus.processing)
    await events.publish_scene(scene.id, "scene.processing", job_kind=job.kind.value)

    capture_dir = settings.captures_dir() / capture.id
    scene_dir = settings.scenes_dir() / scene.id
    scene_dir.mkdir(parents=True, exist_ok=True)

    async def progress(pct: float, msg: str) -> None:
        await store.update_job(
            job.id, progress=pct, progress_msg=msg, heartbeat=True
        )
        await events.publish_job(job.id, "job.progress", progress=pct, message=msg)

    # Dispatch runs in a child task so the heartbeat task can
    # cancel it on user-requested cancellation.
    dispatch_task = asyncio.create_task(
        _dispatch(
            job=job,
            capture_dir=capture_dir,
            scene_dir=scene_dir,
            progress=progress,
        )
    )
    hb_task = asyncio.create_task(_heartbeat(job.id, dispatch_task))

    try:
        result = await dispatch_task
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

    # Always check scene finalization after a successful job. The
    # previous "only after export" path was wrong: dispatch order is
    # sfm → train → export → mesh, so when export completes the mesh
    # job is still queued and _maybe_finalize_scene returns early
    # (some-job-not-completed). Mesh then completes but nothing
    # re-triggers the check, leaving the scene + capture stuck at
    # `processing` forever — which also kept the web viewer hidden
    # since it conditions on scene.status == "completed".
    await _maybe_finalize_scene(scene)


async def _run_filter(*, job: Job, scene: Scene, settings: Settings) -> None:
    """Run the filter step on an already-exported scene.

    Maintains ``Scene.edit_status`` + ``edit_error`` independently
    of the main pipeline status — a failed filter never demotes a
    completed scene back to ``failed``. On success writes the edited
    artifact paths to the scene row so the api download endpoint
    can serve them.
    """
    src_ply = scene.ply_path
    if not src_ply or not Path(src_ply).exists():
        raise RuntimeError("scene has no source .ply to filter")

    recipe = scene.edit_recipe or {"ops": []}

    await store.update_scene(scene.id, edit_status=EditStatus.running, edit_error=None)
    await events.publish_scene(scene.id, "scene.edit_running")

    edit_dir = settings.scenes_dir() / scene.id / "edit"

    async def progress(pct: float, msg: str) -> None:
        await store.update_job(
            job.id, progress=pct, progress_msg=msg, heartbeat=True
        )
        await events.publish_job(job.id, "job.progress", progress=pct, message=msg)
        # Also surface on the scene topic — the web client opened
        # its scene WS before the filter job existed, so it has no
        # per-job subscription to listen on. Mirroring on the scene
        # topic gives it a live progress stream without needing a
        # WS reconnect dance.
        await events.publish_scene(
            scene.id, "scene.edit_progress", progress=pct, message=msg,
        )

    dispatch_task = asyncio.create_task(
        filter_step.filter_splat(
            src_ply=Path(src_ply),
            out_dir=edit_dir,
            recipe=recipe,
            progress=progress,
            job_id=job.id,
        )
    )
    hb_task = asyncio.create_task(_heartbeat(job.id, dispatch_task))
    try:
        try:
            result = await dispatch_task
        finally:
            hb_task.cancel()
    except (asyncio.CancelledError, Exception) as exc:  # noqa: BLE001
        # User cancel path: leave edit_status alone if cancelled (the
        # outer run_forever handles _ack_user_cancel) — otherwise mark
        # failed so the UI surfaces the recipe error.
        if await _ack_user_cancel(job):
            await store.update_scene(scene.id, edit_status=EditStatus.none)
            return
        msg = f"{exc}"
        await store.update_scene(
            scene.id, edit_status=EditStatus.failed, edit_error=msg,
        )
        await events.publish_scene(scene.id, "scene.edit_failed", error=msg)
        raise

    # Re-check the job's row before committing the result: a fast
    # filter (≤ 5 s) can finish before the heartbeat loop notices a
    # PUT-replace or DELETE-discard, in which case `result` is stale
    # data from a recipe the user has already abandoned. Acking the
    # cancel here keeps the next-queued filter job authoritative
    # rather than racing with our late write. Edit artifacts are
    # left on disk; the discard / replace endpoint cleans them up
    # (DELETE) or the next job overwrites them (PUT).
    refreshed = await store.get_job(job.id)
    if refreshed is not None and refreshed.status == JobStatus.canceled:
        log.info(
            "filter %s finished but DB row is canceled; skipping commit",
            job.id,
        )
        await store.update_job(job.id, completed=True)
        await events.publish_job(job.id, "job.canceled")
        # Don't touch edit_status — the cancel path on the api side
        # has already moved it to its next state (queued for the
        # replacement, or none for the discard).
        return

    await store.update_scene(
        scene.id,
        edited_ply_path=str(result.get("ply")) if result.get("ply") else None,
        edited_spz_path=str(result.get("spz")) if result.get("spz") else None,
        edit_status=EditStatus.completed,
        edit_error=None,
    )
    await store.update_job(
        job.id,
        status=JobStatus.completed,
        progress=1.0,
        progress_msg="done",
        completed=True,
        result=result,
    )
    await events.publish_job(job.id, "job.completed", result=result)
    await events.publish_scene(
        scene.id, "scene.edited",
        kept=result.get("kept"),
        total=result.get("total"),
    )


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
            job_id=job.id,
        )
    if job.kind == JobKind.export:
        return await export_step.run_export(
            scene_dir=scene_dir,
            formats=list(job.payload.get("formats", ["ply", "spz"])),
            progress=progress,
            job_id=job.id,
        )
    raise RuntimeError(f"no handler for {job.kind}")


async def _run_mesh(*, job: Job, scene: Scene, settings: Settings) -> None:
    """Run on-demand Poisson mesh extraction.

    Mirrors `_run_filter` in spirit: independent status column
    (``mesh_status``), failures don't demote the scene, progress is
    mirrored on the scene topic so a web client whose snapshot
    pre-dates the job still sees live updates.
    """
    scene_dir = settings.scenes_dir() / scene.id
    train_dir = scene_dir / "train"
    if not train_dir.exists():
        raise RuntimeError("scene has no train/ output to mesh")

    params = scene.mesh_params or {}

    await store.update_scene(scene.id, mesh_status=MeshStatus.running, mesh_error=None)
    await events.publish_scene(scene.id, "scene.mesh_running")

    async def progress(pct: float, msg: str) -> None:
        await store.update_job(
            job.id, progress=pct, progress_msg=msg, heartbeat=True
        )
        await events.publish_job(job.id, "job.progress", progress=pct, message=msg)
        # Same scene-topic mirror as _run_filter — mesh is enqueued
        # mid-session so the per-job WS subscription set up at
        # snapshot time can't carry its events.
        await events.publish_scene(
            scene.id, "scene.mesh_progress", progress=pct, message=msg,
        )

    dispatch_task = asyncio.create_task(
        mesh_step.run_mesh(
            scene_dir=scene_dir,
            params=params,
            progress=progress,
            job_id=job.id,
        )
    )
    hb_task = asyncio.create_task(_heartbeat(job.id, dispatch_task))
    try:
        try:
            result = await dispatch_task
        finally:
            hb_task.cancel()
    except (asyncio.CancelledError, Exception) as exc:  # noqa: BLE001
        if await _ack_user_cancel(job):
            # Only clear mesh_status if it's still "running". If a
            # replacement POST /mesh has raced ahead and flipped it
            # to "queued", clobbering back to "none" would hide that
            # a fresh extraction is pending — re-fetch to catch the
            # race rather than blindly writing.
            refreshed = await store.get_scene(scene.id)
            if refreshed and refreshed.mesh_status == MeshStatus.running:
                await store.update_scene(scene.id, mesh_status=MeshStatus.none)
            return
        msg = f"{exc}"
        await store.update_scene(
            scene.id, mesh_status=MeshStatus.failed, mesh_error=msg,
        )
        await events.publish_scene(scene.id, "scene.mesh_failed", error=msg)
        raise

    refreshed = await store.get_job(job.id)
    if refreshed is not None and refreshed.status == JobStatus.canceled:
        log.info(
            "mesh %s finished but DB row is canceled; skipping commit",
            job.id,
        )
        await store.update_job(job.id, completed=True)
        await events.publish_job(job.id, "job.canceled")
        return

    await store.update_scene(
        scene.id,
        mesh_obj_path=str(result.get("obj")) if result.get("obj") else None,
        mesh_glb_path=str(result.get("glb")) if result.get("glb") else None,
        mesh_status=MeshStatus.completed,
        mesh_error=None,
    )
    await store.update_job(
        job.id,
        status=JobStatus.completed,
        progress=1.0,
        progress_msg="done",
        completed=True,
        result=result,
    )
    await events.publish_job(job.id, "job.completed", result=result)
    await events.publish_scene(scene.id, "scene.meshed", obj=result.get("obj"))


async def _heartbeat(job_id: str, dispatch_task: asyncio.Task) -> None:
    """Heartbeat task: keeps claimed_by fresh AND watches for user
    cancellation. On cancel, kills any registered subprocess for
    the job and cancels the dispatch coroutine.
    """
    while True:
        try:
            await asyncio.sleep(HEARTBEAT_INTERVAL)
            await store.update_job(job_id, heartbeat=True)
            j = await store.get_job(job_id)
            if j is not None and j.status == JobStatus.canceled:
                log.info(
                    "job %s canceled — killing subprocess + dispatch task",
                    job_id,
                )
                # SIGKILL the subprocess if a step has spawned one;
                # subprocess death will propagate as RuntimeError
                # through the step's `await proc.wait()`. Also
                # cancel the dispatch coroutine for the case where
                # we're between subprocesses (setup / teardown,
                # arcore conversion, etc) so the cancel takes
                # effect even when no proc is registered.
                _running.kill_for_job(job_id)
                dispatch_task.cancel()
                return
        except asyncio.CancelledError:
            return
        except Exception:
            log.exception("heartbeat for %s failed", job_id)


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

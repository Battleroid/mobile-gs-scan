"""Scene HTTP + event-WebSocket routes."""
from __future__ import annotations

import asyncio
import json
import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.jobs import events, store
from app.jobs.schema import EditStatus, JobKind, JobStatus, Scene

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/scenes", tags=["scenes"])


class JobView(BaseModel):
    id: str
    kind: str
    status: str
    progress: float
    progress_msg: str | None
    error: str | None


class SceneView(BaseModel):
    id: str
    capture_id: str
    status: str
    error: str | None
    ply_url: str | None
    spz_url: str | None
    jobs: list[JobView]
    created_at: str
    completed_at: str | None
    edited_ply_url: str | None = None
    edited_spz_url: str | None = None
    edit_status: str = "none"
    edit_error: str | None = None
    edit_recipe: dict | None = None


async def _to_view(scene: Scene) -> SceneView:
    jobs = await store.list_jobs_for_scene(scene.id)
    return SceneView(
        id=scene.id,
        capture_id=scene.capture_id,
        status=scene.status.value,
        error=scene.error,
        ply_url=f"/api/scenes/{scene.id}/artifacts/ply" if scene.ply_path else None,
        spz_url=f"/api/scenes/{scene.id}/artifacts/spz" if scene.spz_path else None,
        jobs=[
            JobView(
                id=j.id,
                kind=j.kind.value,
                status=j.status.value,
                progress=j.progress,
                progress_msg=j.progress_msg,
                error=j.error,
            )
            for j in jobs
        ],
        created_at=scene.created_at.isoformat(),
        completed_at=scene.completed_at.isoformat() if scene.completed_at else None,
        edited_ply_url=(
            f"/api/scenes/{scene.id}/artifacts/ply?edit=true"
            if scene.edited_ply_path else None
        ),
        edited_spz_url=(
            f"/api/scenes/{scene.id}/artifacts/spz?edit=true"
            if scene.edited_spz_path else None
        ),
        edit_status=scene.edit_status.value,
        edit_error=scene.edit_error,
        edit_recipe=scene.edit_recipe,
    )


@router.get("/{scene_id}")
async def get_scene(scene_id: str) -> SceneView:
    scene = await store.get_scene(scene_id)
    if scene is None:
        raise HTTPException(404, "scene not found")
    return await _to_view(scene)


@router.get("/{scene_id}/artifacts/{kind}")
async def download_artifact(scene_id: str, kind: str, edit: bool = False) -> Any:
    """Download the original splat (default) or its edited copy.

    `edit=true` switches to the filter pipeline's output. 404 if the
    scene has no edit yet.
    """
    scene = await store.get_scene(scene_id)
    if scene is None:
        raise HTTPException(404, "scene not found")
    path: str | None
    if kind == "ply":
        path = scene.edited_ply_path if edit else scene.ply_path
        filename = "scene-edit.ply" if edit else "scene.ply"
    elif kind == "spz":
        path = scene.edited_spz_path if edit else scene.spz_path
        filename = "scene-edit.spz" if edit else "scene.spz"
    else:
        raise HTTPException(400, f"unknown artifact kind {kind!r}")
    if not path or not Path(path).exists():
        which = "edited " if edit else ""
        raise HTTPException(404, f"{which}artifact {kind!r} not yet produced")
    return FileResponse(path, media_type="application/octet-stream", filename=filename)


class EditRequest(BaseModel):
    recipe: dict


@router.put("/{scene_id}/edit")
async def upsert_edit(scene_id: str, body: EditRequest) -> SceneView:
    """Replace the scene's edit recipe and (re)enqueue a filter job.

    Idempotent at the recipe level. If a filter job is currently
    queued/running for this scene, we mark it canceled and start a
    fresh one. The worker is single-threaded per class so a running
    job will still finish its current run, but its result is overwritten
    by the newly enqueued one as soon as it lands.
    """
    scene = await store.get_scene(scene_id)
    if scene is None:
        raise HTTPException(404, "scene not found")
    if not scene.ply_path:
        raise HTTPException(400, "scene has no source ply yet")
    # Validate the recipe up front so a malformed PUT doesn't queue
    # a doomed job. Lazy import — keeps the api container free of plyfile.
    try:
        from app.pipeline import filter as filter_step

        filter_step.validate_recipe(body.recipe)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except ImportError:
        # api container doesn't ship plyfile — skip pre-flight, the
        # worker-gs container will still validate at run time.
        pass

    # Cancel any in-flight filter job for this scene.
    for j in await store.list_jobs_for_scene(scene_id):
        if j.kind == JobKind.filter and j.status in (
            JobStatus.queued,
            JobStatus.claimed,
            JobStatus.running,
        ):
            await store.update_job(
                j.id,
                status=JobStatus.canceled,
                completed=True,
                error="superseded by a new edit recipe",
            )
            await events.publish_job(j.id, "job.canceled")

    await store.update_scene(
        scene_id,
        edit_recipe=body.recipe,
        edit_status=EditStatus.queued,
        edit_error=None,
    )
    await store.enqueue_job(scene_id, JobKind.filter, payload={"recipe": body.recipe})
    await events.publish_scene(scene_id, "scene.edit_queued")
    refreshed = await store.get_scene(scene_id)
    return await _to_view(refreshed)  # type: ignore[arg-type]


@router.delete("/{scene_id}/edit")
async def clear_edit(scene_id: str) -> SceneView:
    """Discard the scene's edit. Cancels any pending filter job, deletes
    the edited artifacts, and nulls the recipe."""
    scene = await store.get_scene(scene_id)
    if scene is None:
        raise HTTPException(404, "scene not found")

    for j in await store.list_jobs_for_scene(scene_id):
        if j.kind == JobKind.filter and j.status in (
            JobStatus.queued,
            JobStatus.claimed,
            JobStatus.running,
        ):
            await store.update_job(
                j.id,
                status=JobStatus.canceled,
                completed=True,
                error="edit discarded",
            )
            await events.publish_job(j.id, "job.canceled")

    for p in (scene.edited_ply_path, scene.edited_spz_path):
        if p:
            try:
                Path(p).unlink(missing_ok=True)
            except OSError as exc:
                log.warning("failed to delete edit artifact %s: %s", p, exc)

    await store.update_scene(
        scene_id,
        edited_ply_path=None,
        edited_spz_path=None,
        edit_recipe=None,
        edit_status=EditStatus.none,
        edit_error=None,
    )
    await events.publish_scene(scene_id, "scene.edit_cleared")
    refreshed = await store.get_scene(scene_id)
    return await _to_view(refreshed)  # type: ignore[arg-type]


@router.websocket("/{scene_id}/events")
async def scene_events_endpoint(ws: WebSocket, scene_id: str) -> None:
    scene = await store.get_scene(scene_id)
    if scene is None:
        await ws.close(code=4404, reason="scene not found")
        return
    await ws.accept()

    topic = f"scene.{scene.id}"
    queue = await events.subscribe(topic)
    job_queues = []
    try:
        view = await _to_view(scene)
        await ws.send_text(
            json.dumps({"topic": topic, "kind": "snapshot", "data": view.model_dump()})
        )
        for j in await store.list_jobs_for_scene(scene.id):
            job_topic = f"job.{j.id}"
            jq = await events.subscribe(job_topic)
            job_queues.append((job_topic, jq))

        async def fwd(q):
            while True:
                evt = await q.get()
                await ws.send_text(evt.to_json())

        tasks = [asyncio.create_task(fwd(queue))] + [
            asyncio.create_task(fwd(jq)) for _, jq in job_queues
        ]
        try:
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_EXCEPTION
            )
            for t in pending:
                t.cancel()
        except WebSocketDisconnect:
            pass
    finally:
        await events.unsubscribe(topic, queue)
        for jt, jq in job_queues:
            await events.unsubscribe(jt, jq)

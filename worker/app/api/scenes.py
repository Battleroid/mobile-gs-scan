"""Scene HTTP + event-WebSocket routes."""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.config import get_settings
from app.jobs import events, store
from app.jobs.schema import EditStatus, JobKind, JobStatus, Scene
from app.pipeline.filter import validate_recipe

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
    edited_ply_url: str | None
    edited_spz_url: str | None
    edit_status: str
    edit_error: str | None
    edit_recipe: dict | None
    jobs: list[JobView]
    created_at: str
    completed_at: str | None


async def _to_view(scene: Scene) -> SceneView:
    jobs = await store.list_jobs_for_scene(scene.id)
    edit_status = (
        scene.edit_status.value
        if scene.edit_status is not None
        else EditStatus.none.value
    )
    return SceneView(
        id=scene.id,
        capture_id=scene.capture_id,
        status=scene.status.value,
        error=scene.error,
        ply_url=f"/api/scenes/{scene.id}/artifacts/ply" if scene.ply_path else None,
        spz_url=f"/api/scenes/{scene.id}/artifacts/spz" if scene.spz_path else None,
        edited_ply_url=(
            f"/api/scenes/{scene.id}/artifacts/ply?edit=true"
            if scene.edited_ply_path
            else None
        ),
        edited_spz_url=(
            f"/api/scenes/{scene.id}/artifacts/spz?edit=true"
            if scene.edited_spz_path
            else None
        ),
        edit_status=edit_status,
        edit_error=scene.edit_error,
        edit_recipe=scene.edit_recipe,
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
    )


@router.get("/{scene_id}")
async def get_scene(scene_id: str) -> SceneView:
    scene = await store.get_scene(scene_id)
    if scene is None:
        raise HTTPException(404, "scene not found")
    return await _to_view(scene)


@router.get("/{scene_id}/artifacts/{kind}")
async def download_artifact(scene_id: str, kind: str, edit: bool = False) -> Any:
    scene = await store.get_scene(scene_id)
    if scene is None:
        raise HTTPException(404, "scene not found")
    path: str | None
    if edit:
        if kind == "ply":
            path = scene.edited_ply_path
            filename = "scene-edited.ply"
        elif kind == "spz":
            path = scene.edited_spz_path
            filename = "scene-edited.spz"
        else:
            raise HTTPException(400, f"unknown artifact kind {kind!r}")
        if not path or not Path(path).exists():
            raise HTTPException(404, f"edited {kind!r} not yet produced")
    else:
        if kind == "ply":
            path = scene.ply_path
            filename = "scene.ply"
        elif kind == "spz":
            path = scene.spz_path
            filename = "scene.spz"
        else:
            raise HTTPException(400, f"unknown artifact kind {kind!r}")
        if not path or not Path(path).exists():
            raise HTTPException(404, f"artifact {kind!r} not yet produced")
    return FileResponse(path, media_type="application/octet-stream", filename=filename)


# ─── edit recipe (filter pipeline) ─────────────────


class EditRequest(BaseModel):
    """User-authored cleanup recipe. The shape is validated by
    ``app.pipeline.filter.validate_recipe`` — keeping the server-side
    schema check in one place so the worker and api stay aligned."""
    recipe: dict


@router.put("/{scene_id}/edit")
async def upsert_edit(scene_id: str, body: EditRequest) -> SceneView:
    """Replace the scene's edit recipe and (re)enqueue a filter job.

    Idempotent — re-PUTting cancels any in-flight filter job for this
    scene, swaps in the new recipe, and enqueues a fresh job. We
    DON'T touch the existing edited artifacts on disk yet; the new
    job overwrites them on success and leaves the previous edit
    accessible until then.
    """
    scene = await store.get_scene(scene_id)
    if scene is None:
        raise HTTPException(404, "scene not found")
    if not scene.ply_path:
        raise HTTPException(409, "scene has no source splat to edit yet")

    try:
        recipe = validate_recipe(body.recipe)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    # Cancel any in-flight filter job for this scene before queueing
    # a new one. Otherwise two concurrent filter jobs would race on
    # the same edit/ output dir.
    for j in await store.list_jobs_for_scene(scene.id):
        if j.kind != JobKind.filter:
            continue
        if j.status in (JobStatus.queued, JobStatus.claimed, JobStatus.running):
            if await store.cancel_job(j.id):
                await events.publish_job(j.id, "job.canceled")

    # Drop terminal filter rows so the scene's job list converges on
    # a single, canonical filter row rather than accumulating one per
    # apply. The web pipeline UI keys off scene.jobs and would
    # otherwise grow a "filter / filter / filter / …" list as the
    # user iterates on the recipe.
    await store.delete_terminal_jobs_of_kind(scene.id, JobKind.filter)

    await store.update_scene(
        scene.id,
        edit_recipe=recipe,
        edit_status=EditStatus.queued,
        edit_error=None,
    )
    job = await store.enqueue_job(scene.id, JobKind.filter, payload={})
    # Just the bare event — the client only uses scene.edit_queued
    # to flip its local edit_status and re-fetch the snapshot, so
    # broadcasting the recipe (which can be MB-scale once
    # keep_indices lands) is pure waste on the WS hot path.
    await events.publish_scene(
        scene.id, "scene.edit_queued", job_id=job.id,
    )

    refreshed = await store.get_scene(scene.id)
    assert refreshed is not None
    return await _to_view(refreshed)


@router.delete("/{scene_id}/edit")
async def clear_edit(scene_id: str) -> SceneView:
    """Cancel any in-flight filter job, remove the edit artifacts,
    and null the recipe. The original splat is untouched."""
    scene = await store.get_scene(scene_id)
    if scene is None:
        raise HTTPException(404, "scene not found")

    for j in await store.list_jobs_for_scene(scene.id):
        if j.kind != JobKind.filter:
            continue
        if j.status in (JobStatus.queued, JobStatus.claimed, JobStatus.running):
            if await store.cancel_job(j.id):
                await events.publish_job(j.id, "job.canceled")

    settings = get_settings()
    edit_dir = settings.scenes_dir() / scene.id / "edit"
    if edit_dir.exists():
        shutil.rmtree(edit_dir, ignore_errors=True)

    await store.update_scene(
        scene.id,
        edited_ply_path=None,
        edited_spz_path=None,
        edit_recipe=None,
        edit_status=EditStatus.none,
        edit_error=None,
    )
    await events.publish_scene(scene.id, "scene.edit_cleared")

    refreshed = await store.get_scene(scene.id)
    assert refreshed is not None
    return await _to_view(refreshed)


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

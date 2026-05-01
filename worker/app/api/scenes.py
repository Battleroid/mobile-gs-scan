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
from app.jobs.schema import Scene

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
    )


@router.get("/{scene_id}")
async def get_scene(scene_id: str) -> SceneView:
    scene = await store.get_scene(scene_id)
    if scene is None:
        raise HTTPException(404, "scene not found")
    return await _to_view(scene)


@router.get("/{scene_id}/artifacts/{kind}")
async def download_artifact(scene_id: str, kind: str) -> Any:
    scene = await store.get_scene(scene_id)
    if scene is None:
        raise HTTPException(404, "scene not found")
    path: str | None
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

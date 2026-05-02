"""Capture-session HTTP + WebSocket routes."""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path
from typing import Any

from fastapi import (
    APIRouter,
    File,
    HTTPException,
    Query,
    UploadFile,
    WebSocket,
    WebSocketDisconnect,
)
from pydantic import BaseModel, Field

from app import words
from app.config import get_settings
from app.jobs import events, store
from app.jobs.schema import Capture, CaptureSource, CaptureStatus, JobStatus
from app.pipeline.dispatch import enqueue_pipeline
from app.sessions.ingest import run_stream_session

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/captures", tags=["captures"])


# ─── DTOs ───────────────────────────────────────────


class CaptureCreate(BaseModel):
    # Optional now: when missing or blank, the server picks a
    # memorable random name (see app.words.random_name). This lets
    # the phone / web "+ new capture" flows skip the naming dialog
    # entirely; the user can rename later via PATCH.
    name: str | None = None
    source: CaptureSource = CaptureSource.mobile_native
    has_pose: bool = False
    meta: dict[str, Any] = {}


class CaptureRename(BaseModel):
    # Trimmed by the handler; must be non-empty after trim.
    name: str = Field(min_length=1, max_length=200)


class CaptureView(BaseModel):
    id: str
    name: str
    status: CaptureStatus
    source: CaptureSource
    pair_token: str | None
    pair_url: str | None
    frame_count: int
    dropped_count: int
    has_pose: bool
    meta: dict[str, Any]
    error: str | None
    scene_id: str | None
    created_at: str
    updated_at: str


def _to_view(cap: Capture, scene_id: str | None = None) -> CaptureView:
    pair_url = f"/m/{cap.pair_token}" if cap.pair_token else None
    return CaptureView(
        id=cap.id,
        name=cap.name,
        status=cap.status,
        source=cap.source,
        pair_token=cap.pair_token,
        pair_url=pair_url,
        frame_count=cap.frame_count,
        dropped_count=cap.dropped_count,
        has_pose=cap.has_pose,
        meta=cap.meta,
        error=cap.error,
        scene_id=scene_id,
        created_at=cap.created_at.isoformat(),
        updated_at=cap.updated_at.isoformat(),
    )


# ─── HTTP ───────────────────────────────────────────


@router.get("")
async def list_captures() -> list[CaptureView]:
    rows = await store.list_captures()
    out: list[CaptureView] = []
    for cap in rows:
        scene = await store.get_scene_for_capture(cap.id)
        out.append(_to_view(cap, scene_id=scene.id if scene else None))
    return out


@router.post("")
async def create_capture(body: CaptureCreate) -> CaptureView:
    # Strip + auto-name in one place so every entrypoint that
    # creates a capture (web new-capture button, phone-native
    # "+ new capture", future local-record upload flow) gets the
    # same default-name treatment without each having to mirror
    # the logic.
    name = (body.name or "").strip() or words.random_name()
    cap = await store.create_capture(
        name=name,
        source=body.source,
        has_pose=body.has_pose,
        meta=body.meta,
    )
    return _to_view(cap)


@router.get("/{capture_id}")
async def get_capture(capture_id: str) -> CaptureView:
    cap = await store.get_capture(capture_id)
    if cap is None:
        raise HTTPException(404, "capture not found")
    scene = await store.get_scene_for_capture(cap.id)
    return _to_view(cap, scene_id=scene.id if scene else None)


@router.patch("/{capture_id}")
async def rename_capture(capture_id: str, body: CaptureRename) -> CaptureView:
    """Rename a capture. The capture id stays the same — this only
    updates the human-facing label shown on the home / detail
    screens. Used by the rename UI on web + android."""
    cap = await store.get_capture(capture_id)
    if cap is None:
        raise HTTPException(404, "capture not found")
    new_name = body.name.strip()
    if not new_name:
        raise HTTPException(422, "name must be non-empty after trimming")
    await store.set_capture_name(cap.id, new_name)
    cap = await store.get_capture(capture_id)
    assert cap is not None  # re-read; row exists per the check above
    scene = await store.get_scene_for_capture(cap.id)
    await events.publish_capture(cap.id, "capture.renamed", name=new_name)
    return _to_view(cap, scene_id=scene.id if scene else None)


@router.get("/by-token/{token}")
async def resolve_pair_token(token: str) -> CaptureView:
    """Phone calls this immediately after scanning the QR to confirm
    the token is still valid (separate from claiming it via WS)."""
    cap = await store.get_capture_by_pair_token(token)
    if cap is None:
        raise HTTPException(404, "invalid or consumed token")
    return _to_view(cap)


class FinalizeBody(BaseModel):
    reason: str = "user"


@router.post("/{capture_id}/finalize")
async def finalize_capture(capture_id: str, body: FinalizeBody) -> dict:
    """Used by the upload path. Phone-stream finalize comes through
    the WebSocket itself."""
    cap = await store.get_capture(capture_id)
    if cap is None:
        raise HTTPException(404, "capture not found")
    existing = await store.get_scene_for_capture(cap.id)
    if existing:
        return {"scene_id": existing.id}

    scene = await store.create_scene(cap.id)
    await store.set_capture_status(cap.id, CaptureStatus.queued)
    await enqueue_pipeline(scene.id, has_pose=cap.has_pose, source=cap.source)
    await events.publish_capture(cap.id, "capture.finalized", scene_id=scene.id)
    return {"scene_id": scene.id}


@router.post("/{capture_id}/upload")
async def upload_to_capture(
    capture_id: str,
    files: list[UploadFile] = File(...),
) -> dict:
    """Drag-and-drop image-set upload. The web UI POSTs the image
    files here (one request, multiple parts) and then calls
    /finalize when the upload is done."""
    settings = get_settings()
    cap = await store.get_capture(capture_id)
    if cap is None:
        raise HTTPException(404, "capture not found")
    if cap.source != CaptureSource.upload:
        raise HTTPException(400, "capture is not an upload session")

    frames_dir = settings.captures_dir() / cap.id / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)

    await store.set_capture_status(cap.id, CaptureStatus.uploading)
    accepted = 0
    next_idx = cap.frame_count
    for f in files:
        suffix = Path(f.filename or "").suffix.lower() or ".jpg"
        if suffix not in (".jpg", ".jpeg", ".png", ".webp"):
            continue
        dst = frames_dir / f"{next_idx:06d}{suffix}"
        with dst.open("wb") as out:
            shutil.copyfileobj(f.file, out)
        next_idx += 1
        accepted += 1

    await store.bump_capture_frames(cap.id, accepted=accepted, dropped=0)
    return {"accepted": accepted, "total": next_idx}


@router.delete("/{capture_id}")
async def delete_capture(capture_id: str) -> dict:
    """Delete a capture: cancel any in-flight pipeline jobs first
    (so the worker stops chewing on data we're about to delete),
    then mark the capture as canceled and tear down the on-disk
    capture + scene directories.
    """
    settings = get_settings()
    cap = await store.get_capture(capture_id)
    if cap is None:
        raise HTTPException(404, "capture not found")

    scene = await store.get_scene_for_capture(cap.id)
    if scene is not None:
        for j in await store.list_jobs_for_scene(scene.id):
            if j.status in (
                JobStatus.queued,
                JobStatus.claimed,
                JobStatus.running,
            ):
                if await store.cancel_job(j.id):
                    await events.publish_job(j.id, "job.canceled")

    await store.set_capture_status(cap.id, CaptureStatus.canceled)

    cap_dir = settings.captures_dir() / cap.id
    if cap_dir.exists():
        shutil.rmtree(cap_dir, ignore_errors=True)
    if scene is not None:
        scene_dir = settings.scenes_dir() / scene.id
        if scene_dir.exists():
            shutil.rmtree(scene_dir, ignore_errors=True)

    return {"ok": True}


@router.websocket("/{capture_id}/stream")
async def stream_endpoint(
    ws: WebSocket,
    capture_id: str,
    token: str = Query(...),
) -> None:
    cap = await store.get_capture_by_pair_token(token)
    if cap is None or cap.id != capture_id:
        await ws.close(code=4401, reason="invalid token for this capture")
        return
    await ws.accept()
    await run_stream_session(ws, pair_token=token)


@router.websocket("/{capture_id}/events")
async def capture_events_endpoint(ws: WebSocket, capture_id: str) -> None:
    cap = await store.get_capture(capture_id)
    if cap is None:
        await ws.close(code=4404, reason="capture not found")
        return
    await ws.accept()

    topic = f"capture.{cap.id}"
    queue = await events.subscribe(topic)
    try:
        scene = await store.get_scene_for_capture(cap.id)
        await ws.send_text(
            json.dumps(
                {
                    "topic": topic,
                    "kind": "snapshot",
                    "data": _to_view(cap, scene_id=scene.id if scene else None).model_dump(),
                }
            )
        )
        while True:
            evt = await queue.get()
            try:
                await ws.send_text(evt.to_json())
            except (WebSocketDisconnect, RuntimeError):
                break
    except WebSocketDisconnect:
        pass
    finally:
        await events.unsubscribe(topic, queue)


_ = asyncio.Queue

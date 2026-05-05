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

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/captures", tags=["captures"])


# ─── DTOs ───────────────────────────────────────────


class CaptureCreate(BaseModel):
    # Optional now: when missing or blank, the server picks a
    # memorable random name (see app.words.random_name). This lets
    # the phone / web "+ new capture" flows skip the naming dialog
    # entirely; the user can rename later via PATCH.
    name: str | None = None
    source: CaptureSource = CaptureSource.upload
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
    frame_count: int
    dropped_count: int
    has_pose: bool
    meta: dict[str, Any]
    error: str | None
    scene_id: str | None
    created_at: str
    updated_at: str


def _to_view(cap: Capture, scene_id: str | None = None) -> CaptureView:
    return CaptureView(
        id=cap.id,
        name=cap.name,
        status=cap.status,
        source=cap.source,
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


class FinalizeBody(BaseModel):
    reason: str = "user"


@router.post("/{capture_id}/finalize")
async def finalize_capture(capture_id: str, body: FinalizeBody) -> dict:
    """Marks an upload-mode capture as ready for the pipeline. Caller
    should hit /upload first and then /finalize once all files are
    in. Idempotent: a second call after the scene exists returns the
    same scene id."""
    cap = await store.get_capture(capture_id)
    if cap is None:
        raise HTTPException(404, "capture not found")
    existing = await store.get_scene_for_capture(cap.id)
    if existing:
        return {"scene_id": existing.id}

    # ``create_scene`` is atomic: it returns None if the capture row
    # disappeared between our ``get_capture`` above and the INSERT.
    # Concretely, that's the case where a concurrent
    # ``DELETE /api/captures/{id}`` committed first; finalize is the
    # loser of the race and must surface a 404 rather than press on
    # to enqueue jobs against a deleted capture.
    scene = await store.create_scene(cap.id)
    if scene is None:
        raise HTTPException(404, "capture deleted before finalize completed")
    await store.set_capture_status(cap.id, CaptureStatus.queued)
    job_ids = await enqueue_pipeline(
        scene.id, has_pose=cap.has_pose, source=cap.source
    )
    if job_ids is None:
        # Scene cascaded away between create_scene and one of the
        # pipeline enqueues — concurrent capture-delete won the
        # race after we got past the create_scene atomicity gate.
        # ``enqueue_pipeline`` is all-or-nothing: a None return
        # means the scene is gone and any jobs we did insert were
        # cascaded out by the same delete. Surface as 404 rather
        # than reporting success with stale references.
        raise HTTPException(404, "capture deleted before finalize completed")
    await events.publish_capture(cap.id, "capture.finalized", scene_id=scene.id)
    return {"scene_id": scene.id}


_IMAGE_SUFFIXES = (".jpg", ".jpeg", ".png", ".webp")
_VIDEO_SUFFIXES = (".mp4", ".mov", ".webm", ".mkv")


@router.post("/{capture_id}/upload")
async def upload_to_capture(
    capture_id: str,
    files: list[UploadFile] = File(...),
) -> dict:
    """Drag-drop / video upload. Image files land in
    ``capture_dir/frames/`` immediately; a single video file lands in
    ``capture_dir/source/`` and is left there for the worker's
    ``extract`` step (ffmpeg) to crack open. Mixed image+video drops
    are rejected so the post-conditions stay simple.
    """
    settings = get_settings()
    cap = await store.get_capture(capture_id)
    if cap is None:
        raise HTTPException(404, "capture not found")
    if cap.source != CaptureSource.upload:
        raise HTTPException(400, "capture is not an upload session")

    # Tag each part with its kind up front so we can route on the
    # shape of the request without ever indexing into ``files`` by
    # an assumption about which slot the video lives in.
    classified = [
        (f, Path(f.filename or "").suffix.lower()) for f in files
    ]
    image_parts = [(f, s) for f, s in classified if s in _IMAGE_SUFFIXES]
    video_parts = [(f, s) for f, s in classified if s in _VIDEO_SUFFIXES]
    if image_parts and video_parts:
        raise HTTPException(
            422, "upload either image files or one video — not both",
        )
    if len(video_parts) > 1:
        raise HTTPException(
            422, "only one video per upload is supported",
        )

    capture_dir = settings.captures_dir() / cap.id
    # Cross-request invariant: a capture is *either* an image set *or*
    # a single-video upload, never both — even split across multiple
    # /upload calls (Android batches frames; the user might also
    # split a multi-folder drag into multiple POSTs). Reject the
    # request if the on-disk state from prior calls disagrees with
    # what this one is trying to add.
    has_existing_video = (capture_dir / "source").exists() and any(
        p.suffix.lower() in _VIDEO_SUFFIXES
        for p in (capture_dir / "source").iterdir()
        if p.is_file()
    )
    has_existing_frames = cap.frame_count > 0 or (
        (capture_dir / "frames").exists()
        and any((capture_dir / "frames").iterdir())
    )
    if video_parts and has_existing_frames:
        raise HTTPException(
            422,
            "this capture already has uploaded images; videos can't be "
            "added to the same capture",
        )
    if image_parts and has_existing_video:
        raise HTTPException(
            422,
            "this capture already has an uploaded video; images can't be "
            "added to the same capture",
        )
    if video_parts and has_existing_video:
        raise HTTPException(
            422, "a video has already been uploaded for this capture",
        )

    await store.set_capture_status(cap.id, CaptureStatus.uploading)

    if video_parts:
        # Single-video path: stash the source under ``source/`` so the
        # extract step picks it up. Don't bump frame_count yet — the
        # worker increments it as ffmpeg writes frames.
        source_dir = capture_dir / "source"
        source_dir.mkdir(parents=True, exist_ok=True)
        f, suffix = video_parts[0]
        dst = source_dir / f"video{suffix}"
        with dst.open("wb") as out:
            shutil.copyfileobj(f.file, out)
        return {"accepted_video": dst.name, "total": 0}

    # Image-set path: drop each accepted file into ``frames/`` with a
    # six-digit index. Anything outside the image suffix allowlist
    # silently drops (count is reported).
    frames_dir = capture_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    accepted = 0
    next_idx = cap.frame_count
    for f in files:
        suffix = Path(f.filename or "").suffix.lower() or ".jpg"
        if suffix not in _IMAGE_SUFFIXES:
            continue
        dst = frames_dir / f"{next_idx:06d}{suffix}"
        with dst.open("wb") as out:
            shutil.copyfileobj(f.file, out)
        next_idx += 1
        accepted += 1

    await store.bump_capture_frames(cap.id, accepted=accepted, dropped=0)
    return {"accepted": accepted, "total": next_idx}


@router.post("/{capture_id}/poses")
async def upload_poses(
    capture_id: str,
    file: UploadFile = File(...),
) -> dict:
    """Upload the per-frame ARCore poses + intrinsics for a capture.

    Used by the Android client's record-then-upload flow to ship
    ``poses.jsonl`` alongside the JPEG frames. Lets the dispatcher
    pick the ``arcore_native`` SfM backend (transforms.json from
    poses, no real glomap run) instead of re-deriving everything
    via feature matching.

    Body is a single .jsonl file (one JSON object per line, fields
    ``idx``, ``pose`` [16 floats column-major], ``intrinsics``,
    ``ts``). Server writes it verbatim to
    ``captures/<id>/poses.jsonl``; the SfM step's
    ``write_arcore_transforms_json`` parses it from there.
    """
    cap = await store.get_capture(capture_id)
    if cap is None:
        raise HTTPException(404, "capture not found")
    if cap.source != CaptureSource.upload:
        raise HTTPException(400, "capture is not an upload session")

    settings = get_settings()
    capture_dir = settings.captures_dir() / cap.id
    capture_dir.mkdir(parents=True, exist_ok=True)
    dst = capture_dir / "poses.jsonl"
    with dst.open("wb") as out:
        shutil.copyfileobj(file.file, out)
    return {"ok": True, "path": str(dst.relative_to(settings.data_dir))}


@router.delete("/{capture_id}")
async def delete_capture(capture_id: str) -> dict:
    """Delete a capture: cancel any in-flight pipeline jobs first
    (so the worker stops chewing on data we're about to delete),
    tear down the on-disk capture + scene directories, then hard-
    delete the capture row from the database.

    Previously this only flagged the capture ``canceled`` and tore
    down disk artifacts, leaving the row in the DB — which meant
    ``GET /api/captures`` kept returning the capture with status
    ``canceled``. The web ``CaptureList`` lumps ``canceled`` into the
    "failed" filter bucket, so a deleted capture stayed visible
    forever from the user's perspective. The hard-delete below
    matches what the UI's "Delete" button promises.
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

    cap_dir = settings.captures_dir() / cap.id
    if cap_dir.exists():
        shutil.rmtree(cap_dir, ignore_errors=True)
    if scene is not None:
        scene_dir = settings.scenes_dir() / scene.id
        if scene_dir.exists():
            shutil.rmtree(scene_dir, ignore_errors=True)

    await store.delete_capture(cap.id)

    return {"ok": True}


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

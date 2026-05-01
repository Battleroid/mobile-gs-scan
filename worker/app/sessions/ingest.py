"""Frame-streaming WebSocket handler.

Wire protocol:

  client → server, alternating per frame:
      1) JSON header  {"type":"frame","idx":N,"ts":<ms>,"pose":?,"intrinsics":?}
      2) raw JPEG bytes for that idx

  client → server, control:
      first message     {"type":"session", "device":..., "intrinsics":?, "has_pose": bool}
      periodic          {"type":"heartbeat","ts":<ms>}
      final             {"type":"finalize","reason":"user"|"timeout"}

  server → client:
      every 16 frames   {"type":"ack","frames_received":N,"frames_dropped":M}
      cap reached       {"type":"limit","reason":"max_frames","cap":N}
      after finalize    {"type":"queued","scene_id":"..."}
"""
from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

from fastapi import WebSocket, WebSocketDisconnect

from app.config import get_settings
from app.jobs import events, store
from app.jobs.schema import CaptureSource, CaptureStatus
from app.pipeline.dispatch import enqueue_pipeline

log = logging.getLogger(__name__)


async def run_stream_session(ws: WebSocket, *, pair_token: str) -> None:
    """Drive a single phone-side capture WebSocket end-to-end."""
    settings = get_settings()

    capture = await store.claim_pair_token(pair_token)
    if capture is None:
        await ws.close(code=4401, reason="invalid or expired pair token")
        return

    capture_dir = settings.captures_dir() / capture.id
    frames_dir = capture_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    poses_path = capture_dir / "poses.jsonl"
    meta_path = capture_dir / "meta.json"

    log.info("ws: capture=%s starting (source=%s)", capture.id, capture.source.value)
    await events.publish_capture(capture.id, "stream.connected")

    accepted = 0
    dropped = 0
    pending_header: dict[str, Any] | None = None

    try:
        while True:
            msg = await ws.receive()

            if "text" in msg and msg["text"] is not None:
                try:
                    payload = json.loads(msg["text"])
                except json.JSONDecodeError:
                    log.warning("ws: capture=%s bad json", capture.id)
                    continue

                kind = payload.get("type")
                if kind == "session":
                    await _persist_session_meta(capture.id, payload, meta_path)
                    if payload.get("has_pose"):
                        await store.set_capture_status(
                            capture.id, CaptureStatus.streaming
                        )
                elif kind == "heartbeat":
                    pass
                elif kind == "frame":
                    pending_header = payload
                elif kind == "finalize":
                    log.info(
                        "ws: capture=%s finalize (frames=%d dropped=%d)",
                        capture.id,
                        accepted,
                        dropped,
                    )
                    scene_id = await _finalize(capture.id, capture.has_pose)
                    await ws.send_text(
                        json.dumps({"type": "queued", "scene_id": scene_id})
                    )
                    return
                else:
                    log.warning("ws: capture=%s unknown msg type %r", capture.id, kind)

            elif "bytes" in msg and msg["bytes"] is not None:
                if pending_header is None:
                    log.warning("ws: capture=%s binary without header", capture.id)
                    continue
                if accepted + dropped >= settings.capture_max_frames:
                    dropped += 1
                    if dropped == 1:
                        await ws.send_text(
                            json.dumps(
                                {
                                    "type": "limit",
                                    "reason": "max_frames",
                                    "cap": settings.capture_max_frames,
                                }
                            )
                        )
                    pending_header = None
                    continue

                idx = int(pending_header.get("idx", accepted))
                frame_path = frames_dir / f"{idx:06d}.jpg"
                frame_path.write_bytes(msg["bytes"])

                pose = pending_header.get("pose")
                intr = pending_header.get("intrinsics")
                ts = pending_header.get("ts")
                with poses_path.open("a") as f:
                    f.write(
                        json.dumps(
                            {"idx": idx, "ts": ts, "pose": pose, "intrinsics": intr}
                        )
                        + "\n"
                    )

                accepted += 1
                pending_header = None

                if accepted % 16 == 0:
                    await store.bump_capture_frames(
                        capture.id, accepted=16, dropped=0
                    )
                    await events.publish_capture(
                        capture.id,
                        "stream.frames",
                        accepted=accepted,
                        dropped=dropped,
                    )
                    await ws.send_text(
                        json.dumps(
                            {
                                "type": "ack",
                                "frames_received": accepted,
                                "frames_dropped": dropped,
                            }
                        )
                    )

            elif msg.get("type") == "websocket.disconnect":
                raise WebSocketDisconnect(msg.get("code", 1000))

    except WebSocketDisconnect:
        log.info(
            "ws: capture=%s disconnected (frames=%d dropped=%d)",
            capture.id,
            accepted,
            dropped,
        )
    except Exception:
        log.exception("ws: capture=%s crashed", capture.id)
        await store.set_capture_status(
            capture.id, CaptureStatus.failed, error="stream crashed"
        )
    finally:
        leftover_accepted = accepted % 16
        if leftover_accepted or dropped:
            await store.bump_capture_frames(
                capture.id, accepted=leftover_accepted, dropped=dropped
            )


async def _persist_session_meta(
    capture_id: str, payload: dict[str, Any], meta_path: Path
) -> None:
    meta = {
        "device": payload.get("device", {}),
        "intrinsics": payload.get("intrinsics"),
        "has_pose": bool(payload.get("has_pose")),
    }
    meta_path.write_text(json.dumps(meta, indent=2))


async def _finalize(capture_id: str, has_pose: bool) -> str:
    """Flip the capture into queued + spin up the pipeline jobs."""
    await store.set_capture_status(capture_id, CaptureStatus.queued)
    scene = await store.create_scene(capture_id)
    await enqueue_pipeline(scene.id, has_pose=has_pose, source=CaptureSource.mobile_native)
    await events.publish_capture(
        capture_id, "stream.finalized", scene_id=scene.id
    )
    return scene.id

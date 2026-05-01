"""In-memory pub/sub for live progress events.

Three topics:

  capture.<capture_id>   frame counts, status flips, dropped frames
  scene.<scene_id>       roll-up of all the scene's jobs
  job.<job_id>           per-job status, progress, log lines

Subscribers register an asyncio.Queue and receive every event
published to their topic. There's no persistence — a client that's
not connected when an event fires misses it. That's fine for a
single-user studio with the web UI in the foreground.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from typing import Any


@dataclass
class Event:
    topic: str
    kind: str
    data: dict[str, Any] = field(default_factory=dict)
    ts: float = field(default_factory=time.time)

    def to_json(self) -> str:
        return json.dumps(asdict(self))


_subscribers: dict[str, set[asyncio.Queue[Event]]] = defaultdict(set)
_lock = asyncio.Lock()


async def publish(topic: str, kind: str, **data: Any) -> None:
    evt = Event(topic=topic, kind=kind, data=data)
    async with _lock:
        queues = list(_subscribers.get(topic, ()))
    for q in queues:
        try:
            q.put_nowait(evt)
        except asyncio.QueueFull:
            # Subscriber is slow — drop. Better than blocking the
            # publisher (which is usually the worker progress loop).
            pass


async def subscribe(topic: str, *, maxsize: int = 256) -> asyncio.Queue[Event]:
    q: asyncio.Queue[Event] = asyncio.Queue(maxsize=maxsize)
    async with _lock:
        _subscribers[topic].add(q)
    return q


async def unsubscribe(topic: str, q: asyncio.Queue[Event]) -> None:
    async with _lock:
        _subscribers.get(topic, set()).discard(q)
        if topic in _subscribers and not _subscribers[topic]:
            del _subscribers[topic]


# Convenience wrappers — keeps the call sites consistent.

async def publish_capture(capture_id: str, kind: str, **data: Any) -> None:
    await publish(f"capture.{capture_id}", kind, **data)


async def publish_scene(scene_id: str, kind: str, **data: Any) -> None:
    await publish(f"scene.{scene_id}", kind, **data)


async def publish_job(job_id: str, kind: str, **data: Any) -> None:
    await publish(f"job.{job_id}", kind, **data)

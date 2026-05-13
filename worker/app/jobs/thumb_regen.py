"""Helpers for re-rendering a scene's thumbnail / orbit pair when
the source splat changes underneath the post-export render.

Two triggers ship using these:
  * Filter apply (runner._run_filter): the edited PLY becomes the
    rendering source; the regen pair uses the gsplat PLY path
    (use_edited_ply=True).
  * Filter discard (api/scenes.clear_edit): the original PLY +
    splatfacto checkpoint take over again; the regen pair uses
    the default ns-render path (use_edited_ply=False).

In both cases we cancel any in-flight thumbnail / orbit jobs
first. Without that, a stale post-export render finishing AFTER
the regen enqueue can overwrite Scene.thumbnail_path /
Scene.orbit_path with the wrong frame, leaving the user
intermittently seeing the pre-action thumb.
"""
from __future__ import annotations

import logging

from app.jobs import events, store
from app.jobs.schema import JobKind, JobStatus

log = logging.getLogger(__name__)


async def cancel_in_flight_thumb_orbit(scene_id: str) -> None:
    """Flip every queued / claimed / running thumbnail or orbit
    job for the scene to ``canceled``. Idempotent: a cancel on an
    already-terminal row is a no-op. The worker's heartbeat loop
    picks up status=canceled on its next tick to kill any running
    subprocess; the runner's cancel-ack paths leave
    Scene.thumbnail_path / Scene.orbit_path untouched so the regen
    jobs that follow land on a stable starting state.
    """
    in_flight = (JobStatus.queued, JobStatus.claimed, JobStatus.running)
    for j in await store.list_jobs_for_scene(scene_id):
        if j.kind in (JobKind.thumbnail, JobKind.orbit) and j.status in in_flight:
            await store.cancel_job(j.id)
            await events.publish_job(j.id, "job.canceled")


async def enqueue_regen(scene_id: str, *, use_edited_ply: bool) -> None:
    """Cancel in-flight thumbnail / orbit jobs, then enqueue a
    fresh thumbnail with the given payload. The thumbnail's
    success path enqueues the matching orbit so we only have to
    queue one job here.

    ``use_edited_ply``:
      * True (filter apply) — route through the gsplat PLY path
        against scene.edited_ply_path.
      * False (filter discard / explicit re-render of original)
        — route through ns-render against the splatfacto
        checkpoint at scene.ply_path. The same code path the
        first post-export thumbnail used.
    """
    await cancel_in_flight_thumb_orbit(scene_id)
    payload: dict = {}
    if use_edited_ply:
        payload["use_edited_ply"] = True
    await store.enqueue_job(scene_id, JobKind.thumbnail, payload=payload)

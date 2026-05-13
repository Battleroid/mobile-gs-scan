"""Shared scene-finalization helper.

A scene flips to its terminal capture status (``completed`` or
``canceled``) once every job for it has reached a terminal job
status. This used to live as a private helper inside the worker's
job runner, but PR-D's ``JobKind.thumbnail`` introduced a code
path where the API itself needs to trigger finalization:

  * A user cancels a queued thumbnail (the UI allows this) before
    the worker ever claims it.
  * No worker ever runs ``_run_thumbnail`` for that job, so the
    runner-side finalize call never fires.
  * Without the API-side fallback, the scene + capture stay
    stuck at ``processing`` forever even though every job is
    terminal.

Extracted here so both the runner and the API import the same
implementation. Filter / mesh queued cancels don't need this hook
because they run on already-completed scenes — but adding the
hook for those kinds is a no-op (the all-terminal pass returns
True immediately, the status flip is idempotent).
"""
from __future__ import annotations

from app.jobs import events, store
from app.jobs.schema import CaptureStatus, JobKind, JobStatus

# Job kinds that should NOT block scene finalization. These run
# AFTER the scene has everything it needs to be marked completed,
# and their failure / pending state shouldn't keep the home grid's
# capture chip stuck on "training". Currently just JobKind.orbit
# — the still PNG (JobKind.thumbnail) is the canonical thumbnail
# and lands first; the MP4 orbit is a richer-feel backfill that
# can take a few extra minutes. If we ever add other "richer feel"
# post-pipeline jobs (preview spz, etc.) they belong here too.
NON_BLOCKING_KINDS: frozenset[JobKind] = frozenset({JobKind.orbit})


async def maybe_finalize_scene(scene_id: str) -> None:
    """Mark scene + capture completed if every blocking job is done.

    Re-fetches the scene by id (rather than trusting a snapshot)
    so a caller that hands in a stale ``Scene`` post capture-delete
    cascade still no-ops instead of publishing a spurious
    ``scene.completed`` event.

    Differentiates "every job terminal AND we have artifacts"
    (→ completed) from "every job terminal BUT no .ply produced"
    (→ canceled). The artifact-presence check keeps the rule
    durable as the pipeline evolves — no need to hardcode which
    JobKinds are essential.

    ``NON_BLOCKING_KINDS`` (currently just ``JobKind.orbit``) is
    skipped during the all-terminal pass so the scene can finalize
    on PNG thumbnail completion without waiting for the heavier
    MP4 orbit. The orbit job's own completion will re-call this
    method, which is idempotent once the scene is already
    completed (the unconditional status flip + event publish
    re-run cheaply; if we ever want to suppress them, gate on a
    `status == processing` check here).
    """
    refreshed = await store.get_scene(scene_id)
    if refreshed is None:
        return
    jobs = await store.list_jobs_for_scene(refreshed.id)
    blocking = [j for j in jobs if j.kind not in NON_BLOCKING_KINDS]
    if any(
        j.status not in (JobStatus.completed, JobStatus.canceled) for j in blocking
    ):
        return
    if any(j.status == JobStatus.failed for j in blocking):
        return

    if not refreshed.ply_path:
        # Essential upstream job was canceled mid-pipeline. Don't
        # mislead the user into thinking this capture is ready.
        await store.update_scene(refreshed.id, status=CaptureStatus.canceled)
        await events.publish_scene(refreshed.id, "scene.canceled")
        cap = await store.get_capture(refreshed.capture_id)
        if cap:
            await store.set_capture_status(cap.id, CaptureStatus.canceled)
        return

    await store.update_scene(refreshed.id, status=CaptureStatus.completed)
    await events.publish_scene(refreshed.id, "scene.completed")
    cap = await store.get_capture(refreshed.capture_id)
    if cap:
        await store.set_capture_status(cap.id, CaptureStatus.completed)

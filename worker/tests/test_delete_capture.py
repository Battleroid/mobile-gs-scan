"""Tests for capture deletion semantics.

Regression test: previously the DELETE /api/captures/{id} handler
only set status=canceled on the capture row + tore down disk
artifacts, but left the row in the DB. ``GET /api/captures`` kept
returning it; the web's "failed" filter bucket lumps ``canceled``
in with ``failed`` so deleted captures stayed visible forever.

These tests pin the new behavior: ``store.delete_capture()`` hard-
deletes the capture, its scene, and all of the scene's jobs in one
transaction. The schema's ``ondelete=CASCADE`` declarations are
no-ops on SQLite without per-connection ``PRAGMA foreign_keys=ON``,
so we cascade manually — these tests verify that cascade actually
removes the child rows.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from app.config import Settings
from app.jobs import runner, store
from app.jobs.schema import CaptureStatus, JobKind, JobStatus
from app.pipeline import _running


@pytest.fixture
def isolated_store(tmp_path: Path):
    """Spin up a fresh sqlite DB under tmp_path for each test.

    init_store creates captures/ + scenes/ subdirs alongside the db
    file; tmp_path is per-test so nothing leaks between runs.
    """
    settings = Settings(
        data_dir=tmp_path,
        db_filename="test_delete_capture.sqlite",
    )

    async def setup():
        await store.init_store(settings)

    async def teardown():
        await store.shutdown_store()

    asyncio.run(setup())
    yield
    asyncio.run(teardown())


def _run(coro):
    return asyncio.run(coro)


def test_delete_capture_removes_row(isolated_store):
    async def go():
        cap = await store.create_capture(name="cap-a", source="upload")
        assert await store.get_capture(cap.id) is not None
        deleted = await store.delete_capture(cap.id)
        assert deleted is True
        assert await store.get_capture(cap.id) is None
        # list_captures should also no longer surface it.
        assert all(c.id != cap.id for c in await store.list_captures())

    _run(go())


def test_delete_capture_cascades_to_scene_and_jobs(isolated_store):
    """Hard delete must cascade — leaving an orphan scene or job
    pointing at a deleted capture would corrupt downstream queries.
    """
    async def go():
        cap = await store.create_capture(name="cap-b", source="upload")
        scene = await store.create_scene(cap.id)
        job = await store.enqueue_job(scene.id, JobKind.train)

        # Sanity — child rows exist.
        assert await store.get_scene(scene.id) is not None
        assert await store.get_job(job.id) is not None

        deleted = await store.delete_capture(cap.id)
        assert deleted is True

        # Capture, scene, and the scene's job all gone.
        assert await store.get_capture(cap.id) is None
        assert await store.get_scene(scene.id) is None
        assert await store.get_job(job.id) is None

    _run(go())


def test_delete_capture_returns_false_when_missing(isolated_store):
    async def go():
        assert await store.delete_capture("nope-no-such-id") is False

    _run(go())


def test_delete_capture_idempotent(isolated_store):
    """Calling delete twice must succeed-then-return-False, not
    raise. The handler treats the first call as the operative one;
    a duplicate request (network retry, etc.) shouldn't 500."""
    async def go():
        cap = await store.create_capture(name="cap-c", source="upload")
        assert await store.delete_capture(cap.id) is True
        assert await store.delete_capture(cap.id) is False

    _run(go())


def test_delete_capture_canceled_status_no_longer_persists(isolated_store):
    """Regression: previous behavior left the capture row with
    status=canceled. After this fix the row is gone entirely, which
    is what the web's "Delete" button promises.
    """
    async def go():
        cap = await store.create_capture(name="cap-d", source="upload")
        # Mimic what the API handler does with in-flight cancels —
        # status flips to canceled before the row is removed in the
        # old flow. Test that even with a canceled status set, the
        # delete still removes the row.
        await store.set_capture_status(cap.id, CaptureStatus.canceled)
        await store.delete_capture(cap.id)
        assert await store.get_capture(cap.id) is None

    _run(go())


def test_delete_capture_only_affects_target(isolated_store):
    """Cascading must not vacuum unrelated captures' scenes or jobs."""
    async def go():
        keep = await store.create_capture(name="keep", source="upload")
        keep_scene = await store.create_scene(keep.id)
        keep_job = await store.enqueue_job(keep_scene.id, JobKind.train)

        drop = await store.create_capture(name="drop", source="upload")
        drop_scene = await store.create_scene(drop.id)
        await store.enqueue_job(drop_scene.id, JobKind.train)

        await store.delete_capture(drop.id)

        assert await store.get_capture(keep.id) is not None
        assert await store.get_scene(keep_scene.id) is not None
        kept = await store.get_job(keep_job.id)
        assert kept is not None
        assert kept.status == JobStatus.queued

    _run(go())


def test_heartbeat_kills_subprocess_when_row_deleted(
    isolated_store, monkeypatch: pytest.MonkeyPatch
):
    """When a capture is deleted while one of its jobs is mid-flight,
    cascading row removal must trigger the worker's heartbeat to kill
    the subprocess and cancel the dispatch task. Otherwise the worker
    would keep grinding on already-deleted disk until the subprocess
    noticed an I/O error on its own — sometimes minutes later.

    Regression for the codex P1 on PR #81: the heartbeat used to
    silently skip when ``get_job`` returned ``None`` (only acting on
    explicit status=canceled), so a delete-during-train left the
    pipeline running.
    """
    async def go():
        # Speed the heartbeat up so the test doesn't sit on the
        # 5s production interval. Patched at module-level so the
        # _heartbeat coroutine's `asyncio.sleep(HEARTBEAT_INTERVAL)`
        # picks it up.
        monkeypatch.setattr(runner, "HEARTBEAT_INTERVAL", 0.02)

        # Stub kill_for_job so we don't need a real subprocess.
        # Track which job ids it's called with.
        killed: list[str] = []
        monkeypatch.setattr(
            _running, "kill_for_job", lambda jid: killed.append(jid)
        )

        cap = await store.create_capture(name="cap-delete", source="upload")
        scene = await store.create_scene(cap.id)
        job = await store.enqueue_job(scene.id, JobKind.train)
        # Move the job into running state so it looks like real work.
        await store.update_job(job.id, status=JobStatus.running)

        # Stand-in for the dispatch coroutine — sleeps long enough
        # that the heartbeat is the one to terminate it.
        dispatch_task = asyncio.create_task(asyncio.sleep(60))
        hb_task = asyncio.create_task(runner._heartbeat(job.id, dispatch_task))

        # Let the heartbeat tick a couple of times.
        await asyncio.sleep(0.05)
        # User deletes the capture — cascades through to job rows.
        assert await store.delete_capture(cap.id) is True
        # Heartbeat should observe the missing row on its next tick
        # and call kill_for_job + dispatch_task.cancel(), then exit.
        await asyncio.wait_for(hb_task, timeout=2.0)

        assert killed == [job.id], (
            "heartbeat must SIGKILL the running subprocess when its "
            "job row is gone (capture was deleted out from under it)"
        )
        assert dispatch_task.cancelled() or dispatch_task.done()
        # Clean up the dispatch_task placeholder.
        if not dispatch_task.done():
            dispatch_task.cancel()

    _run(go())


def test_ack_user_cancel_treats_missing_row_as_canceled(isolated_store):
    """The outer worker loop's CancelledError handler calls
    ``_ack_user_cancel`` and re-raises if it returns False — which
    would terminate the worker process. After the heartbeat fix,
    a row going missing (capture deleted out from under a running
    job) MUST be ack'd as user-cancel so the worker loop continues
    instead of exiting until restart.

    Regression for the second codex P1 on PR #81 — the heartbeat
    fix alone was insufficient because the cancel chain
    (heartbeat → dispatch_task.cancel() → CancelledError →
    _ack_user_cancel → raise) ended at ``raise`` when the row was
    gone, taking down the worker.
    """
    async def go():
        cap = await store.create_capture(name="cap-y", source="upload")
        scene = await store.create_scene(cap.id)
        job = await store.enqueue_job(scene.id, JobKind.train)
        await store.update_job(job.id, status=JobStatus.running)

        # Hold a reference to the Job object the way run_forever
        # does — _ack_user_cancel takes the job DTO and re-fetches
        # by id, so the original is fine to keep around.
        job_ref = job

        # Sanity — running row, not canceled. Should not ack.
        assert await runner._ack_user_cancel(job_ref) is False

        # Delete the capture — cascades the row away.
        await store.delete_capture(cap.id)

        # Row gone. _ack_user_cancel must return True so
        # run_forever swallows the CancelledError and continues.
        assert await runner._ack_user_cancel(job_ref) is True

    _run(go())


def test_ack_user_cancel_returns_true_for_canceled_row(isolated_store):
    """The pre-existing canceled-status path stays working — the
    missing-row fix above is additive."""
    async def go():
        cap = await store.create_capture(name="cap-z", source="upload")
        scene = await store.create_scene(cap.id)
        job = await store.enqueue_job(scene.id, JobKind.train)
        await store.update_job(job.id, status=JobStatus.running)
        await store.cancel_job(job.id)
        assert await runner._ack_user_cancel(job) is True

    _run(go())


def test_heartbeat_kills_subprocess_when_status_canceled(
    isolated_store, monkeypatch: pytest.MonkeyPatch
):
    """The pre-existing canceled-status path must still work — the
    deleted-row fix above adds a second trigger, doesn't replace this
    one. The API DELETE handler cancels jobs first, then deletes the
    row; whichever the heartbeat sees first should result in the
    same kill.
    """
    async def go():
        monkeypatch.setattr(runner, "HEARTBEAT_INTERVAL", 0.02)
        killed: list[str] = []
        monkeypatch.setattr(
            _running, "kill_for_job", lambda jid: killed.append(jid)
        )

        cap = await store.create_capture(name="cap-cancel", source="upload")
        scene = await store.create_scene(cap.id)
        job = await store.enqueue_job(scene.id, JobKind.train)
        await store.update_job(job.id, status=JobStatus.running)

        dispatch_task = asyncio.create_task(asyncio.sleep(60))
        hb_task = asyncio.create_task(runner._heartbeat(job.id, dispatch_task))

        await asyncio.sleep(0.05)
        # Flip status to canceled but leave the row in place.
        assert await store.cancel_job(job.id) is True
        await asyncio.wait_for(hb_task, timeout=2.0)

        assert killed == [job.id]
        assert dispatch_task.cancelled() or dispatch_task.done()
        if not dispatch_task.done():
            dispatch_task.cancel()

    _run(go())

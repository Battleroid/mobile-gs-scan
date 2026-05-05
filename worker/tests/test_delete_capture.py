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
from app.jobs import store
from app.jobs.schema import CaptureStatus, JobKind, JobStatus


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

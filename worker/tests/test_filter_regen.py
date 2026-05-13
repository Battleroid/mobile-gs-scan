"""Tests for filter-triggered thumbnail / orbit regen (Phase 3 PR-B).

Covers the new code surface without a GPU:
* ``ply_render.is_available()`` resolves False on a test host
  (no CUDA), with a non-empty reason string.
* ``run_thumbnail(use_ply_renderer=True)`` returns a permanent_skip
  marker when ply_render isn't available — same shape the
  ns-render path uses, so backfill / finalize logic doesn't need
  to special-case the renderer choice.
* ``run_orbit(use_ply_renderer=True)`` same — but only when ffmpeg
  is also present (ffmpeg is required regardless of which path
  produces the frame sequence).
* Filter completion in the runner enqueues a regen thumbnail with
  ``use_edited_ply: True``.
* The regen thumbnail's success path forwards the
  ``use_edited_ply`` flag when enqueuing the follow-up orbit.
* The regen thumbnail in the runner reads ``scene.edited_ply_path``
  rather than ``scene.ply_path`` — feeding ns-render the edited
  PLY would produce a byte-identical unedited frame, defeating
  the whole regen.

GPU-side rendering (gsplat.rasterization against a real splatfacto
PLY) is not exercised in pytest; verify via ``make up`` smoke.
"""
from __future__ import annotations

import asyncio
import os
import shutil
import struct
from pathlib import Path

import pytest

from app.config import Settings
from app.jobs import runner, store
from app.jobs.schema import EditStatus, JobKind, JobStatus
from app.pipeline import ply_render, orbit as orbit_step, thumbnail as thumbnail_step


@pytest.fixture
def isolated_store(tmp_path: Path):
    settings = Settings(data_dir=tmp_path, db_filename="test_filter_regen.sqlite")

    async def setup():
        await store.init_store(settings)

    async def teardown():
        await store.shutdown_store()

    asyncio.run(setup())
    yield
    asyncio.run(teardown())


def _run(coro):
    return asyncio.run(coro)


def test_ply_render_unavailable_on_test_host():
    """No CUDA on the test runner → is_available() reports False
    with a reason string. The reason is fed straight into the
    permanent_skip marker so backfill can stop re-enqueueing."""
    ok, reason = ply_render.is_available()
    assert isinstance(ok, bool)
    if not ok:
        assert reason and isinstance(reason, str)


def test_run_thumbnail_ply_renderer_skips_when_unavailable(tmp_path: Path):
    """``use_ply_renderer=True`` + no CUDA → permanent_skip. Same
    shape the ns-render miss takes, so the runner / backfill don't
    need to know which renderer was attempted."""
    scene_dir = tmp_path / "scene"
    scene_dir.mkdir(parents=True)
    (scene_dir / "train").mkdir()
    src_ply = scene_dir / "scene.ply"
    _write_minimal_ply(src_ply)

    progress_calls: list[tuple[float, str]] = []

    async def progress(pct: float, msg: str) -> None:
        progress_calls.append((pct, msg))

    result = _run(thumbnail_step.run_thumbnail(
        scene_dir=scene_dir,
        src_ply=src_ply,
        progress=progress,
        use_ply_renderer=True,
    ))
    # If the test host happens to have CUDA + gsplat (rare in CI),
    # we'd get a real render. Skip the assertion in that case so
    # the test stays portable.
    ok, _ = ply_render.is_available()
    if ok:
        pytest.skip("ply_render is available on this host; skipping the unavailable-skip assertion")
    assert "permanent_skip" in result
    assert any(pct == 1.0 for pct, _ in progress_calls)


def test_run_orbit_ply_renderer_skips_when_unavailable(tmp_path: Path):
    """Same shape for orbit. Requires ffmpeg on PATH so the early
    ffmpeg-missing check doesn't intercept; the test host likely
    has ffmpeg (Dockerfile.base) but skip if not."""
    if not shutil.which("ffmpeg"):
        pytest.skip("ffmpeg not on PATH; orbit's pre-check would skip before the renderer probe")
    ok, _ = ply_render.is_available()
    if ok:
        pytest.skip("ply_render is available on this host")

    scene_dir = tmp_path / "scene"
    scene_dir.mkdir(parents=True)
    (scene_dir / "train").mkdir()
    src_ply = scene_dir / "scene.ply"
    _write_minimal_ply(src_ply)

    async def progress(pct: float, msg: str) -> None:
        pass

    result = _run(orbit_step.run_orbit(
        scene_dir=scene_dir,
        src_ply=src_ply,
        progress=progress,
        use_ply_renderer=True,
    ))
    assert "permanent_skip" in result


def test_filter_completion_enqueues_regen_thumbnail(
    isolated_store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """After a filter job lands successfully, a fresh thumbnail
    job must be enqueued with ``use_edited_ply: True``. Without
    this, the home grid stays on the pre-filter thumbnail until
    the user re-triggers training."""
    async def go():
        cap = await store.create_capture(name="filter-regen", source="upload")
        scene = await store.create_scene(cap.id)
        assert scene is not None

        # Set up a "completed" scene with a ply ready to be filtered.
        ply_path = tmp_path / "scene.ply"
        _write_minimal_ply(ply_path)
        await store.update_scene(scene.id, ply_path=str(ply_path))

        # Filter job: stage as running, then run the success path
        # via a monkeypatched filter_step.run_filter that "produces"
        # an edited PLY.
        filter_job = await store.enqueue_job(scene.id, JobKind.filter, payload={
            "recipe": {"ops": []},
        })
        assert filter_job is not None
        await store.update_job(filter_job.id, status=JobStatus.running)

        edited_ply = tmp_path / "edited.ply"
        edited_ply.write_bytes(ply_path.read_bytes())

        async def fake_filter_splat(*args, **kwargs):
            return {
                "ply": str(edited_ply),
                "spz": str(edited_ply.with_suffix(".spz")),
                "kept": 4,
                "total": 8,
            }

        from app.pipeline import filter as filter_step
        monkeypatch.setattr(filter_step, "filter_splat", fake_filter_splat)

        live_job = await store.get_job(filter_job.id)
        assert live_job is not None
        live_scene = await store.get_scene(scene.id)
        assert live_scene is not None

        settings = Settings(data_dir=tmp_path, db_filename="test_filter_regen.sqlite")
        await runner._run_filter(
            job=live_job, scene=live_scene, settings=settings,
        )

        jobs = await store.list_jobs_for_scene(scene.id)
        thumb_jobs = [j for j in jobs if j.kind == JobKind.thumbnail]
        assert len(thumb_jobs) == 1, (
            f"filter success should enqueue exactly one regen "
            f"thumbnail; saw {[j.kind.value for j in jobs]}"
        )
        regen = thumb_jobs[0]
        assert regen.payload.get("use_edited_ply") is True, (
            f"regen thumbnail must carry use_edited_ply=True; "
            f"got payload {regen.payload}"
        )
        # Scene should also reflect filter success.
        refreshed = await store.get_scene(scene.id)
        assert refreshed is not None
        assert refreshed.edit_status == EditStatus.completed
        assert refreshed.edited_ply_path == str(edited_ply)

    _run(go())


def test_filter_completion_cancels_in_flight_thumb_orbit(
    isolated_store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Stale-render race: if a thumbnail or orbit is still queued
    / running from the post-export pipeline when filter completes,
    the older unedited render could overwrite Scene.thumbnail_path
    / Scene.orbit_path AFTER the regen lands. Filter's success
    path must cancel any in-flight thumbnail / orbit jobs before
    enqueuing the regen so the regen lands on a clean slot.

    Regression for the Codex P1 on PR #94.
    """
    async def go():
        cap = await store.create_capture(name="race-fix", source="upload")
        scene = await store.create_scene(cap.id)
        assert scene is not None

        ply_path = tmp_path / "scene.ply"
        _write_minimal_ply(ply_path)
        await store.update_scene(scene.id, ply_path=str(ply_path))

        # Stage in-flight thumbnail (queued) + orbit (running) +
        # an already-completed thumbnail (should NOT be touched).
        queued_thumb = await store.enqueue_job(
            scene.id, JobKind.thumbnail, payload={},
        )
        running_orbit = await store.enqueue_job(
            scene.id, JobKind.orbit, payload={},
        )
        assert queued_thumb is not None and running_orbit is not None
        await store.update_job(running_orbit.id, status=JobStatus.running)
        prior_thumb = await store.enqueue_job(
            scene.id, JobKind.thumbnail, payload={},
        )
        assert prior_thumb is not None
        await store.update_job(
            prior_thumb.id, status=JobStatus.completed, completed=True,
        )

        # Stage filter job + success.
        filter_job = await store.enqueue_job(scene.id, JobKind.filter, payload={
            "recipe": {"ops": []},
        })
        assert filter_job is not None
        await store.update_job(filter_job.id, status=JobStatus.running)

        edited_ply = tmp_path / "edited.ply"
        edited_ply.write_bytes(ply_path.read_bytes())

        async def fake_filter_splat(*args, **kwargs):
            return {
                "ply": str(edited_ply),
                "spz": str(edited_ply.with_suffix(".spz")),
                "kept": 4,
                "total": 8,
            }

        from app.pipeline import filter as filter_step
        monkeypatch.setattr(filter_step, "filter_splat", fake_filter_splat)

        live_job = await store.get_job(filter_job.id)
        assert live_job is not None
        live_scene = await store.get_scene(scene.id)
        assert live_scene is not None

        settings = Settings(data_dir=tmp_path, db_filename="test_filter_regen.sqlite")
        await runner._run_filter(
            job=live_job, scene=live_scene, settings=settings,
        )

        # Post-conditions:
        #  * queued_thumb (was queued) → canceled
        #  * running_orbit (was running) → canceled
        #  * prior_thumb (was completed) → still completed (untouched)
        #  * a fresh regen thumbnail with use_edited_ply=True appears
        post_qt = await store.get_job(queued_thumb.id)
        post_ro = await store.get_job(running_orbit.id)
        post_pt = await store.get_job(prior_thumb.id)
        assert post_qt is not None and post_qt.status == JobStatus.canceled, (
            f"in-flight queued thumbnail should be canceled; "
            f"got {post_qt.status if post_qt else 'gone'}"
        )
        assert post_ro is not None and post_ro.status == JobStatus.canceled, (
            f"in-flight running orbit should be canceled; "
            f"got {post_ro.status if post_ro else 'gone'}"
        )
        assert post_pt is not None and post_pt.status == JobStatus.completed, (
            f"already-completed thumbnail must NOT be touched; "
            f"got {post_pt.status if post_pt else 'gone'}"
        )

        # The regen thumbnail must be queued (NOT one of the canceled
        # ones — the cancel happens BEFORE the enqueue).
        jobs = await store.list_jobs_for_scene(scene.id)
        regen_candidates = [
            j for j in jobs
            if j.kind == JobKind.thumbnail
            and j.status == JobStatus.queued
            and j.payload.get("use_edited_ply") is True
        ]
        assert len(regen_candidates) == 1, (
            f"exactly one regen thumbnail should be queued; "
            f"saw {[(j.id, j.status.value, j.payload) for j in jobs if j.kind == JobKind.thumbnail]}"
        )

    _run(go())


def test_edit_clear_regens_thumbnail_against_original_splat(
    isolated_store, tmp_path: Path,
):
    """When the user discards their edit (DELETE /api/scenes/{id}/edit),
    the home grid should flip back to a thumbnail of the ORIGINAL
    splat. Without this, the filter-regen render stays in
    Scene.thumbnail_path / Scene.orbit_path after the user cleared
    their edit, leaving the card showing a filtered preview for a
    scene that no longer has the filter applied.

    The clear-edit endpoint must enqueue a fresh thumbnail job
    (with use_edited_ply=False so it routes through ns-render
    against the original splatfacto checkpoint) and cancel any
    in-flight filter-regen thumbnail/orbit jobs first.
    """
    from app.api.scenes import clear_edit

    async def go():
        cap = await store.create_capture(name="clear-edit", source="upload")
        scene = await store.create_scene(cap.id)
        assert scene is not None

        ply_path = tmp_path / "scene.ply"
        edited_ply = tmp_path / "edited.ply"
        _write_minimal_ply(ply_path)
        _write_minimal_ply(edited_ply)
        await store.update_scene(
            scene.id,
            ply_path=str(ply_path),
            edited_ply_path=str(edited_ply),
            edit_status=EditStatus.completed,
        )

        # Stage an in-flight filter-regen thumbnail (queued, with
        # use_edited_ply=True) — clear_edit should cancel it so it
        # can't land an edited render after the discard.
        stale = await store.enqueue_job(
            scene.id, JobKind.thumbnail,
            payload={"use_edited_ply": True},
        )
        assert stale is not None

        await clear_edit(scene.id)

        # Stale filter-regen thumbnail must be canceled.
        post_stale = await store.get_job(stale.id)
        assert post_stale is not None and post_stale.status == JobStatus.canceled, (
            f"in-flight filter-regen thumbnail should be canceled; "
            f"got {post_stale.status if post_stale else 'gone'}"
        )

        # A fresh regen thumbnail must be queued, this one with
        # NO use_edited_ply flag so it renders the original.
        jobs = await store.list_jobs_for_scene(scene.id)
        fresh = [
            j for j in jobs
            if j.kind == JobKind.thumbnail
            and j.status == JobStatus.queued
            and not j.payload.get("use_edited_ply")
        ]
        assert len(fresh) == 1, (
            f"clear_edit should enqueue exactly one original-splat "
            f"regen thumbnail; saw "
            f"{[(j.id, j.status.value, j.payload) for j in jobs if j.kind == JobKind.thumbnail]}"
        )

    _run(go())


def test_regen_thumbnail_reads_edited_ply_path(
    isolated_store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """``_run_thumbnail`` with a use_edited_ply payload must point
    ``run_thumbnail`` at ``scene.edited_ply_path``, not the
    unedited ``scene.ply_path``. Feeding ns-render the edited PLY
    would still produce an unedited frame (it reads the
    checkpoint), so this is the gate that actually routes through
    the gsplat path."""
    async def go():
        cap = await store.create_capture(name="regen-src", source="upload")
        scene = await store.create_scene(cap.id)
        assert scene is not None

        ply_path = tmp_path / "scene.ply"
        edited_ply = tmp_path / "edited.ply"
        _write_minimal_ply(ply_path)
        _write_minimal_ply(edited_ply, scale=2.0)
        await store.update_scene(
            scene.id,
            ply_path=str(ply_path),
            edited_ply_path=str(edited_ply),
        )

        # Capture the src_ply + use_ply_renderer args the
        # thumbnail step was called with.
        captured: dict = {}

        async def fake_run_thumbnail(*, scene_dir, src_ply, progress, job_id=None, use_ply_renderer=False):
            captured["src_ply"] = src_ply
            captured["use_ply_renderer"] = use_ply_renderer
            return {"skipped": "test stub"}

        monkeypatch.setattr(thumbnail_step, "run_thumbnail", fake_run_thumbnail)

        regen_job = await store.enqueue_job(
            scene.id, JobKind.thumbnail,
            payload={"use_edited_ply": True},
        )
        assert regen_job is not None
        await store.update_job(regen_job.id, status=JobStatus.running)

        live_job = await store.get_job(regen_job.id)
        assert live_job is not None
        live_scene = await store.get_scene(scene.id)
        assert live_scene is not None

        settings = Settings(data_dir=tmp_path, db_filename="test_filter_regen.sqlite")
        await runner._run_thumbnail(
            job=live_job, scene=live_scene, settings=settings,
        )

        assert captured["src_ply"] == Path(str(edited_ply)), (
            f"regen thumbnail must read edited_ply_path; got {captured.get('src_ply')}"
        )
        assert captured["use_ply_renderer"] is True

    _run(go())


def test_thumbnail_success_forwards_use_edited_ply_to_orbit(
    isolated_store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """The post-thumbnail orbit-enqueue must carry use_edited_ply
    when the parent thumbnail did. Otherwise the regen flow would
    produce: edited PNG + unedited orbit, which is worse than
    either consistent state."""
    async def go():
        cap = await store.create_capture(name="forward-flag", source="upload")
        scene = await store.create_scene(cap.id)
        assert scene is not None

        ply_path = tmp_path / "scene.ply"
        edited_ply = tmp_path / "edited.ply"
        _write_minimal_ply(ply_path)
        _write_minimal_ply(edited_ply)
        await store.update_scene(
            scene.id,
            ply_path=str(ply_path),
            edited_ply_path=str(edited_ply),
        )

        # Stub run_thumbnail to return a "successful" result; the
        # runner's post-success branch is what enqueues orbit.
        thumb_out = tmp_path / "thumb.png"
        thumb_out.write_bytes(b"fake png")

        async def fake_run_thumbnail(*, scene_dir, src_ply, progress, job_id=None, use_ply_renderer=False):
            return {"thumbnail": str(thumb_out)}

        monkeypatch.setattr(thumbnail_step, "run_thumbnail", fake_run_thumbnail)

        regen_thumb = await store.enqueue_job(
            scene.id, JobKind.thumbnail,
            payload={"use_edited_ply": True},
        )
        assert regen_thumb is not None
        await store.update_job(regen_thumb.id, status=JobStatus.running)

        live_job = await store.get_job(regen_thumb.id)
        assert live_job is not None
        live_scene = await store.get_scene(scene.id)
        assert live_scene is not None

        settings = Settings(data_dir=tmp_path, db_filename="test_filter_regen.sqlite")
        await runner._run_thumbnail(
            job=live_job, scene=live_scene, settings=settings,
        )

        jobs = await store.list_jobs_for_scene(scene.id)
        orbit_jobs = [j for j in jobs if j.kind == JobKind.orbit]
        assert len(orbit_jobs) == 1
        assert orbit_jobs[0].payload.get("use_edited_ply") is True, (
            f"regen orbit must carry use_edited_ply=True; got "
            f"payload {orbit_jobs[0].payload}"
        )

    _run(go())


def _write_minimal_ply(path: Path, *, scale: float = 1.0) -> None:
    """Minimal 8-corner PLY. Only x/y/z are populated; the renderer
    code reads more fields but those paths are GPU-only and the
    tests here all stub the renderer."""
    n = 8
    header = (
        "ply\n"
        "format binary_little_endian 1.0\n"
        f"element vertex {n}\n"
        "property float x\n"
        "property float y\n"
        "property float z\n"
        "end_header\n"
    ).encode("ascii")
    points = [
        (-scale, -scale, -scale),
        (-scale, -scale,  scale),
        (-scale,  scale, -scale),
        (-scale,  scale,  scale),
        ( scale, -scale, -scale),
        ( scale, -scale,  scale),
        ( scale,  scale, -scale),
        ( scale,  scale,  scale),
    ]
    body = b"".join(struct.pack("<fff", *p) for p in points)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(header + body)

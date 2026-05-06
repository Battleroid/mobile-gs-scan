"""Tests for the thumbnail render pipeline.

Covers the soft-failure surface that PR-D adds:
* JobKind.thumbnail is recognised by the schema + enum round-trip
* Dispatch order ends with thumbnail
* Camera computation handles empty / sparse / well-populated PLYs
* The render step skips cleanly when ns-render isn't on PATH (test
  hosts don't have nerfstudio installed; the runner treats the empty
  result as "no thumbnail produced" — no exception, no orphan job)

The actual GPU render (ns-render against a real splatfacto config)
isn't covered in pytest — that needs a CUDA worker; verify via
``make up`` smoke test instead.
"""
from __future__ import annotations

import asyncio
import json
import struct
from pathlib import Path

import pytest

from app.config import Settings
from app.jobs import runner, store
from app.jobs.schema import CaptureSource, CaptureStatus, JobKind, JobStatus
from app.pipeline import thumbnail as thumbnail_step


@pytest.fixture
def isolated_store(tmp_path: Path):
    """Per-test sqlite db (matches test_delete_capture pattern)."""
    settings = Settings(
        data_dir=tmp_path,
        db_filename="test_thumbnail.sqlite",
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


def test_jobkind_thumbnail_round_trips_through_db(isolated_store):
    """The new enum value must round-trip through the JSON-serialised
    JobStatus column; otherwise a worker that picks up a thumbnail
    job would crash on read.
    """
    async def go():
        cap = await store.create_capture(name="thumb-rt", source="upload")
        scene = await store.create_scene(cap.id)
        assert scene is not None
        job = await store.enqueue_job(scene.id, JobKind.thumbnail, payload={})
        assert job is not None
        assert job.kind == JobKind.thumbnail

        # Re-fetch to make sure it survives a serialisation cycle.
        refetched = await store.get_job(job.id)
        assert refetched is not None
        assert refetched.kind == JobKind.thumbnail

    _run(go())


def test_dispatch_pipeline_ends_with_thumbnail(isolated_store):
    """``enqueue_pipeline`` must enqueue thumbnail after export so
    the web home grid gets a rendered tile alongside the .ply / .spz.
    Without this, the pipeline would stop at export and the home
    page would only ever see gradient placeholders."""
    from app.pipeline.dispatch import enqueue_pipeline

    async def go():
        cap = await store.create_capture(name="dispatch", source="upload")
        scene = await store.create_scene(cap.id)
        assert scene is not None

        job_ids = await enqueue_pipeline(
            scene.id, has_pose=False, source=CaptureSource.upload,
        )
        assert job_ids is not None and len(job_ids) >= 1

        jobs = await store.list_jobs_for_scene(scene.id)
        kinds = [j.kind for j in jobs]
        # The exact pipeline shape depends on settings.sfm_backend,
        # but thumbnail is unconditional and must be the last step.
        assert JobKind.thumbnail in kinds, (
            "thumbnail must be in the pipeline; "
            "found kinds: " + ", ".join(k.value for k in kinds)
        )
        # Find the thumbnail job; its created_at should be the
        # latest of all jobs (= last enqueued).
        thumb_jobs = [j for j in jobs if j.kind == JobKind.thumbnail]
        assert len(thumb_jobs) == 1
        latest = max(jobs, key=lambda j: j.created_at)
        assert latest.kind == JobKind.thumbnail, (
            "thumbnail must be enqueued last so finalize-on-completion "
            "doesn't hit it before earlier steps run"
        )

    _run(go())


def test_run_thumbnail_skips_when_ns_render_unavailable(tmp_path: Path):
    """No ns-render on PATH (typical test host) → step returns an
    empty result rather than raising. The runner branches on this
    to mark the job completed-with-skip rather than failed."""
    scene_dir = tmp_path / "scene"
    train_dir = scene_dir / "train"
    train_dir.mkdir(parents=True)
    # Write a config.yml so the path-existence checks pass
    (train_dir / "config.yml").write_text("# stub")
    src_ply = scene_dir / "scene.ply"
    _write_minimal_ply(src_ply)

    progress_calls: list[tuple[float, str]] = []

    async def progress(pct: float, msg: str) -> None:
        progress_calls.append((pct, msg))

    async def go():
        # Force ns-render to look unavailable. shutil.which works by
        # consulting PATH; setting it to /nonexistent makes any
        # binary lookup return None.
        import os
        prior_path = os.environ.get("PATH", "")
        os.environ["PATH"] = "/__pebble_pytest_no_path__"
        try:
            return await thumbnail_step.run_thumbnail(
                scene_dir=scene_dir,
                src_ply=src_ply,
                progress=progress,
            )
        finally:
            os.environ["PATH"] = prior_path

    result = _run(go())
    assert result == {}
    # Should still report progress so the UI sees the step finishing
    # (even if it didn't produce an artifact).
    assert any(p[0] == 1.0 for p in progress_calls), (
        "skip path must still publish a final progress tick so the "
        "pipeline panel shows the step as done"
    )


def test_run_thumbnail_skips_for_stub_scene(tmp_path: Path):
    """Stub training writes ``synthetic.json`` to the train dir.
    Thumbnail must skip rather than try to render a placeholder
    .ply through ns-render (which would either crash or render
    a useless single-gaussian frame)."""
    scene_dir = tmp_path / "scene"
    train_dir = scene_dir / "train"
    train_dir.mkdir(parents=True)
    (train_dir / "synthetic.json").write_text("{}")
    src_ply = scene_dir / "scene.ply"
    _write_minimal_ply(src_ply)

    async def progress(pct: float, msg: str) -> None:
        pass

    result = _run(thumbnail_step.run_thumbnail(
        scene_dir=scene_dir, src_ply=src_ply, progress=progress,
    ))
    assert result == {}


def test_camera_for_ply_handles_empty(tmp_path: Path):
    """Empty / unparseable PLY must not crash the camera builder.
    Falls back to a default camera at the origin."""
    bogus = tmp_path / "bogus.ply"
    bogus.write_bytes(b"not a real ply file")
    # Should fall back without raising.
    cam = thumbnail_step._camera_for_ply(bogus)
    assert isinstance(cam, list)
    assert len(cam) == 16
    # The translation column (last column of a row-major 4×4) sits
    # at indices [3, 7, 11, 15]; with eye = (cx, cy + 0.35d, cz + d)
    # and the fallback centroid = origin + extent=1, the camera
    # ends up offset from the origin (i.e. non-zero).
    tz = cam[11]
    assert tz != 0.0, "fallback camera must sit somewhere off-origin"


def test_camera_for_ply_frames_actual_bbox(tmp_path: Path):
    """Camera distance scales with bbox extent so the splat is
    actually framed instead of cropped or lost in the distance."""
    ply_small = tmp_path / "small.ply"
    _write_minimal_ply(ply_small, scale=0.1)
    ply_large = tmp_path / "large.ply"
    _write_minimal_ply(ply_large, scale=10.0)

    cam_small = thumbnail_step._camera_for_ply(ply_small)
    cam_large = thumbnail_step._camera_for_ply(ply_large)

    # Camera Z (12th flat-index, row-major translation column) on a
    # larger bbox should be farther away.
    z_small = cam_small[11]
    z_large = cam_large[11]
    assert z_large > z_small, (
        f"camera should pull back for larger bboxes "
        f"(small_z={z_small:.3f} vs large_z={z_large:.3f})"
    )


def test_render_camera_path_json_round_trips():
    """The ns-render camera_path JSON must parse back into the
    expected shape — nerfstudio is strict about the schema."""
    cam = [
        1.0, 0.0, 0.0, 0.0,
        0.0, 1.0, 0.0, 0.0,
        0.0, 0.0, 1.0, 3.0,
        0.0, 0.0, 0.0, 1.0,
    ]
    raw = thumbnail_step._render_camera_path_json(cam)
    parsed = json.loads(raw)
    assert parsed["render_width"] == thumbnail_step.THUMB_W
    assert parsed["render_height"] == thumbnail_step.THUMB_H
    assert len(parsed["camera_path"]) == 1
    kf = parsed["camera_path"][0]
    assert kf["camera_to_world"] == cam
    assert kf["fov"] == thumbnail_step.DEFAULT_FOV_DEG


def test_run_thumbnail_cancel_finalizes_scene(
    isolated_store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Thumbnail is the last enqueued step, so the export-time
    ``_maybe_finalize_scene`` call sees thumbnail still queued and
    bails. If a thumbnail job then gets canceled, the cancel-ack
    branch in ``_run_thumbnail`` MUST itself call
    ``_maybe_finalize_scene`` — otherwise the scene stays stuck at
    ``processing`` forever.

    Regression for the codex P1 on PR #82 that flagged this.
    """
    async def go():
        cap = await store.create_capture(name="cancel-finalize", source="upload")
        scene = await store.create_scene(cap.id)
        assert scene is not None

        # Bring the scene + capture to a state where every other
        # job is already terminal — extract / sfm / train / export
        # all completed. Thumbnail is the only outstanding work.
        # Set ply_path to model what export's success branch would
        # have done; without this _maybe_finalize_scene's missing-
        # ply guard would (correctly) refuse to flip to completed,
        # since a real completed export always writes the .ply.
        ply_path = tmp_path / "scene.ply"
        ply_path.write_bytes(b"fake ply for fixture")
        await store.update_scene(scene.id, ply_path=str(ply_path))
        for kind in (JobKind.extract, JobKind.sfm, JobKind.train, JobKind.export):
            j = await store.enqueue_job(scene.id, kind, payload={})
            assert j is not None
            await store.update_job(
                j.id, status=JobStatus.completed, completed=True,
            )
        thumb_job = await store.enqueue_job(scene.id, JobKind.thumbnail, payload={})
        assert thumb_job is not None
        await store.update_job(thumb_job.id, status=JobStatus.running)

        # Pre-condition: scene is still ``queued`` (initial state)
        # because the thumbnail job is non-terminal.
        scene_before = await store.get_scene(scene.id)
        assert scene_before is not None
        assert scene_before.status != CaptureStatus.completed

        # Now flip the thumbnail row to canceled (the user-cancel
        # path) and stub thumbnail_step.run_thumbnail to raise
        # CancelledError — same shape as the heartbeat-driven kill
        # actually triggers in production.
        await store.cancel_job(thumb_job.id)

        async def fake_run(*args, **kwargs):
            raise asyncio.CancelledError

        monkeypatch.setattr(thumbnail_step, "run_thumbnail", fake_run)

        # Re-fetch the live job + scene (the runner takes them as
        # snapshots; we mirror that).
        live_job = await store.get_job(thumb_job.id)
        assert live_job is not None
        live_scene = await store.get_scene(scene.id)
        assert live_scene is not None

        settings = Settings(
            data_dir=tmp_path,
            db_filename="test_thumbnail.sqlite",
        )
        await runner._run_thumbnail(
            job=live_job, scene=live_scene, settings=settings,
        )

        # Post-condition: scene flipped to completed because the
        # thumbnail's cancel ack triggers _maybe_finalize_scene.
        scene_after = await store.get_scene(scene.id)
        assert scene_after is not None
        assert scene_after.status == CaptureStatus.completed, (
            f"scene must finalize on thumbnail cancel; got "
            f"{scene_after.status.value}"
        )
        cap_after = await store.get_capture(cap.id)
        assert cap_after is not None
        assert cap_after.status == CaptureStatus.completed

    _run(go())


def test_run_thumbnail_cancel_skips_finalize_when_scene_deleted(
    isolated_store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """When the cancel-ack came from a capture-delete cascade
    (job row gone, scene gone), ``_run_thumbnail`` MUST NOT call
    ``_maybe_finalize_scene`` against the now-absent scene.
    Otherwise it sees an empty job list, passes the all-terminal
    check, and publishes a spurious ``scene.completed`` event for
    a capture that's already deleted — which corrupts websocket
    subscriber state.

    Regression for the codex P2 on PR #82.
    """
    published: list[tuple[str, str]] = []

    async def fake_publish_scene(scene_id: str, kind: str, **kwargs) -> None:
        published.append((scene_id, kind))

    async def go():
        from app.jobs import events

        cap = await store.create_capture(name="delete-ack", source="upload")
        scene = await store.create_scene(cap.id)
        assert scene is not None
        thumb_job = await store.enqueue_job(scene.id, JobKind.thumbnail, payload={})
        assert thumb_job is not None
        await store.update_job(thumb_job.id, status=JobStatus.running)

        # Snapshot the live job + scene the way the runner would.
        live_job = await store.get_job(thumb_job.id)
        assert live_job is not None
        live_scene = await store.get_scene(scene.id)
        assert live_scene is not None

        # Simulate the capture-delete cascade — every row for this
        # scene is gone (jobs + scene + capture). _ack_user_cancel
        # now sees the missing row + treats it as cancel-ack.
        await store.delete_capture(cap.id)
        assert await store.get_scene(scene.id) is None

        # Stub the render to raise CancelledError so the cancel
        # branch triggers, and stub publish_scene so we can inspect
        # what events fire (or don't).
        async def fake_run(*args, **kwargs):
            raise asyncio.CancelledError

        monkeypatch.setattr(thumbnail_step, "run_thumbnail", fake_run)
        monkeypatch.setattr(events, "publish_scene", fake_publish_scene)

        settings = Settings(
            data_dir=tmp_path,
            db_filename="test_thumbnail.sqlite",
        )
        await runner._run_thumbnail(
            job=live_job, scene=live_scene, settings=settings,
        )

        # The scene event topic should NOT have seen a completed
        # event because the scene is gone — the bug Codex flagged.
        completed_events = [
            e for e in published if e[1] == "scene.completed"
        ]
        assert completed_events == [], (
            f"scene.completed must NOT publish for a deleted scene "
            f"(saw: {published})"
        )

    _run(go())


def test_run_thumbnail_pre_commit_cancel_guard_skips_thumb_url_event(
    isolated_store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """Race window: dispatch_task completes successfully (PNG on
    disk) in the gap between two heartbeat ticks, but the user has
    already canceled or deleted in the meantime. The success
    branch must NOT commit thumbnail_path or emit
    scene.thumbnail_ready in that window — both would leak terminal
    events for a row the user just removed.

    Regression for the codex P2 on PR #82 about pre-commit cancel
    guards. Mirrors the same guard already in _run_filter /
    _run_mesh.
    """
    published_scene: list[tuple[str, str]] = []
    published_job: list[tuple[str, str]] = []

    async def fake_publish_scene(scene_id: str, kind: str, **kwargs) -> None:
        published_scene.append((scene_id, kind))

    async def fake_publish_job(job_id: str, kind: str, **kwargs) -> None:
        published_job.append((job_id, kind))

    async def go():
        from app.jobs import events

        cap = await store.create_capture(name="precommit", source="upload")
        scene = await store.create_scene(cap.id)
        assert scene is not None
        # Set a ply_path so _run_thumbnail enters the dispatch path
        # rather than the no-ply skip branch.
        ply_path = tmp_path / "scene.ply"
        _write_minimal_ply(ply_path)
        await store.update_scene(scene.id, ply_path=str(ply_path))

        thumb_job = await store.enqueue_job(scene.id, JobKind.thumbnail, payload={})
        assert thumb_job is not None
        await store.update_job(thumb_job.id, status=JobStatus.running)

        # Simulate a successful render but cancel-during-render —
        # dispatch_task returns a thumbnail path, then we cancel
        # the job row before _run_thumbnail's post-dispatch path
        # gets to commit anything.
        rendered_path = tmp_path / "thumb.png"
        rendered_path.write_bytes(b"fake png")

        async def fake_run(*args, **kwargs):
            # Flip the job status to canceled mid-render — same
            # state the heartbeat would observe a tick later.
            await store.cancel_job(thumb_job.id)
            return {"thumbnail": str(rendered_path)}

        monkeypatch.setattr(thumbnail_step, "run_thumbnail", fake_run)
        monkeypatch.setattr(events, "publish_scene", fake_publish_scene)
        monkeypatch.setattr(events, "publish_job", fake_publish_job)

        live_job = await store.get_job(thumb_job.id)
        assert live_job is not None
        live_scene = await store.get_scene(scene.id)
        assert live_scene is not None

        settings = Settings(
            data_dir=tmp_path,
            db_filename="test_thumbnail.sqlite",
        )
        await runner._run_thumbnail(
            job=live_job, scene=live_scene, settings=settings,
        )

        # Pre-commit guard must have caught the canceled status:
        # no thumbnail_ready / job.completed events for this run,
        # only the cancel-ack job.canceled.
        ready_events = [e for e in published_scene if e[1] == "scene.thumbnail_ready"]
        assert ready_events == [], (
            "scene.thumbnail_ready must NOT publish when the job "
            "row is already canceled by the time dispatch returns "
            f"(saw: {published_scene})"
        )
        completed_events = [e for e in published_job if e[1] == "job.completed"]
        assert completed_events == [], (
            "job.completed must NOT publish on the canceled path "
            f"(saw: {published_job})"
        )
        canceled_events = [e for e in published_job if e[1] == "job.canceled"]
        assert canceled_events, (
            "job.canceled must publish so subscribers see the "
            f"terminal event (saw: {published_job})"
        )

        # And thumbnail_path must NOT have been written.
        scene_now = await store.get_scene(scene.id)
        assert scene_now is not None
        assert scene_now.thumbnail_path is None, (
            "thumbnail_path must not be committed when the row is "
            "canceled at pre-commit time"
        )

    _run(go())


def test_finalize_treats_missing_ply_as_canceled_not_completed(
    isolated_store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    """User-visible bug: cancelling export from the pipeline UI
    leaves the scene without a .ply, but the queued thumbnail job
    still runs (skip-with-no-ply branch) and then triggers
    _maybe_finalize_scene. Pre-fix, _maybe_finalize_scene would see
    every job terminal (export=canceled, thumbnail=completed) +
    no failures, and flip the scene/capture to ``completed`` —
    leaving a 'ready' capture whose /artifacts/ply 404s.

    Post-fix, the missing .ply triggers a 'canceled' end-state
    instead, which is what the UI should actually surface.

    Regression for the codex P1 on PR #82.
    """
    async def go():
        cap = await store.create_capture(name="export-canceled", source="upload")
        scene = await store.create_scene(cap.id)
        assert scene is not None
        # Stage every essential upstream job. extract/sfm/train all
        # ran cleanly; export was canceled mid-flight (no .ply
        # written); thumbnail then ran and skipped because there's
        # no source ply.
        for kind, status in (
            (JobKind.extract, JobStatus.completed),
            (JobKind.sfm, JobStatus.completed),
            (JobKind.train, JobStatus.completed),
            (JobKind.export, JobStatus.canceled),
            (JobKind.thumbnail, JobStatus.completed),
        ):
            j = await store.enqueue_job(scene.id, kind, payload={})
            assert j is not None
            await store.update_job(
                j.id, status=status, completed=True,
            )

        # Sanity: scene has no ply_path yet (export canceled).
        live_scene = await store.get_scene(scene.id)
        assert live_scene is not None
        assert live_scene.ply_path is None

        await runner._maybe_finalize_scene(live_scene)

        # The scene must NOT flip to completed without a .ply —
        # that would surface as 'ready' in the UI but every viewer
        # / download path would 404. Mark it canceled instead.
        scene_after = await store.get_scene(scene.id)
        assert scene_after is not None
        assert scene_after.status == CaptureStatus.canceled, (
            f"scene with no .ply must finalize as canceled, not "
            f"completed (got {scene_after.status.value})"
        )
        cap_after = await store.get_capture(cap.id)
        assert cap_after is not None
        assert cap_after.status == CaptureStatus.canceled

    _run(go())


def test_finalize_completes_when_ply_present(
    isolated_store, tmp_path: Path
):
    """The happy path stays intact: scene with all jobs terminal,
    no failures, AND a ply_path on disk → flip to completed.
    """
    async def go():
        cap = await store.create_capture(name="happy", source="upload")
        scene = await store.create_scene(cap.id)
        assert scene is not None
        ply_path = tmp_path / "scene.ply"
        _write_minimal_ply(ply_path)
        await store.update_scene(scene.id, ply_path=str(ply_path))

        for kind in (
            JobKind.extract, JobKind.sfm, JobKind.train,
            JobKind.export, JobKind.thumbnail,
        ):
            j = await store.enqueue_job(scene.id, kind, payload={})
            assert j is not None
            await store.update_job(
                j.id, status=JobStatus.completed, completed=True,
            )

        live_scene = await store.get_scene(scene.id)
        assert live_scene is not None
        await runner._maybe_finalize_scene(live_scene)

        scene_after = await store.get_scene(scene.id)
        assert scene_after is not None
        assert scene_after.status == CaptureStatus.completed
        cap_after = await store.get_capture(cap.id)
        assert cap_after is not None
        assert cap_after.status == CaptureStatus.completed

    _run(go())


def test_backfill_enqueues_for_pre_existing_completed_scenes(
    isolated_store, tmp_path: Path
):
    """Captures that completed before JobKind.thumbnail shipped
    have a ply_path but no thumbnail_path and no thumbnail jobs.
    The boot-time backfill must pick them up and enqueue a render.
    """
    async def go():
        cap = await store.create_capture(name="pre-existing", source="upload")
        scene = await store.create_scene(cap.id)
        assert scene is not None
        ply_path = tmp_path / "scene.ply"
        _write_minimal_ply(ply_path)
        await store.update_scene(scene.id, ply_path=str(ply_path))

        # Pre-condition: no thumbnail job exists, scene has ply.
        needing = await store.list_scenes_needing_thumbnail()
        assert any(s.id == scene.id for s in needing)

        await runner._backfill_thumbnails()

        # Post-condition: a queued thumbnail job is now on the
        # scene, and the same scene no longer shows up in the
        # needing-thumbnail set (since there's an active job).
        jobs = await store.list_jobs_for_scene(scene.id)
        thumb_jobs = [j for j in jobs if j.kind == JobKind.thumbnail]
        assert len(thumb_jobs) == 1
        assert thumb_jobs[0].status == JobStatus.queued

        needing_after = await store.list_scenes_needing_thumbnail()
        assert all(s.id != scene.id for s in needing_after)

    _run(go())


def test_backfill_skips_scenes_with_active_thumbnail_job(
    isolated_store, tmp_path: Path
):
    """Worker restart mid-backfill must not double-enqueue. Any
    scene already carrying a queued/running/claimed thumbnail job
    is excluded from the needing-thumbnail set.
    """
    async def go():
        cap = await store.create_capture(name="already-queued", source="upload")
        scene = await store.create_scene(cap.id)
        assert scene is not None
        ply_path = tmp_path / "scene.ply"
        _write_minimal_ply(ply_path)
        await store.update_scene(scene.id, ply_path=str(ply_path))
        # Pre-existing queued thumbnail job (worker crashed before
        # claiming it).
        existing = await store.enqueue_job(
            scene.id, JobKind.thumbnail, payload={}
        )
        assert existing is not None

        # Backfill must NOT add a second thumbnail job.
        await runner._backfill_thumbnails()
        jobs = await store.list_jobs_for_scene(scene.id)
        thumb_jobs = [j for j in jobs if j.kind == JobKind.thumbnail]
        assert len(thumb_jobs) == 1, (
            "backfill must skip scenes that already have a queued "
            "thumbnail job; got "
            + ", ".join(f"{j.id}={j.status.value}" for j in thumb_jobs)
        )

    _run(go())


def test_backfill_skips_scenes_already_with_thumbnail(
    isolated_store, tmp_path: Path
):
    """Scenes with thumbnail_path already set are off-limits — we
    don't want backfill ever clobbering / re-rendering work the
    earlier pipeline produced cleanly."""
    async def go():
        cap = await store.create_capture(name="has-thumb", source="upload")
        scene = await store.create_scene(cap.id)
        assert scene is not None
        ply_path = tmp_path / "scene.ply"
        thumb_path = tmp_path / "thumb.png"
        _write_minimal_ply(ply_path)
        thumb_path.write_bytes(b"png")
        await store.update_scene(
            scene.id, ply_path=str(ply_path), thumbnail_path=str(thumb_path),
        )

        await runner._backfill_thumbnails()
        jobs = await store.list_jobs_for_scene(scene.id)
        thumb_jobs = [j for j in jobs if j.kind == JobKind.thumbnail]
        assert thumb_jobs == [], (
            "backfill must not touch scenes that already have a "
            "thumbnail_path"
        )

    _run(go())


def test_backfill_skips_scenes_without_ply_path(
    isolated_store, tmp_path: Path
):
    """No .ply means there's nothing to render; backfill must
    leave such scenes alone (they were canceled mid-pipeline)."""
    async def go():
        cap = await store.create_capture(name="no-ply", source="upload")
        scene = await store.create_scene(cap.id)
        assert scene is not None
        # Don't set ply_path.

        await runner._backfill_thumbnails()
        jobs = await store.list_jobs_for_scene(scene.id)
        assert all(j.kind != JobKind.thumbnail for j in jobs)

    _run(go())


# ─── helpers ───────────────────────────────────────────────


def _write_minimal_ply(path: Path, *, scale: float = 1.0) -> None:
    """Tiny binary PLY with 8 cube-corner gaussians scaled by
    ``scale``, sufficient for bbox math in the camera builder.
    """
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
    path.write_bytes(header + body)

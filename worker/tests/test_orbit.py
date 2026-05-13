"""Tests for the orbit-MP4 thumbnail companion (Phase 3 PR-A).

Covers the soft-failure surface that the new step adds:
* JobKind.orbit round-trips through the DB enum.
* Stub-scene / ns-render-missing / ffmpeg-missing all return
  ``permanent_skip`` markers (same shape as thumbnail.py).
* The camera-path JSON serializes 24 keyframes at the expected
  output dimensions.
* maybe_finalize_scene treats orbit as non-blocking — a queued
  orbit must not hold the scene at ``processing``.
* The thumbnail's success path enqueues a follow-up orbit job.

GPU-side rendering (ns-render + ffmpeg) isn't covered in pytest;
verify via ``make up`` smoke test.
"""
from __future__ import annotations

import asyncio
import json
import struct
from pathlib import Path

import pytest

from app.config import Settings
from app.jobs import finalize, runner, store
from app.jobs.schema import CaptureStatus, JobKind, JobStatus
from app.pipeline import orbit as orbit_step
from app.pipeline import thumbnail as thumbnail_step


@pytest.fixture
def isolated_store(tmp_path: Path):
    settings = Settings(data_dir=tmp_path, db_filename="test_orbit.sqlite")

    async def setup():
        await store.init_store(settings)

    async def teardown():
        await store.shutdown_store()

    asyncio.run(setup())
    yield
    asyncio.run(teardown())


def _run(coro):
    return asyncio.run(coro)


def test_jobkind_orbit_round_trips_through_db(isolated_store):
    """JobKind.orbit must round-trip through the JSON-serialised
    JobStatus column; otherwise a worker that picks up an orbit
    job would crash on read."""
    async def go():
        cap = await store.create_capture(name="orbit-rt", source="upload")
        scene = await store.create_scene(cap.id)
        assert scene is not None
        job = await store.enqueue_job(scene.id, JobKind.orbit, payload={})
        assert job is not None
        assert job.kind == JobKind.orbit
        refetched = await store.get_job(job.id)
        assert refetched is not None
        assert refetched.kind == JobKind.orbit

    _run(go())


def test_run_orbit_skips_when_ns_render_unavailable(tmp_path: Path):
    """No ns-render on PATH → permanent_skip marker, no exception."""
    scene_dir = tmp_path / "scene"
    train_dir = scene_dir / "train"
    train_dir.mkdir(parents=True)
    (train_dir / "config.yml").write_text("# stub")
    src_ply = scene_dir / "scene.ply"
    _write_minimal_ply(src_ply)

    progress_calls: list[tuple[float, str]] = []

    async def progress(pct: float, msg: str) -> None:
        progress_calls.append((pct, msg))

    async def go():
        import os
        prior_path = os.environ.get("PATH", "")
        os.environ["PATH"] = "/__pebble_pytest_no_path__"
        try:
            return await orbit_step.run_orbit(
                scene_dir=scene_dir,
                src_ply=src_ply,
                progress=progress,
            )
        finally:
            os.environ["PATH"] = prior_path

    result = _run(go())
    # Clearing PATH knocks out ffmpeg as well as ns-render. orbit's
    # early ffmpeg check fires first (both renderer paths need it),
    # so that's the marker we expect. Same backfill semantics
    # either way — any permanent_skip pin holds.
    assert "permanent_skip" in result
    # Final progress tick keeps the pipeline panel honest.
    assert any(p[0] == 1.0 for p in progress_calls)


def test_run_orbit_skips_for_stub_scene(tmp_path: Path):
    """Stub training (synthetic.json) → skip; the stub scene has
    no real splatfacto checkpoint to orbit around."""
    scene_dir = tmp_path / "scene"
    train_dir = scene_dir / "train"
    train_dir.mkdir(parents=True)
    (train_dir / "synthetic.json").write_text("{}")
    src_ply = scene_dir / "scene.ply"
    _write_minimal_ply(src_ply)

    async def progress(pct: float, msg: str) -> None:
        pass

    result = _run(orbit_step.run_orbit(
        scene_dir=scene_dir, src_ply=src_ply, progress=progress,
    ))
    assert result == {"permanent_skip": "synthetic stub scene"}


def test_orbit_camera_path_json_round_trips():
    """Camera-path JSON serializes 24 keyframes with the right
    render dimensions + matching aspect, and the keyframes form a
    closed loop so the <video> element can `loop` without a visible
    jump."""
    poses = [
        [
            1.0, 0.0, 0.0, float(i),
            0.0, 1.0, 0.0, 0.0,
            0.0, 0.0, 1.0, 0.0,
            0.0, 0.0, 0.0, 1.0,
        ]
        for i in range(orbit_step.ORBIT_FRAMES)
    ]
    raw = orbit_step._orbit_camera_path_json(poses)
    parsed = json.loads(raw)
    assert parsed["render_width"] == orbit_step.ORBIT_W
    assert parsed["render_height"] == orbit_step.ORBIT_H
    assert parsed["fps"] == orbit_step.ORBIT_FPS
    assert pytest.approx(parsed["seconds"]) == orbit_step.ORBIT_SECONDS
    assert len(parsed["camera_path"]) == orbit_step.ORBIT_FRAMES
    # Each entry must carry a deterministic file_path the post-
    # render rename step uses to find frames in order.
    for i, entry in enumerate(parsed["camera_path"]):
        assert entry["file_path"] == f"frames/{i:05d}.png"
        assert entry["fov"] == orbit_step.DEFAULT_FOV_DEG
    # is_cycle is metadata-only (nerfstudio interpolates with it
    # for smoothness_value > 0, which we set to 0 — so it's a no-op
    # on the render side but documents the closed-loop intent).
    assert parsed["is_cycle"] is True


def test_orbit_camera_path_circles_centroid(tmp_path: Path):
    """The 24 camera positions should sit at a roughly constant
    radius from the splat's centroid — that's the geometric
    invariant of an orbit. Without this, a math regression in
    _orbit_camera_path could ship a non-circular path that the
    grid sees as a jittery zig-zag."""
    src_ply = tmp_path / "scene.ply"
    _write_minimal_ply(src_ply, scale=1.0)
    poses = orbit_step._orbit_camera_path(src_ply)
    assert len(poses) == orbit_step.ORBIT_FRAMES

    # camera_to_world column 3 is the camera position in world
    # space (rows 0-2 of the last column). Pull (x, y, z) for each
    # frame; all should be at roughly the same distance from the
    # centroid (0, 0, 0 since the cube is centered there).
    import math
    radii = []
    for pose in poses:
        # Row-major flatten; position is at indices 3, 7, 11.
        x, y, z = pose[3], pose[7], pose[11]
        radii.append(math.sqrt(x * x + y * y + z * z))
    avg = sum(radii) / len(radii)
    # Allow some tolerance because the camera is lifted along +Y
    # (the constant 0.35 * distance) so the radius from origin
    # varies between frames by exactly the Y-lift relative to the
    # XZ radius. But every frame should have IDENTICAL radius by
    # symmetry — let's just check that.
    for r in radii:
        assert abs(r - avg) < 1e-3, (
            f"orbit must be a circle; frame radii vary by "
            f"{abs(r - avg):.4f} from average {avg:.4f}"
        )


def test_finalize_does_not_wait_for_orbit(isolated_store, tmp_path: Path):
    """maybe_finalize_scene must skip JobKind.orbit when checking
    that all jobs are terminal. The two-stage thumbnail promise:
    scene flips to ``completed`` when the still PNG lands, the MP4
    orbit backfills without blocking that flip. Without the
    NON_BLOCKING_KINDS gate, a queued orbit would keep the scene
    stuck at ``processing`` indefinitely.
    """
    async def go():
        cap = await store.create_capture(name="orbit-finalize", source="upload")
        scene = await store.create_scene(cap.id)
        assert scene is not None

        # Stage: every blocking job done, ply_path written, orbit
        # still queued.
        ply_path = tmp_path / "scene.ply"
        ply_path.write_bytes(b"fake ply")
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
        orbit_job = await store.enqueue_job(scene.id, JobKind.orbit, payload={})
        assert orbit_job is not None
        # orbit_job stays at JobStatus.queued.

        await finalize.maybe_finalize_scene(scene.id)

        refreshed = await store.get_scene(scene.id)
        assert refreshed is not None
        assert refreshed.status == CaptureStatus.completed, (
            f"scene must finalize with orbit still queued; "
            f"got {refreshed.status.value}"
        )
        cap_after = await store.get_capture(cap.id)
        assert cap_after is not None
        assert cap_after.status == CaptureStatus.completed

    _run(go())


def test_run_thumbnail_enqueues_orbit_on_success(
    isolated_store, tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    """Thumbnail's success path enqueues the follow-up orbit job.
    Without this, the orbit would never run unless triggered by a
    separate dispatch + backfill path."""
    async def go():
        cap = await store.create_capture(name="thumb-then-orbit", source="upload")
        scene = await store.create_scene(cap.id)
        assert scene is not None
        ply_path = tmp_path / "scene.ply"
        ply_path.write_bytes(b"fake ply for fixture")
        await store.update_scene(scene.id, ply_path=str(ply_path))

        # Stage a thumbnail job sitting at running.
        thumb_job = await store.enqueue_job(scene.id, JobKind.thumbnail, payload={})
        assert thumb_job is not None
        await store.update_job(thumb_job.id, status=JobStatus.running)

        # Stub the thumbnail step to "succeed" with a thumbnail
        # result. Real run_thumbnail would invoke ns-render, which
        # the test host doesn't have; we want to exercise the
        # post-success branch where _run_thumbnail enqueues orbit.
        thumb_out = tmp_path / "thumb.png"
        thumb_out.write_bytes(b"fake png")

        async def fake_run(*args, **kwargs):
            return {"thumbnail": str(thumb_out)}

        monkeypatch.setattr(thumbnail_step, "run_thumbnail", fake_run)

        live_job = await store.get_job(thumb_job.id)
        assert live_job is not None
        live_scene = await store.get_scene(scene.id)
        assert live_scene is not None

        settings = Settings(data_dir=tmp_path, db_filename="test_orbit.sqlite")
        await runner._run_thumbnail(
            job=live_job, scene=live_scene, settings=settings,
        )

        jobs = await store.list_jobs_for_scene(scene.id)
        kinds = [j.kind for j in jobs]
        assert JobKind.orbit in kinds, (
            "thumbnail's success path must enqueue a follow-up orbit "
            f"job; saw kinds {[k.value for k in kinds]}"
        )
        orbit_jobs = [j for j in jobs if j.kind == JobKind.orbit]
        assert len(orbit_jobs) == 1
        assert orbit_jobs[0].status == JobStatus.queued

    _run(go())


def _write_minimal_ply(path: Path, *, scale: float = 1.0) -> None:
    """8-corner cube PLY, big-enough bbox for orbit's distance fit
    to produce non-degenerate camera positions."""
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

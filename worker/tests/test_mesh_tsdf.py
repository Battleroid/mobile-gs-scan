"""Tests for the TSDF-tier mesh extraction surface.

The pytest environment can't run the actual TSDF subprocess (needs
CUDA + gsplat + open3d to do real work), so this file focuses on
the wrapper surfaces that DON'T need a GPU:

* Param-validation contract on POST /api/scenes/{id}/mesh — tier,
  n_views, view_elevations, voxel_size, sdf_trunc_mult, depth_trunc.
* The trigger endpoint's persisted-tier guard — if the user omits
  ``tier`` and the persisted ``scene.mesh_params`` row carries an
  inactive tier, the request must 422 rather than queue a doomed
  job that fails in the worker.
* Tier dispatch in ``mesh.run_mesh`` — standard / higher tiers must
  surface ``NotImplementedError`` so the runner writes a clean
  ``mesh_error`` rather than queuing a doomed subprocess.
* Pure-math helpers — ``ply_render.opencv_extrinsic_from_opengl``
  (numpy-only, exercises the OpenGL→OpenCV flip used by the TSDF
  integrate path).

End-to-end (real splat → mesh) verification happens via the dev
``make up`` smoke test described in the plan / PR.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import numpy as np
import pytest
from fastapi import HTTPException

from app.api.scenes import _validate_mesh_params, trigger_mesh, MeshRequest
from app.config import Settings
from app.jobs import store
from app.jobs.schema import CaptureStatus, MeshStatus
from app.pipeline import mesh as mesh_step
from app.pipeline import ply_render


def _run(coro):
    return asyncio.run(coro)


# ─── validation: tier ─────────────────────────────────────────


def test_validate_mesh_params_accepts_low_tier():
    out = _validate_mesh_params({"tier": "low"})
    assert out == {"tier": "low"}


def test_validate_mesh_params_rejects_standard_tier():
    """Standard tier is in the allowed-keys allowlist (so older
    clients can submit it without 422'ing on the unknown-key
    check) but the trigger endpoint refuses to enqueue it today.
    Mirror the runtime behaviour in the validator."""
    with pytest.raises(HTTPException) as exc:
        _validate_mesh_params({"tier": "standard"})
    assert exc.value.status_code == 422
    assert "not yet implemented" in exc.value.detail


def test_validate_mesh_params_rejects_higher_tier():
    with pytest.raises(HTTPException) as exc:
        _validate_mesh_params({"tier": "higher"})
    assert exc.value.status_code == 422


def test_validate_mesh_params_rejects_unknown_tier():
    with pytest.raises(HTTPException) as exc:
        _validate_mesh_params({"tier": "bogus"})
    assert exc.value.status_code == 422


# ─── validation: TSDF knobs ──────────────────────────────────


def test_validate_mesh_params_accepts_full_tsdf_knob_set():
    out = _validate_mesh_params({
        "tier": "low",
        "n_views": 144,
        "view_elevations": [0.0, 0.3, 0.7],
        "voxel_size": 0.004,
        "sdf_trunc_mult": 3.0,
        "depth_trunc": 6.0,
        "remove_outliers": False,
        "use_bounding_box": True,
    })
    assert out["n_views"] == 144
    assert out["view_elevations"] == [0.0, 0.3, 0.7]
    assert out["voxel_size"] == pytest.approx(0.004)
    assert out["sdf_trunc_mult"] == pytest.approx(3.0)
    assert out["depth_trunc"] == pytest.approx(6.0)
    assert out["remove_outliers"] is False
    assert out["use_bounding_box"] is True


@pytest.mark.parametrize("bad", [0, 23, 361, 1000, -1, True, "96"])
def test_validate_mesh_params_rejects_bad_n_views(bad):
    with pytest.raises(HTTPException) as exc:
        _validate_mesh_params({"n_views": bad})
    assert exc.value.status_code == 422


@pytest.mark.parametrize("bad", [
    [],                # empty
    [0.0, 0.0, 0.0, 0.0, 0.0],  # too long
    [1.1],             # out of range
    [-1.5],            # out of range
    [True, 0.5],       # bool entry
    ["0.5"],           # string entry
    "not a list",      # not a list
])
def test_validate_mesh_params_rejects_bad_view_elevations(bad):
    with pytest.raises(HTTPException) as exc:
        _validate_mesh_params({"view_elevations": bad})
    assert exc.value.status_code == 422


@pytest.mark.parametrize("bad", [0.0, -0.001, 0.5, True, "0.005"])
def test_validate_mesh_params_rejects_bad_voxel_size(bad):
    with pytest.raises(HTTPException) as exc:
        _validate_mesh_params({"voxel_size": bad})
    assert exc.value.status_code == 422


@pytest.mark.parametrize("bad", [0.5, 9.0, True, "4"])
def test_validate_mesh_params_rejects_bad_sdf_trunc_mult(bad):
    with pytest.raises(HTTPException) as exc:
        _validate_mesh_params({"sdf_trunc_mult": bad})
    assert exc.value.status_code == 422


@pytest.mark.parametrize("bad", [0.5, 100.0, True, "8"])
def test_validate_mesh_params_rejects_bad_depth_trunc(bad):
    with pytest.raises(HTTPException) as exc:
        _validate_mesh_params({"depth_trunc": bad})
    assert exc.value.status_code == 422


def test_validate_mesh_params_accepts_legacy_poisson_keys_silently():
    """Older mesh_params rows persist num_points / depth /
    density_quantile / normal_method. The new validator should
    still accept them (so a forward-rolled web client can re-extract
    the scene without a 422) but the TSDF subprocess ignores them."""
    out = _validate_mesh_params({
        "num_points": 500_000,
        "depth": 9,
        "density_quantile": 0.05,
        "normal_method": "open3d",
        "tier": "low",
    })
    assert out["num_points"] == 500_000
    assert out["depth"] == 9
    assert out["density_quantile"] == pytest.approx(0.05)
    assert out["normal_method"] == "open3d"
    assert out["tier"] == "low"


# ─── dispatch: NotImplementedError on inactive tiers ─────────


def test_run_mesh_raises_on_standard_tier(tmp_path: Path):
    """A scene with mesh_params.tier=standard that reaches the
    dispatcher (e.g. a queued job from before the API guard landed)
    must surface NotImplementedError. The runner catches it and
    writes ``mesh_error`` cleanly instead of leaving the row stuck
    running."""
    scene_dir = tmp_path / "scene_X"
    scene_dir.mkdir()
    # Dummy src_ply that exists so the stub-fallback branch
    # doesn't short-circuit before tier dispatch.
    src_ply = scene_dir / "scene.ply"
    src_ply.write_bytes(b"ply\nformat ascii 1.0\nelement vertex 0\nend_header\n")

    async def _noop_progress(p, m):
        return None

    async def go():
        with pytest.raises(NotImplementedError):
            await mesh_step.run_mesh(
                scene_dir=scene_dir,
                src_ply=src_ply,
                params={"tier": "standard"},
                progress=_noop_progress,
                job_id="t",
            )

    _run(go())


def test_run_mesh_raises_on_higher_tier(tmp_path: Path):
    scene_dir = tmp_path / "scene_X"
    scene_dir.mkdir()
    src_ply = scene_dir / "scene.ply"
    src_ply.write_bytes(b"ply\nformat ascii 1.0\nelement vertex 0\nend_header\n")

    async def _noop_progress(p, m):
        return None

    async def go():
        with pytest.raises(NotImplementedError):
            await mesh_step.run_mesh(
                scene_dir=scene_dir,
                src_ply=src_ply,
                params={"tier": "higher"},
                progress=_noop_progress,
                job_id="t",
            )

    _run(go())


# ─── trigger endpoint: persisted-tier guard ──────────────────


@pytest.fixture
def isolated_store(tmp_path: Path):
    """Per-test sqlite db (matches test_thumbnail.py pattern)."""
    settings = Settings(
        data_dir=tmp_path,
        db_filename="test_mesh_tsdf.sqlite",
    )

    async def setup():
        await store.init_store(settings)

    async def teardown():
        await store.shutdown_store()

    asyncio.run(setup())
    yield
    asyncio.run(teardown())


def test_trigger_rejects_persisted_inactive_tier(isolated_store):
    """A scene whose persisted ``mesh_params.tier`` is "standard"
    or "higher" must NOT be allowed to queue a fresh job when the
    incoming request omits ``tier``. Without this guard the runner
    would dequeue the job and fail in the worker with
    ``NotImplementedError``, polluting the pipeline list with
    queued/failed churn — the exact case the API validator's tier
    check exists to prevent."""

    async def go():
        cap = await store.create_capture(name="t", source="upload")
        scene = await store.create_scene(cap.id)
        assert scene is not None
        # Force the scene into a state the trigger endpoint would
        # accept (completed) and inject a forward-rolled tier into
        # the persisted row.
        await store.update_scene(
            scene.id,
            status=CaptureStatus.completed,
            mesh_params={"tier": "standard"},
        )

        # Empty body — falls back to persisted mesh_params during
        # job dispatch; the new guard must reject before the
        # enqueue happens.
        with pytest.raises(HTTPException) as exc:
            await trigger_mesh(scene.id, MeshRequest(params=None))
        assert exc.value.status_code == 422
        assert "not yet implemented" in exc.value.detail

        # And no job should have queued.
        jobs = await store.list_jobs_for_scene(scene.id)
        assert all(j.kind.value != "mesh" or j.status.value != "queued"
                   for j in jobs)

    asyncio.run(go())


def test_trigger_allows_persisted_inactive_tier_overridden_to_low(isolated_store):
    """The persisted-tier guard must NOT fire when the incoming
    request explicitly sets ``tier: low`` — that's the documented
    escape hatch the 422 message points users at."""

    async def go():
        cap = await store.create_capture(name="t2", source="upload")
        scene = await store.create_scene(cap.id)
        assert scene is not None
        await store.update_scene(
            scene.id,
            status=CaptureStatus.completed,
            mesh_params={"tier": "higher"},
        )

        # Override tier in the request — should be accepted, scene
        # update happens, job queues.
        result = await trigger_mesh(
            scene.id, MeshRequest(params={"tier": "low"}),
        )
        assert result.mesh_status == MeshStatus.queued

    asyncio.run(go())


# ─── numpy-only: extrinsic flip ──────────────────────────────


def test_opencv_extrinsic_from_opengl_identity_camera():
    """Identity OpenGL c2w (camera at origin, looking down -Z, +Y
    up) should map to an OpenCV extrinsic that looks down +Z with
    +Y down. After the YZ flip the camera-from-world matrix is a
    diagonal with sign flips on Y and Z."""
    c2w_identity = np.eye(4, dtype=np.float32).flatten().tolist()
    ext = ply_render.opencv_extrinsic_from_opengl(c2w_identity)
    # c2w_cv = identity @ diag(1, -1, -1, 1) → diag(1, -1, -1, 1)
    # inv(diag(1, -1, -1, 1)) = diag(1, -1, -1, 1) (involution)
    expected = np.diag([1.0, -1.0, -1.0, 1.0]).astype(np.float32)
    assert ext.shape == (4, 4)
    assert ext.dtype == np.float32
    np.testing.assert_allclose(ext, expected, atol=1e-6)


def test_opencv_extrinsic_from_opengl_translated_camera():
    """A camera translated to (0, 0, 5) along OpenGL +Z (i.e.
    looking back at the origin from in front of it) should produce
    an extrinsic that moves world points to (0, 0, -5) in camera
    coords after the flip — i.e. translation along OpenCV +Z."""
    c2w = np.eye(4, dtype=np.float32)
    c2w[:3, 3] = (0.0, 0.0, 5.0)
    ext = ply_render.opencv_extrinsic_from_opengl(c2w.flatten().tolist())
    # World origin (0,0,0,1) through ext should land at camera-space
    # (0,0,5,1) — the YZ flip flips the sign of Z too, so the
    # original +5 OpenGL translation becomes -5 OpenCV, and the
    # inverse extrinsic puts the world point at +5 in camera Z.
    p_cam = ext @ np.array([0, 0, 0, 1.0], dtype=np.float32)
    np.testing.assert_allclose(p_cam[2], 5.0, atol=1e-5)

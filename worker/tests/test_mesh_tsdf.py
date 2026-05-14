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


def test_validate_mesh_params_accepts_standard_tier_now():
    """``standard`` was added to the active set when the OpenMVS
    backend landed. Validator must NOT 422 anymore — the broader
    standard-tier behaviour is covered in ``test_mesh_mvs.py``."""
    out = _validate_mesh_params({"tier": "standard"})
    assert out == {"tier": "standard"}


def test_validate_mesh_params_accepts_higher_tier_now():
    """``higher`` was added to the active set when the 2DGS
    backend landed. Validator must NOT 422 anymore — the broader
    higher-tier behaviour is covered in ``test_mesh_higher.py``."""
    out = _validate_mesh_params({"tier": "higher"})
    assert out == {"tier": "higher"}


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


# ─── validation: TSDF quality knobs ──────────────────────────


def test_validate_mesh_params_accepts_full_quality_knob_set():
    out = _validate_mesh_params({
        "use_edited_splat": False,
        "alpha_min": 0.3,
        "bbox_percentile_low": 5,
        "bbox_percentile_high": 95,
        "floater_opacity_min": 0.1,
        "floater_scale_max_pct": 90,
    })
    assert out["use_edited_splat"] is False
    assert out["alpha_min"] == pytest.approx(0.3)
    assert out["bbox_percentile_low"] == pytest.approx(5.0)
    assert out["bbox_percentile_high"] == pytest.approx(95.0)
    assert out["floater_opacity_min"] == pytest.approx(0.1)
    assert out["floater_scale_max_pct"] == pytest.approx(90.0)


@pytest.mark.parametrize("bad", [-0.1, 1.5, True, "0.5"])
def test_validate_mesh_params_rejects_bad_alpha_min(bad):
    with pytest.raises(HTTPException) as exc:
        _validate_mesh_params({"alpha_min": bad})
    assert exc.value.status_code == 422


def test_validate_mesh_params_rejects_inverted_bbox_percentiles():
    """low >= high makes no sense; the validator must 422 rather
    than letting the worker compute an empty / negative bbox."""
    with pytest.raises(HTTPException) as exc:
        _validate_mesh_params({
            "bbox_percentile_low": 60,
            "bbox_percentile_high": 60,
        })
    assert exc.value.status_code == 422
    with pytest.raises(HTTPException):
        _validate_mesh_params({
            "bbox_percentile_low": 75,
            "bbox_percentile_high": 25,
        })


@pytest.mark.parametrize("bad", [-1, 101, True, "0"])
def test_validate_mesh_params_rejects_bad_bbox_percentile_low(bad):
    with pytest.raises(HTTPException) as exc:
        _validate_mesh_params({"bbox_percentile_low": bad})
    assert exc.value.status_code == 422


@pytest.mark.parametrize("bad", [-0.5, 2.0, True, "0.05"])
def test_validate_mesh_params_rejects_bad_floater_opacity_min(bad):
    with pytest.raises(HTTPException) as exc:
        _validate_mesh_params({"floater_opacity_min": bad})
    assert exc.value.status_code == 422


@pytest.mark.parametrize("bad", [-1, 200, True, "95"])
def test_validate_mesh_params_rejects_bad_floater_scale_max_pct(bad):
    with pytest.raises(HTTPException) as exc:
        _validate_mesh_params({"floater_scale_max_pct": bad})
    assert exc.value.status_code == 422


def test_validate_mesh_params_rejects_non_bool_use_edited_splat():
    with pytest.raises(HTTPException) as exc:
        _validate_mesh_params({"use_edited_splat": "yes"})
    assert exc.value.status_code == 422


# ─── dispatch: NotImplementedError on inactive tiers ─────────


def test_run_mesh_standard_tier_requires_transforms_json(tmp_path: Path):
    """Standard tier is now a real dispatcher path (not a
    NotImplementedError), but it needs ``sfm/transforms.json`` to
    feed OpenMVS. When that's absent the dispatcher surfaces a
    clear RuntimeError so the runner can write ``mesh_error``."""
    scene_dir = tmp_path / "scene_X"
    scene_dir.mkdir()
    src_ply = scene_dir / "scene.ply"
    src_ply.write_bytes(b"ply\nformat ascii 1.0\nelement vertex 0\nend_header\n")

    async def _noop_progress(p, m):
        return None

    async def go():
        with pytest.raises(RuntimeError, match="transforms.json"):
            await mesh_step.run_mesh(
                scene_dir=scene_dir,
                src_ply=src_ply,
                params={"tier": "standard"},
                progress=_noop_progress,
                job_id="t",
            )

    _run(go())


def test_run_mesh_higher_tier_requires_transforms_json(tmp_path: Path):
    """Higher tier is now an active backend (2DGS retrain) — no
    longer raises NotImplementedError. Like the standard tier, it
    consumes the SfM workspace and surfaces a clean RuntimeError
    when transforms.json is absent. Broader higher-tier coverage
    is in test_mesh_higher.py."""
    scene_dir = tmp_path / "scene_X"
    scene_dir.mkdir()
    src_ply = scene_dir / "scene.ply"
    src_ply.write_bytes(b"ply\nformat ascii 1.0\nelement vertex 0\nend_header\n")

    async def _noop_progress(p, m):
        return None

    async def go():
        with pytest.raises(RuntimeError, match="transforms.json"):
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


def test_trigger_rejects_persisted_unknown_tier(isolated_store):
    """When a scene's persisted ``mesh_params.tier`` is something
    the API doesn't recognise (forward-rolled by a future client,
    hand-edited via direct DB access), the trigger endpoint must
    refuse to queue the job — otherwise the runner would dequeue
    and fail it in the worker, polluting the pipeline list with
    queued/failed churn. All three tiers (low / standard / higher)
    are active now, so the only way to land here is a typo /
    unknown value; ``"bogus"`` exercises that path."""

    async def go():
        cap = await store.create_capture(name="t", source="upload")
        scene = await store.create_scene(cap.id)
        assert scene is not None
        await store.update_scene(
            scene.id,
            status=CaptureStatus.completed,
            mesh_params={"tier": "bogus"},
        )

        # Empty body — falls back to persisted mesh_params during
        # job dispatch; the guard must reject before the enqueue
        # happens.
        with pytest.raises(HTTPException) as exc:
            await trigger_mesh(scene.id, MeshRequest(params=None))
        assert exc.value.status_code == 422

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

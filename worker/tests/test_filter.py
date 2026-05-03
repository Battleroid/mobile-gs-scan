"""Unit tests for the filter pipeline.

Builds a tiny synthetic splatfacto-shaped PLY in memory (≈100
gaussians) seeded with known-bad points so each op can verify both
the kept count AND that per-vertex attributes round-trip through the
read-modify-write cycle without mutation.

These tests are intentionally cheap so they can run on the same CI
worker that already does `python -c "import app.main"`. The CI
recipe will need plyfile + scipy + scikit-learn installed on top of
the lightweight deps; gated on `pytest` discovery.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import numpy as np
import pytest

plyfile = pytest.importorskip("plyfile")
pytest.importorskip("scipy")
pytest.importorskip("sklearn")

from app.pipeline.filter import filter_splat, validate_recipe  # noqa: E402


def _build_synthetic_ply(path: Path) -> dict:
    """Write a 53-property splatfacto PLY with ~100 gaussians.

    Returns a dict describing the planted bad points so tests can
    assert against them. Layout matches what splatfacto / nerfstudio
    emit so the filter pipeline exercises the real property names.
    """
    rng = np.random.default_rng(42)

    n_main = 80
    main_xyz = rng.normal(loc=0.0, scale=0.3, size=(n_main, 3)).astype(np.float32)

    # Bad point set 1: 10 low-opacity floaters near origin.
    floaters = rng.normal(loc=0.0, scale=0.2, size=(10, 3)).astype(np.float32)

    # Bad point set 2: 5 huge-scale "fuzzy" gaussians.
    huge = rng.normal(loc=0.0, scale=0.4, size=(5, 3)).astype(np.float32)

    # Bad point set 3: 5 far-away outliers way outside the main bbox.
    far = (rng.uniform(low=10.0, high=20.0, size=(5, 3))
           * rng.choice([-1, 1], size=(5, 3))).astype(np.float32)

    xyz = np.vstack([main_xyz, floaters, huge, far])
    n = xyz.shape[0]

    # Opacity logits: main = 2.0 (sigmoid ≈ 0.88), floaters = -3.0
    # (sigmoid ≈ 0.047), huge / far inherit a healthy 1.5.
    opacity = np.full(n, 2.0, dtype=np.float32)
    opacity[n_main : n_main + 10] = -3.0
    opacity[n_main + 10 : n_main + 15] = 1.5
    opacity[n_main + 15 :] = 1.5

    # Log-scales: main gaussians at log(0.05) ≈ -3, huge at log(1.0) = 0.
    scale = np.full((n, 3), -3.0, dtype=np.float32)
    scale[n_main + 10 : n_main + 15] = 0.0  # 5 huge ones

    rot = np.zeros((n, 4), dtype=np.float32)
    rot[:, 0] = 1.0  # identity quaternion

    # SH DC + degree-3 rest = 3 + 45 = 48 floats. Fill with deterministic
    # patterns so tests can assert exact passthrough.
    f_dc = np.tile(np.array([0.4, 0.5, 0.6], dtype=np.float32), (n, 1))
    f_rest = np.linspace(0.0, 1.0, num=n * 45, dtype=np.float32).reshape(n, 45)

    normals = np.zeros((n, 3), dtype=np.float32)

    dtype = [
        ("x", "f4"), ("y", "f4"), ("z", "f4"),
        ("nx", "f4"), ("ny", "f4"), ("nz", "f4"),
        ("f_dc_0", "f4"), ("f_dc_1", "f4"), ("f_dc_2", "f4"),
    ]
    for i in range(45):
        dtype.append((f"f_rest_{i}", "f4"))
    dtype += [
        ("opacity", "f4"),
        ("scale_0", "f4"), ("scale_1", "f4"), ("scale_2", "f4"),
        ("rot_0", "f4"), ("rot_1", "f4"), ("rot_2", "f4"), ("rot_3", "f4"),
    ]

    arr = np.empty(n, dtype=dtype)
    arr["x"], arr["y"], arr["z"] = xyz[:, 0], xyz[:, 1], xyz[:, 2]
    arr["nx"], arr["ny"], arr["nz"] = normals[:, 0], normals[:, 1], normals[:, 2]
    arr["f_dc_0"], arr["f_dc_1"], arr["f_dc_2"] = f_dc[:, 0], f_dc[:, 1], f_dc[:, 2]
    for i in range(45):
        arr[f"f_rest_{i}"] = f_rest[:, i]
    arr["opacity"] = opacity
    arr["scale_0"], arr["scale_1"], arr["scale_2"] = scale[:, 0], scale[:, 1], scale[:, 2]
    arr["rot_0"], arr["rot_1"], arr["rot_2"], arr["rot_3"] = (
        rot[:, 0], rot[:, 1], rot[:, 2], rot[:, 3]
    )

    el = plyfile.PlyElement.describe(arr, "vertex")
    plyfile.PlyData([el], byte_order="<").write(str(path))

    return {
        "n_total": n,
        "n_main": n_main,
        "n_floaters": 10,
        "n_huge": 5,
        "n_far": 5,
    }


async def _noop_progress(_pct: float, _msg: str) -> None:
    return None


@pytest.fixture
def synthetic_ply(tmp_path: Path):
    src = tmp_path / "src.ply"
    info = _build_synthetic_ply(src)
    return src, info, tmp_path


def test_validate_recipe_accepts_known_ops():
    validate_recipe({"ops": [{"type": "opacity_threshold", "min": 0.05}]})


def test_validate_recipe_rejects_unknown_op():
    with pytest.raises(ValueError):
        validate_recipe({"ops": [{"type": "magic"}]})


def test_opacity_threshold_drops_floaters(synthetic_ply):
    src, info, tmp = synthetic_ply
    out_dir = tmp / "out"
    res = asyncio.run(
        filter_splat(
            src_ply=src,
            out_dir=out_dir,
            recipe={"ops": [{"type": "opacity_threshold", "min": 0.5}]},
            progress=_noop_progress,
        )
    )
    assert res["total"] == info["n_total"]
    # All 10 floaters have sigmoid(opacity) ≈ 0.047 < 0.5.
    assert res["kept"] == info["n_total"] - info["n_floaters"]


def test_scale_clamp_drops_huge(synthetic_ply):
    src, info, tmp = synthetic_ply
    out_dir = tmp / "out"
    res = asyncio.run(
        filter_splat(
            src_ply=src,
            out_dir=out_dir,
            recipe={"ops": [{"type": "scale_clamp", "max_scale": 0.5}]},
            progress=_noop_progress,
        )
    )
    # The 5 huge gaussians have exp(0) = 1.0 > 0.5; the rest
    # exp(-3) ≈ 0.05.
    assert res["kept"] == info["n_total"] - info["n_huge"]


def test_bbox_crop_drops_far(synthetic_ply):
    src, info, tmp = synthetic_ply
    out_dir = tmp / "out"
    res = asyncio.run(
        filter_splat(
            src_ply=src,
            out_dir=out_dir,
            recipe={
                "ops": [{
                    "type": "bbox_crop",
                    "min": [-2.0, -2.0, -2.0],
                    "max": [2.0, 2.0, 2.0],
                }],
            },
            progress=_noop_progress,
        )
    )
    assert res["kept"] == info["n_total"] - info["n_far"]


def test_sphere_remove_drops_origin_cluster(synthetic_ply):
    src, info, tmp = synthetic_ply
    out_dir = tmp / "out"
    # A sphere of radius 1.5 around origin engulfs the main + floaters
    # + huge clusters; only the 5 far-away points survive.
    res = asyncio.run(
        filter_splat(
            src_ply=src,
            out_dir=out_dir,
            recipe={
                "ops": [{
                    "type": "sphere_remove",
                    "center": [0, 0, 0],
                    "radius": 1.5,
                }],
            },
            progress=_noop_progress,
        )
    )
    assert res["kept"] == info["n_far"]


def test_attributes_round_trip(synthetic_ply):
    """Run a permissive op and assert per-vertex attributes are
    preserved bit-for-bit on the survivors."""
    src, _info, tmp = synthetic_ply
    out_dir = tmp / "out"
    src_data = plyfile.PlyData.read(str(src))
    src_v = src_data["vertex"].data

    asyncio.run(
        filter_splat(
            src_ply=src,
            out_dir=out_dir,
            # Threshold below every opacity in the synthetic set so
            # every vertex survives.
            recipe={"ops": [{"type": "opacity_threshold", "min": -10.0}]},
            progress=_noop_progress,
        )
    )

    out_data = plyfile.PlyData.read(str(out_dir / "scene.ply"))
    out_v = out_data["vertex"].data
    assert out_v.shape == src_v.shape
    for name in src_v.dtype.names:
        np.testing.assert_array_equal(out_v[name], src_v[name])


def test_dbscan_keeps_largest_cluster(synthetic_ply):
    src, info, tmp = synthetic_ply
    out_dir = tmp / "out"
    # eps tight enough that the far outliers form their own clusters
    # / noise but the main cluster stays intact.
    res = asyncio.run(
        filter_splat(
            src_ply=src,
            out_dir=out_dir,
            recipe={
                "ops": [{
                    "type": "dbscan_keep_largest",
                    "eps": 0.5,
                    "min_samples": 5,
                }],
            },
            progress=_noop_progress,
        )
    )
    # The main cluster (n_main + floaters + huge — they all sit
    # near-origin) wins; the 5 far outliers are dropped as noise or
    # smaller clusters.
    assert res["kept"] >= info["n_main"]
    assert res["kept"] <= info["n_total"] - info["n_far"]


def test_sor_handles_single_point(tmp_path):
    """Regression: SOR on n≤1 used to blow up with an axis error
    because cKDTree.query returns a 1-D array for k=1. The op now
    short-circuits to keep-everything on degenerate input."""
    src = tmp_path / "single.ply"
    out_dir = tmp_path / "out"

    dtype = [
        ("x", "f4"), ("y", "f4"), ("z", "f4"),
        ("opacity", "f4"),
        ("scale_0", "f4"), ("scale_1", "f4"), ("scale_2", "f4"),
        ("rot_0", "f4"), ("rot_1", "f4"), ("rot_2", "f4"), ("rot_3", "f4"),
    ]
    arr = np.zeros(1, dtype=dtype)
    arr["rot_0"] = 1.0
    arr["opacity"] = 2.0
    el = plyfile.PlyElement.describe(arr, "vertex")
    plyfile.PlyData([el], byte_order="<").write(str(src))

    res = asyncio.run(
        filter_splat(
            src_ply=src,
            out_dir=out_dir,
            recipe={"ops": [{"type": "sor", "k": 24, "std_multiplier": 2.0}]},
            progress=_noop_progress,
        )
    )
    assert res["kept"] == 1
    assert res["total"] == 1


def test_empty_result_raises(synthetic_ply):
    src, _info, tmp = synthetic_ply
    out_dir = tmp / "out"
    with pytest.raises(RuntimeError, match="filtered out every gaussian"):
        asyncio.run(
            filter_splat(
                src_ply=src,
                out_dir=out_dir,
                # min above sigmoid(2.0) ≈ 0.88 — nothing survives.
                recipe={"ops": [{"type": "opacity_threshold", "min": 0.99}]},
                progress=_noop_progress,
            )
        )

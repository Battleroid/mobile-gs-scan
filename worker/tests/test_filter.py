"""Unit tests for app/pipeline/filter.py.

Each op is exercised against a tiny synthetic splat PLY so that
attribute round-tripping and per-op masking can be verified without
needing a real trained scene. plyfile is required (gs image only —
this test is skipped on hosts without it).
"""
from __future__ import annotations

import asyncio
import math
from pathlib import Path

import numpy as np
import pytest

plyfile = pytest.importorskip("plyfile")
PlyData = plyfile.PlyData
PlyElement = plyfile.PlyElement


VERTEX_DTYPE = np.dtype(
    [
        ("x", "<f4"),
        ("y", "<f4"),
        ("z", "<f4"),
        ("nx", "<f4"),
        ("ny", "<f4"),
        ("nz", "<f4"),
        ("f_dc_0", "<f4"),
        ("f_dc_1", "<f4"),
        ("f_dc_2", "<f4"),
        ("opacity", "<f4"),
        ("scale_0", "<f4"),
        ("scale_1", "<f4"),
        ("scale_2", "<f4"),
        ("rot_0", "<f4"),
        ("rot_1", "<f4"),
        ("rot_2", "<f4"),
        ("rot_3", "<f4"),
    ]
)


def _logit(p: float) -> float:
    return math.log(p / (1.0 - p))


def _make_synthetic_ply(path: Path) -> tuple[int, dict[str, np.ndarray]]:
    """Hand-pick a 24-gaussian splat with known properties so each op
    has an obvious right answer."""
    rows: list[tuple] = []

    def push(x, y, z, alpha, scale, sh=(0.5, 0.5, 0.5)):
        rows.append(
            (
                x, y, z,                # xyz
                0.0, 0.0, 1.0,          # normals
                sh[0], sh[1], sh[2],    # f_dc
                _logit(alpha),          # raw opacity logit
                math.log(scale), math.log(scale), math.log(scale),
                1.0, 0.0, 0.0, 0.0,     # quaternion w,x,y,z
            )
        )

    # 0..3: low opacity → opacity_threshold drops them
    for i in range(4):
        push(0.1 * i, 0.0, 0.0, alpha=0.02, scale=0.05)
    # 4..7: oversized scale → scale_clamp drops them
    for i in range(4):
        push(0.0, 0.1 * i, 0.0, alpha=0.9, scale=2.0)
    # 8..11: outside bbox [-1,1]^3 → bbox_crop drops them
    for i in range(4):
        push(5.0 + i, 0.0, 0.0, alpha=0.9, scale=0.05)
    # 12..15: inside origin sphere radius=0.2 → sphere_remove drops them
    for i in range(4):
        push(0.05 * (i - 1.5), 0.0, 0.0, alpha=0.9, scale=0.05)
    # 16..19: a tight cluster near (3, 3, 3) — DBSCAN should keep this
    # because it's the largest cluster (with item below also tight).
    for i in range(4):
        push(3.0 + 0.01 * i, 3.0, 3.0, alpha=0.9, scale=0.05)
    # 20..23: scattered floaters — DBSCAN will mark as noise and drop.
    push(-10.0, -10.0, -10.0, alpha=0.9, scale=0.05)
    push(10.0, -10.0, 10.0, alpha=0.9, scale=0.05)
    push(-10.0, 10.0, -10.0, alpha=0.9, scale=0.05)
    push(10.0, 10.0, 10.0, alpha=0.9, scale=0.05)

    arr = np.array(rows, dtype=VERTEX_DTYPE)
    el = PlyElement.describe(arr, "vertex")
    PlyData([el], text=False).write(str(path))
    fields = {name: arr[name].copy() for name in VERTEX_DTYPE.names}
    return len(arr), fields


@pytest.fixture(scope="module")
def synthetic_ply(tmp_path_factory) -> tuple[Path, int, dict[str, np.ndarray]]:
    p = tmp_path_factory.mktemp("filter") / "synthetic.ply"
    n, fields = _make_synthetic_ply(p)
    return p, n, fields


async def _noop_progress(_pct: float, _msg: str) -> None:
    return None


def _run(coro):
    return asyncio.run(coro)


def _read_kept(path: Path) -> int:
    return int(PlyData.read(str(path))["vertex"].count)


def _read_attrs(path: Path) -> np.ndarray:
    return np.asarray(PlyData.read(str(path))["vertex"].data)


def test_opacity_threshold(synthetic_ply, tmp_path):
    src, n, _ = synthetic_ply
    from app.pipeline import filter as fltr

    out = tmp_path / "opacity"
    res = _run(
        fltr.filter_splat(
            src_ply=src,
            out_dir=out,
            recipe={"ops": [{"type": "opacity_threshold", "min": 0.05}]},
            progress=_noop_progress,
        )
    )
    kept = _read_kept(Path(res["ply"]))
    # 4 rows with alpha=0.02 fall out; the rest stay.
    assert kept == n - 4


def test_scale_clamp(synthetic_ply, tmp_path):
    src, n, _ = synthetic_ply
    from app.pipeline import filter as fltr

    out = tmp_path / "scale"
    res = _run(
        fltr.filter_splat(
            src_ply=src,
            out_dir=out,
            recipe={"ops": [{"type": "scale_clamp", "max_scale": 0.5}]},
            progress=_noop_progress,
        )
    )
    kept = _read_kept(Path(res["ply"]))
    # 4 rows with scale=2.0 fall out.
    assert kept == n - 4


def test_bbox_crop(synthetic_ply, tmp_path):
    src, n, _ = synthetic_ply
    from app.pipeline import filter as fltr

    out = tmp_path / "bbox"
    res = _run(
        fltr.filter_splat(
            src_ply=src,
            out_dir=out,
            recipe={
                "ops": [{"type": "bbox_crop", "min": [-1, -1, -1], "max": [1, 1, 1]}]
            },
            progress=_noop_progress,
        )
    )
    kept = _read_kept(Path(res["ply"]))
    # Outside-bbox rows: 4 (idx 8..11), 4 (idx 16..19 at (3,3,3)),
    # 4 (idx 20..23 floaters). 12 rows stay.
    assert kept == n - 12


def test_sphere_remove(synthetic_ply, tmp_path):
    src, n, _ = synthetic_ply
    from app.pipeline import filter as fltr

    out = tmp_path / "sphere"
    res = _run(
        fltr.filter_splat(
            src_ply=src,
            out_dir=out,
            recipe={
                "ops": [
                    {"type": "sphere_remove", "center": [0, 0, 0], "radius": 0.2}
                ]
            },
            progress=_noop_progress,
        )
    )
    kept = _read_kept(Path(res["ply"]))
    # The 4 rows at idx 12..15 are inside the radius-0.2 sphere AND
    # so are the 4 low-opacity rows at idx 0..3 (xyz at 0..0.3, only
    # idx 0 and idx 1 are inside).
    # Verify by computing in-sphere count from fields.
    fields = synthetic_ply[2]
    d2 = fields["x"] ** 2 + fields["y"] ** 2 + fields["z"] ** 2
    inside = int((d2 <= 0.04).sum())
    assert kept == n - inside


def test_attributes_round_trip(synthetic_ply, tmp_path):
    """Filtered PLY must preserve every per-vertex property byte-for-byte
    on the rows that survive."""
    src, n, fields = synthetic_ply
    from app.pipeline import filter as fltr

    out = tmp_path / "rt"
    res = _run(
        fltr.filter_splat(
            src_ply=src,
            out_dir=out,
            recipe={
                "ops": [
                    {"type": "bbox_crop", "min": [-1, -1, -1], "max": [1, 1, 1]}
                ]
            },
            progress=_noop_progress,
        )
    )
    after = _read_attrs(Path(res["ply"]))
    survived = []
    for i in range(n):
        if all(-1 <= fields[a][i] <= 1 for a in ("x", "y", "z")):
            survived.append(i)
    assert len(after) == len(survived)
    for new_idx, src_idx in enumerate(survived):
        for a in VERTEX_DTYPE.names:
            assert after[a][new_idx] == pytest.approx(fields[a][src_idx])


def test_sor_drops_floaters(synthetic_ply, tmp_path):
    """SOR with k=4 should peel off the four corner-floaters whose
    nearest neighbours are far away."""
    src, n, _ = synthetic_ply
    from app.pipeline import filter as fltr

    out = tmp_path / "sor"
    res = _run(
        fltr.filter_splat(
            src_ply=src,
            out_dir=out,
            recipe={"ops": [{"type": "sor", "k": 4, "std_multiplier": 0.5}]},
            progress=_noop_progress,
        )
    )
    kept = _read_kept(Path(res["ply"]))
    # At least the 4 obvious floaters at +/-10 should be discarded.
    assert kept <= n - 4


def test_dbscan_keep_largest(synthetic_ply, tmp_path):
    """DBSCAN with eps tuned tight should keep only the dense cluster
    near origin."""
    src, _n, _ = synthetic_ply
    from app.pipeline import filter as fltr

    out = tmp_path / "dbscan"
    res = _run(
        fltr.filter_splat(
            src_ply=src,
            out_dir=out,
            recipe={
                "ops": [
                    {
                        "type": "dbscan_keep_largest",
                        "eps": 0.4,
                        "min_samples": 3,
                    }
                ]
            },
            progress=_noop_progress,
        )
    )
    after = _read_attrs(Path(res["ply"]))
    assert (np.abs(after["x"]) < 1.0).all()
    assert (np.abs(after["y"]) < 1.0).all()
    assert (np.abs(after["z"]) < 1.0).all()


def test_ops_compose(synthetic_ply, tmp_path):
    """Multiple ops compose; recipe order must be respected."""
    src, n, _ = synthetic_ply
    from app.pipeline import filter as fltr

    out = tmp_path / "combo"
    res = _run(
        fltr.filter_splat(
            src_ply=src,
            out_dir=out,
            recipe={
                "ops": [
                    {"type": "opacity_threshold", "min": 0.05},
                    {"type": "scale_clamp", "max_scale": 0.5},
                ]
            },
            progress=_noop_progress,
        )
    )
    kept = _read_kept(Path(res["ply"]))
    # 4 low-alpha + 4 oversized → 8 rows out.
    assert kept == n - 8


def test_validate_recipe_rejects_unknown(tmp_path):
    from app.pipeline import filter as fltr

    with pytest.raises(ValueError):
        fltr.validate_recipe({"ops": [{"type": "totally_made_up"}]})
    with pytest.raises(ValueError):
        fltr.validate_recipe({"ops": [{"min": 0.05}]})
    with pytest.raises(ValueError):
        fltr.validate_recipe({"ops": "not a list"})

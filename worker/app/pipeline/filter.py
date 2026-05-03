"""Post-processing filters for trained gaussian splat scenes.

The trained splat is a binary PLY produced by `ns-export gaussian-splat`
(splatfacto). Per-vertex properties are stored as raw activations:

  x, y, z              float, world-space centre
  nx, ny, nz           float, normals (unused by the renderer)
  f_dc_0..2            float, SH degree 0 (rgb)
  f_rest_0..44         float, SH degree 1..3 (15 coeffs * 3 channels)
  opacity              float, raw logit — apply sigmoid for alpha
  scale_0..2           float, log-scale per axis — apply exp for metres
  rot_0..3             float, unit quaternion (w, x, y, z)

Filtering is mask-based: each op produces a boolean kept-mask over the N
gaussians. We AND the masks, slice every per-vertex column, and write a
new PLY with the same property layout. SH coefficients, opacity, scale,
rotation pass through unchanged — only the row index set shrinks.

The module is imported only by the worker-gs runner (which has plyfile
+ open3d via Dockerfile.gs). The api container never imports it.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Awaitable, Callable

import numpy as np

from app.pipeline._spz import run_spz_pack

log = logging.getLogger(__name__)

ProgressCb = Callable[[float, str], Awaitable[None]]

# DBSCAN on > N points blows up wall time. We downsample for the cluster
# decision then expand the kept mask back to the full set via 1-NN.
DBSCAN_INPUT_CAP = 200_000

# Recognised op types. Anything else triggers a ValueError so a typo
# doesn't silently skip filtering.
OP_TYPES = {
    "opacity_threshold",
    "scale_clamp",
    "bbox_crop",
    "sphere_remove",
    "sor",
    "dbscan_keep_largest",
}


def validate_recipe(recipe: dict) -> list[dict]:
    """Return the list of ops, raising ValueError on malformed input."""
    if not isinstance(recipe, dict):
        raise ValueError("recipe must be an object")
    ops = recipe.get("ops")
    if not isinstance(ops, list):
        raise ValueError("recipe.ops must be a list")
    for i, op in enumerate(ops):
        if not isinstance(op, dict) or "type" not in op:
            raise ValueError(f"recipe.ops[{i}] missing 'type'")
        if op["type"] not in OP_TYPES:
            raise ValueError(f"recipe.ops[{i}].type unknown: {op['type']!r}")
    return ops


async def filter_splat(
    *,
    src_ply: Path,
    out_dir: Path,
    recipe: dict,
    progress: ProgressCb,
) -> dict[str, str | int]:
    """Read src_ply, apply recipe ops, write filtered .ply + .spz to out_dir.

    Returns a dict with at least 'ply' and 'kept' / 'total'; 'spz' is
    present only when spz_pack succeeded.
    """
    from plyfile import PlyData, PlyElement  # heavy; lazy-imported

    ops = validate_recipe(recipe)

    out_dir.mkdir(parents=True, exist_ok=True)
    await progress(0.05, "filter: read ply")

    ply = PlyData.read(str(src_ply))
    vertex = ply["vertex"]
    n = int(vertex.count)
    if n == 0:
        raise RuntimeError("source ply has no vertices")

    xyz = np.column_stack(
        [np.asarray(vertex["x"]), np.asarray(vertex["y"]), np.asarray(vertex["z"])]
    ).astype(np.float64, copy=False)

    mask = np.ones(n, dtype=bool)
    n_ops = max(1, len(ops))
    for i, op in enumerate(ops):
        kind = op["type"]
        sub = await _apply_op(kind, op, xyz, vertex, mask)
        mask &= sub
        kept = int(mask.sum())
        await progress(
            0.10 + 0.55 * ((i + 1) / n_ops),
            f"filter: {kind} kept {kept}/{n}",
        )
        if kept == 0:
            raise RuntimeError(
                f"filter op {kind!r} would discard all gaussians — aborting"
            )

    kept = int(mask.sum())
    await progress(0.70, f"filter: write ply ({kept}/{n})")

    new_data = vertex.data[mask]
    new_element = PlyElement.describe(new_data, "vertex")
    out_ply = out_dir / "scene.ply"
    PlyData([new_element], text=False, byte_order=ply.byte_order).write(str(out_ply))

    artifacts: dict[str, str | int] = {
        "ply": str(out_ply),
        "kept": kept,
        "total": n,
    }

    await progress(0.85, "filter: spz_pack")
    out_spz = out_dir / "scene.spz"
    packed = await run_spz_pack(out_ply, out_spz)
    if packed is not None:
        artifacts["spz"] = str(packed)

    await progress(1.0, f"filter: done ({kept}/{n})")
    return artifacts


async def _apply_op(
    kind: str, op: dict, xyz: np.ndarray, vertex, current_mask: np.ndarray
) -> np.ndarray:
    n = xyz.shape[0]
    if kind == "opacity_threshold":
        min_alpha = float(op.get("min", 0.05))
        opacity = np.asarray(vertex["opacity"], dtype=np.float64)
        alpha = 1.0 / (1.0 + np.exp(-opacity))
        return alpha > min_alpha

    if kind == "scale_clamp":
        max_scale = float(op["max_scale"])
        s0 = np.asarray(vertex["scale_0"], dtype=np.float64)
        s1 = np.asarray(vertex["scale_1"], dtype=np.float64)
        s2 = np.asarray(vertex["scale_2"], dtype=np.float64)
        scales = np.exp(np.maximum(np.maximum(s0, s1), s2))
        return scales < max_scale

    if kind == "bbox_crop":
        lo = np.asarray(op["min"], dtype=np.float64)
        hi = np.asarray(op["max"], dtype=np.float64)
        if lo.shape != (3,) or hi.shape != (3,):
            raise ValueError("bbox_crop.min/max must be length-3 arrays")
        return np.all((xyz >= lo) & (xyz <= hi), axis=1)

    if kind == "sphere_remove":
        center = np.asarray(op.get("center", [0.0, 0.0, 0.0]), dtype=np.float64)
        radius = float(op["radius"])
        d2 = np.sum((xyz - center) ** 2, axis=1)
        return d2 > radius * radius

    if kind == "sor":
        # Statistical outlier removal: drop points whose mean k-NN
        # distance exceeds the global mean + std_multiplier * std.
        # We compute distances over the currently-kept subset only —
        # earlier ops have already stripped obvious junk, and including
        # those points would distort the mean.
        from scipy.spatial import cKDTree  # type: ignore

        k = int(op.get("k", 24))
        std_mul = float(op.get("std_multiplier", 2.0))
        keep_idx = np.flatnonzero(current_mask)
        if keep_idx.size < k + 1:
            return np.ones(n, dtype=bool)
        tree = cKDTree(xyz[keep_idx])
        # k+1 because the first neighbour is the query point itself.
        dists, _ = tree.query(xyz[keep_idx], k=k + 1)
        mean_d = dists[:, 1:].mean(axis=1)
        cutoff = float(mean_d.mean() + std_mul * mean_d.std())
        local_keep = mean_d < cutoff
        sub = np.ones(n, dtype=bool)
        sub[keep_idx[~local_keep]] = False
        return sub

    if kind == "dbscan_keep_largest":
        import open3d as o3d  # type: ignore

        eps = float(op.get("eps", 0.05))
        min_samples = int(op.get("min_samples", 30))
        keep_idx = np.flatnonzero(current_mask)
        if keep_idx.size == 0:
            return np.ones(n, dtype=bool)

        if keep_idx.size > DBSCAN_INPUT_CAP:
            rng = np.random.default_rng(0)
            sampled_local = rng.choice(
                keep_idx.size, size=DBSCAN_INPUT_CAP, replace=False
            )
            sample_pts = xyz[keep_idx[sampled_local]]
        else:
            sample_pts = xyz[keep_idx]

        pcd = o3d.geometry.PointCloud()
        pcd.points = o3d.utility.Vector3dVector(sample_pts)
        labels = np.asarray(
            pcd.cluster_dbscan(eps=eps, min_points=min_samples, print_progress=False),
            dtype=np.int64,
        )
        if (labels >= 0).sum() == 0:
            return np.ones(n, dtype=bool)
        unique, counts = np.unique(labels[labels >= 0], return_counts=True)
        largest = int(unique[int(np.argmax(counts))])
        sample_keep_local = labels == largest

        if keep_idx.size <= DBSCAN_INPUT_CAP:
            sub = np.ones(n, dtype=bool)
            sub[keep_idx[~sample_keep_local]] = False
            return sub

        # Expand: each unsampled point inherits its nearest sampled
        # point's in/out decision via batched 1-NN on the sampled subset.
        from scipy.spatial import cKDTree  # type: ignore

        tree = cKDTree(sample_pts)
        _, nn_local = tree.query(xyz[keep_idx], k=1)
        sub = np.ones(n, dtype=bool)
        sub[keep_idx[~sample_keep_local[nn_local]]] = False
        return sub

    raise ValueError(f"unknown op {kind!r}")

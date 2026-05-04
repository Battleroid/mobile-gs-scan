"""Apply a user-authored cleanup recipe to a trained splat.

Reads the canonical .ply written by the export step, applies a series
of mask-based ops, and writes a filtered .ply (+ .spz when spz_pack
is available) into ``scene_dir/edit/``.

The recipe is opaque JSON validated here:

    {"ops": [
        {"type": "opacity_threshold", "min": 0.05},
        {"type": "scale_clamp", "max_scale": 0.5},
        {"type": "bbox_crop", "min": [-2,-1,-2], "max": [2,2,2]},
        {"type": "sphere_remove", "center": [0,0,0], "radius": 0.3},
        {"type": "sor", "k": 24, "std_multiplier": 2.0},
        {"type": "dbscan_keep_largest", "eps": 0.05, "min_samples": 30}
    ]}

All ops produce a per-vertex boolean keep-mask. Masks are AND-combined
across ops in declaration order. Every PLY property passes through
unchanged for kept rows — the filter never mutates per-vertex data,
only drops rows.

Heavy imports (numpy, plyfile, scipy, sklearn) live inside
``filter_splat`` so importing this module from the api process (which
doesn't ship those wheels) stays cheap and CI's `import app.main`
smoke test keeps working.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Awaitable, Callable

from app.pipeline._spz import run_spz_pack

log = logging.getLogger(__name__)

ProgressCb = Callable[[float, str], Awaitable[None]]

# Hard cap on points fed into DBSCAN. Beyond this we downsample for
# the cluster-assignment decision and NN-expand the mask back to the
# full point set, since DBSCAN is O(n²) worst case.
DBSCAN_INPUT_CAP = 200_000

ALLOWED_OPS = {
    "opacity_threshold",
    "scale_clamp",
    "bbox_crop",
    "sphere_crop",
    "sphere_remove",
    "sor",
    "dbscan_keep_largest",
    "keep_indices",
}


def validate_recipe(recipe: Any) -> dict:
    """Lightweight schema check for the filter recipe.

    Raises ValueError with a human-readable message on the first
    problem it finds. The HTTP layer surfaces this as a 422 so the
    web editor can show it inline.
    """
    if not isinstance(recipe, dict):
        raise ValueError("recipe must be a JSON object")
    ops = recipe.get("ops")
    if not isinstance(ops, list):
        raise ValueError("recipe.ops must be a list")
    for i, op in enumerate(ops):
        if not isinstance(op, dict):
            raise ValueError(f"ops[{i}] must be an object")
        kind = op.get("type")
        if kind not in ALLOWED_OPS:
            raise ValueError(
                f"ops[{i}].type {kind!r} not in {sorted(ALLOWED_OPS)}"
            )
    return {"ops": ops}


async def filter_splat(
    *,
    src_ply: Path,
    out_dir: Path,
    recipe: dict,
    progress: ProgressCb,
    job_id: str | None = None,
) -> dict[str, str | int]:
    """Read src_ply, apply recipe ops, write filtered .ply + .spz.

    Returns ``{'ply': path, 'spz': path?, 'kept': N, 'total': M}``.
    """
    import json
    import time

    import numpy as np
    from plyfile import PlyData, PlyElement

    out_dir.mkdir(parents=True, exist_ok=True)
    # Per-op log file the JobLogPanel polls. spz_pack appends its own
    # output later. Truncate fresh on every run so the panel doesn't
    # accumulate stale data from prior applies.
    log_path = out_dir / "filter.log"
    log_path.write_text("")

    def _log(msg: str) -> None:
        try:
            with log_path.open("a") as f:
                f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        except OSError:
            pass

    _log(f"recipe: {json.dumps(recipe, separators=(',', ':'))}")

    await progress(0.05, "parse ply")
    ply = PlyData.read(str(src_ply))
    vertex = ply["vertex"]
    n_total = int(vertex.count)
    if n_total == 0:
        raise RuntimeError("source PLY has no vertices to filter")
    _log(f"loaded {n_total} gaussians from {src_ply.name}")

    xyz = np.column_stack([
        np.asarray(vertex["x"], dtype=np.float32),
        np.asarray(vertex["y"], dtype=np.float32),
        np.asarray(vertex["z"], dtype=np.float32),
    ])

    keep = np.ones(n_total, dtype=bool)
    ops = recipe.get("ops", [])

    await progress(0.15, f"applying {len(ops)} op(s)")
    for i, op in enumerate(ops):
        kind = op.get("type")
        log.info("filter op %d/%d: %s", i + 1, len(ops), kind)
        before = int(keep.sum())
        op_start = time.monotonic()
        try:
            mask = _apply_op(op, xyz=xyz, vertex=vertex, op_log=_log)
        except Exception as exc:
            _log(f"op[{i}] {kind} FAILED: {exc}")
            raise RuntimeError(f"op {kind!r} failed: {exc}") from exc
        keep &= mask
        kept_so_far = int(keep.sum())
        dt_ms = int((time.monotonic() - op_start) * 1000)
        _log(
            f"op[{i}] {kind} ({dt_ms} ms): "
            f"{before} → {kept_so_far} (-{before - kept_so_far})"
        )
        # Per-op progress between 0.15 → 0.60
        frac = 0.15 + 0.45 * ((i + 1) / max(1, len(ops)))
        await progress(frac, f"{kind}: kept {kept_so_far}")

    n_kept = int(keep.sum())
    if n_kept == 0:
        _log("ABORT: every gaussian dropped by recipe")
        raise RuntimeError("recipe filtered out every gaussian; aborting")

    await progress(0.7, f"write ply ({n_kept}/{n_total})")
    out_ply = out_dir / "scene.ply"
    _write_filtered_ply(ply, vertex, keep, out_ply, PlyData=PlyData, PlyElement=PlyElement)
    _log(f"wrote {out_ply.name} ({n_kept}/{n_total} kept)")

    result: dict[str, str | int] = {
        "ply": str(out_ply),
        "kept": n_kept,
        "total": n_total,
    }

    await progress(0.9, "spz_pack")
    out_spz = out_dir / "scene.spz"
    ok = await run_spz_pack(
        out_ply, out_spz,
        log_path=out_dir / "spz_pack.log",
        job_id=job_id,
    )
    if ok:
        result["spz"] = str(out_spz)
        _log(f"wrote {out_spz.name}")
    else:
        _log("spz_pack unavailable or failed; ply only")

    _log("done")
    await progress(1.0, "done")
    return result


def _apply_op(op: dict, *, xyz, vertex, op_log: Callable[[str], None] | None = None) -> Any:
    """Dispatch a single op to its mask-builder. Returns a np.ndarray
    of dtype=bool, length n_vertices."""
    import numpy as np

    kind = op["type"]
    if kind == "opacity_threshold":
        # splatfacto stores opacity as raw logits; activation = sigmoid.
        opacity = np.asarray(vertex["opacity"], dtype=np.float32)
        sig = 1.0 / (1.0 + np.exp(-opacity))
        return sig > float(op.get("min", 0.0))

    if kind == "scale_clamp":
        # Scales are stored as log-scales, exponent gives metres.
        s = np.column_stack([
            np.asarray(vertex["scale_0"], dtype=np.float32),
            np.asarray(vertex["scale_1"], dtype=np.float32),
            np.asarray(vertex["scale_2"], dtype=np.float32),
        ])
        max_axis = np.exp(s).max(axis=1)
        return max_axis < float(op.get("max_scale", 1.0))

    if kind == "bbox_crop":
        lo = np.asarray(op.get("min", [-1e9, -1e9, -1e9]), dtype=np.float32)
        hi = np.asarray(op.get("max", [1e9, 1e9, 1e9]), dtype=np.float32)
        return ((xyz >= lo) & (xyz <= hi)).all(axis=1)

    if kind == "sphere_remove":
        center = np.asarray(op.get("center", [0.0, 0.0, 0.0]), dtype=np.float32)
        radius = float(op.get("radius", 0.0))
        d2 = ((xyz - center) ** 2).sum(axis=1)
        # Keep points OUTSIDE the sphere (the sphere defines what to nuke).
        return d2 > (radius * radius)

    if kind == "sphere_crop":
        # Counterpart to sphere_remove: keep only what's INSIDE the
        # sphere. Pairs with the in-viewer sphere widget the same way
        # bbox_crop pairs with the box widget — drag the gizmo to
        # select the region you want to retain.
        center = np.asarray(op.get("center", [0.0, 0.0, 0.0]), dtype=np.float32)
        radius = float(op.get("radius", 0.0))
        d2 = ((xyz - center) ** 2).sum(axis=1)
        return d2 <= (radius * radius)

    if kind == "sor":
        from scipy.spatial import cKDTree
        k = int(op.get("k", 24))
        sigma = float(op.get("std_multiplier", 2.0))
        n = len(xyz)
        # SOR needs at least one neighbour OTHER than each point's
        # self-match for a mean-distance reduction. With ≤ 1 point
        # there are no neighbours; with very small populations the
        # statistic is degenerate. Skip the op (keep everything) so
        # the recipe stays composable on tiny scenes instead of
        # raising an axis error mid-apply.
        if n < 2:
            return np.ones(n, dtype=bool)
        # cKDTree.query returns a 1-D ndarray when k=1 and 2-D when
        # k > 1 — clamp k to the available neighbour count and force
        # the result back to 2-D so the slice + axis-1 reduction
        # below is shape-stable.
        k_eff = max(2, min(k + 1, n))
        tree = cKDTree(xyz)
        dists, _ = tree.query(xyz, k=k_eff)
        dists = np.atleast_2d(dists)
        # Drop the self-distance column.
        neigh = dists[:, 1:]
        mean_dist = neigh.mean(axis=1)
        threshold = float(mean_dist.mean() + sigma * mean_dist.std())
        return mean_dist < threshold

    if kind == "keep_indices":
        # Explicit per-vertex keep set, indices into the SOURCE PLY's
        # vertex order. Phase-2 lasso / 3D widget selections drop into
        # the recipe via this op so server-side dispatch stays
        # uniform with the parameterised ops above. Out-of-range
        # indices are silently dropped (the user might have edited
        # the recipe by hand or the source PLY changed since the
        # selection was made) so the apply doesn't 422 on edge cases.
        raw = op.get("indices", [])
        if not isinstance(raw, list):
            raise ValueError("keep_indices.indices must be a list")
        n = xyz.shape[0]
        idx = np.asarray(raw, dtype=np.int64)
        if idx.size == 0:
            return np.zeros(n, dtype=bool)
        idx = idx[(idx >= 0) & (idx < n)]
        mask = np.zeros(n, dtype=bool)
        mask[idx] = True
        return mask

    if kind == "dbscan_keep_largest":
        from sklearn.cluster import DBSCAN
        eps = float(op.get("eps", 0.05))
        min_samples = int(op.get("min_samples", 30))
        approximate = bool(op.get("approximate", False))
        n = xyz.shape[0]
        if n <= DBSCAN_INPUT_CAP:
            if op_log is not None:
                op_log(
                    f"dbscan_keep_largest mode=deterministic sample_size={n} "
                    f"input_cap={DBSCAN_INPUT_CAP}"
                )
            labels = DBSCAN(eps=eps, min_samples=min_samples, n_jobs=-1).fit(xyz).labels_
            return _largest_cluster_mask(labels)

        # Downsample for the cluster decision then propagate via NN.
        from scipy.spatial import cKDTree
        rng = np.random.default_rng(0)
        if not approximate:
            sample_size = DBSCAN_INPUT_CAP
            mode = "deterministic"
        else:
            # Opt-in fast mode: reduce the DBSCAN working set as the
            # input grows, then project cluster-membership back to the
            # full cloud via chunked nearest-neighbour queries.
            min_sample = max(20_000, DBSCAN_INPUT_CAP // 8)
            ratio = max(DBSCAN_INPUT_CAP / float(n), 0.0)
            sample_size = int(DBSCAN_INPUT_CAP * (ratio ** 0.5))
            sample_size = max(min_sample, min(DBSCAN_INPUT_CAP, sample_size))
            sample_size = min(sample_size, n)
            mode = "approximate"
        if op_log is not None:
            op_log(
                f"dbscan_keep_largest mode={mode} sample_size={sample_size} "
                f"input_size={n} input_cap={DBSCAN_INPUT_CAP}"
            )
        idx = rng.choice(n, size=sample_size, replace=False)
        sub = xyz[idx]
        sub_labels = DBSCAN(eps=eps, min_samples=min_samples, n_jobs=-1).fit(sub).labels_
        sub_keep = _largest_cluster_mask(sub_labels)
        tree = cKDTree(sub)
        if not approximate:
            # Existing behaviour: one-shot NN projection.
            _, nn_idx = tree.query(xyz, k=1)
            return sub_keep[nn_idx]

        # Fast mode: chunked projection to keep memory and peak query
        # time bounded on huge clouds.
        keep = np.empty(n, dtype=bool)
        chunk = max(50_000, min(200_000, sample_size * 2))
        for start in range(0, n, chunk):
            end = min(n, start + chunk)
            _, nn_idx = tree.query(xyz[start:end], k=1)
            keep[start:end] = sub_keep[nn_idx]
        return keep

    raise ValueError(f"unknown op type {kind!r}")


def _largest_cluster_mask(labels) -> Any:
    """Return a boolean mask selecting the largest non-noise (-1)
    DBSCAN cluster. If every point is noise, returns the all-False
    mask — the outer guard rejects empty results so the caller sees
    a clean 'recipe filtered out every gaussian' error rather than
    an empty PLY.
    """
    import numpy as np

    labels = np.asarray(labels)
    valid = labels[labels >= 0]
    if valid.size == 0:
        return np.zeros(labels.shape, dtype=bool)
    # bincount works because labels are non-negative ints in [0, k).
    counts = np.bincount(valid)
    largest = int(counts.argmax())
    return labels == largest


def _write_filtered_ply(ply, vertex, keep, dst: Path, *, PlyData, PlyElement) -> None:
    """Write a PLY containing only the kept rows from ``vertex``.

    Preserves every property exactly as it was in the source so the
    splat can be re-loaded by Spark / SuperSplat / nerfstudio without
    a schema mismatch.
    """
    import numpy as np

    keep_idx = np.flatnonzero(keep)
    filtered = vertex.data[keep_idx]
    new_element = PlyElement.describe(filtered, "vertex")
    # Preserve binary-vs-ascii format from the source.
    PlyData([new_element], text=ply.text, byte_order=ply.byte_order).write(str(dst))

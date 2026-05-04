"""Open3D Poisson mesh extraction worker — child process entrypoint.

Spawned by ``mesh.py``'s ``_run_poisson`` so the heartbeat task in
``app.jobs.runner`` can SIGKILL the long-running native work via
``_running.kill_for_job(job_id)``. Running Open3D's Poisson
reconstruction here (instead of an ``asyncio.to_thread`` in the
worker process) means cancel actually terminates the C++ call
immediately rather than letting an orphan thread chew CPU/RAM
until it finishes naturally — which is what blocks the replacement
job in cancel/replace flows.

Usage::

    python -m app.pipeline._mesh_subprocess \\
        --src-ply <path> \\
        --staging-dir <path> \\
        --params '<json>'

Outputs (stdout, line-buffered):
    ``PROGRESS <fraction> <message>`` lines for the parent's
    progress callback.  Other lines are arbitrary log text the
    parent appends to ``mesh.log`` verbatim.

Files written into ``--staging-dir``:
    ``scene.obj``  — always on success.
    ``scene.glb``  — best-effort via trimesh; missing on conversion
                     failure (parent treats absence as "obj only").
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np
import open3d as o3d


def _emit(progress: float, msg: str) -> None:
    # Bound to [0, 1] just in case a stage label miscounts; the
    # parent's progress callback would clamp anyway, but emitting
    # in-range values keeps the WS payload tidy.
    p = max(0.0, min(1.0, float(progress)))
    print(f"PROGRESS {p:.4f} {msg}", flush=True)


def _log(msg: str) -> None:
    print(msg, flush=True)


def _load_pointcloud(src_ply: Path):
    """Load splat .ply into an Open3D PointCloud.

    Open3D's read_point_cloud handles standard PLYs but returns an
    empty cloud (rather than raising) on splatfacto layouts where
    the property names confuse its picker. Fall back to plyfile in
    that case and rebuild xyz (+ normals if non-zero) by hand.
    """
    pc = o3d.io.read_point_cloud(str(src_ply))
    if len(pc.points) > 0:
        return pc

    _log("open3d.read_point_cloud returned empty; falling back to plyfile")
    from plyfile import PlyData

    ply = PlyData.read(str(src_ply))
    v = ply["vertex"]
    xyz = np.column_stack([
        np.asarray(v["x"], dtype=np.float64),
        np.asarray(v["y"], dtype=np.float64),
        np.asarray(v["z"], dtype=np.float64),
    ])
    if xyz.shape[0] == 0:
        raise RuntimeError("source PLY has no vertices")
    pc = o3d.geometry.PointCloud()
    pc.points = o3d.utility.Vector3dVector(xyz)
    if {"nx", "ny", "nz"}.issubset(set(v.dtype.names or ())):
        normals = np.column_stack([
            np.asarray(v["nx"], dtype=np.float64),
            np.asarray(v["ny"], dtype=np.float64),
            np.asarray(v["nz"], dtype=np.float64),
        ])
        if not np.allclose(normals, 0):
            pc.normals = o3d.utility.Vector3dVector(normals)
    return pc


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src-ply", required=True)
    ap.add_argument("--staging-dir", required=True)
    ap.add_argument("--params", required=True, help="JSON-encoded params dict")
    args = ap.parse_args()

    src_ply = Path(args.src_ply)
    staging_dir = Path(args.staging_dir)
    params = json.loads(args.params)

    target = int(params.get("num_points", 1_000_000))
    depth = int(params.get("depth", 9))
    density_quantile = float(params.get("density_quantile", 0.01))
    remove_outliers = bool(params.get("remove_outliers", True))
    use_bounding_box = bool(params.get("use_bounding_box", False))

    _log(f"open3d poisson on {src_ply.name} params={params}")

    _emit(0.05, "load splat ply")
    pc = _load_pointcloud(src_ply)
    n0 = len(pc.points)
    _log(f"loaded {n0} points")

    if remove_outliers:
        _emit(0.20, "remove outliers")
        pc, ind = pc.remove_statistical_outlier(nb_neighbors=20, std_ratio=2.0)
        _log(f"removed {n0 - len(ind)} outliers ({n0} → {len(ind)})")

    if use_bounding_box:
        _emit(0.28, "crop bbox")
        xyz = np.asarray(pc.points)
        lo = np.percentile(xyz, 1, axis=0)
        hi = np.percentile(xyz, 99, axis=0)
        bbox = o3d.geometry.AxisAlignedBoundingBox(lo, hi)
        pc = pc.crop(bbox)
        _log(f"cropped to robust bbox: {len(pc.points)} points")

    if len(pc.points) > target:
        _emit(0.35, f"subsample {target} points")
        ratio = target / len(pc.points)
        pc = pc.random_down_sample(ratio)
        _log(f"subsampled to {len(pc.points)} points")

    # The API allowlist forces normal_method == "open3d" and the
    # parent normalizes any legacy persisted value before invoking
    # us; PCA normal estimation is the only path here.
    _emit(0.5, "estimate normals")
    pc.estimate_normals(
        search_param=o3d.geometry.KDTreeSearchParamHybrid(
            radius=0.1, max_nn=30,
        ),
    )
    try:
        pc.orient_normals_consistent_tangent_plane(k=20)
    except Exception as exc:  # noqa: BLE001
        _log(f"normal orientation skipped: {exc}")

    _emit(0.65, "poisson reconstruction")
    mesh, densities = (
        o3d.geometry.TriangleMesh.create_from_point_cloud_poisson(
            pc, depth=depth,
        )
    )
    _log(
        f"poisson @ depth={depth}: "
        f"{len(mesh.vertices)} verts, {len(mesh.triangles)} tris"
    )

    if density_quantile > 0:
        _emit(0.85, "prune low-density verts")
        densities_np = np.asarray(densities)
        threshold = np.quantile(densities_np, density_quantile)
        keep_mask = densities_np >= threshold
        mesh.remove_vertices_by_mask(np.logical_not(keep_mask))
        _log(
            f"density prune @ q={density_quantile}: "
            f"kept {int(keep_mask.sum())}/{int(len(keep_mask))} verts"
        )

    _emit(0.92, "write obj")
    staged_obj = staging_dir / "scene.obj"
    o3d.io.write_triangle_mesh(str(staged_obj), mesh, write_ascii=True)
    _log(f"wrote {staged_obj.name}")

    _emit(0.96, "write glb")
    staged_glb = staging_dir / "scene.glb"
    try:
        import trimesh

        mesh_t = trimesh.load(staged_obj, force="mesh")
        mesh_t.export(staged_glb)
        if staged_glb.exists():
            _log(f"wrote {staged_glb.name}")
        else:
            _log("glb conversion produced no file; obj only")
    except Exception as exc:  # noqa: BLE001
        _log(f"glb conversion failed: {exc}; obj only")

    _emit(1.0, "done")
    return 0


if __name__ == "__main__":
    sys.exit(main())

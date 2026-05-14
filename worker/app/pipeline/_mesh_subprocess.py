"""TSDF-fusion mesh extraction worker — child process entrypoint.

Replaces the prior Open3D-Poisson-from-splat-PLY path (preserved in
git history) with a TSDF fusion of RGB+ED views rendered directly
from the splatfacto checkpoint. The earlier Poisson approach
produced blobby / convex-hull output with no vertex colors because:
  * Gaussian centers are not surface points — Poisson over them
    forces a closed surface across gaps.
  * The PLY's SH-DC colors were dropped by Open3D's poisson call.

TSDF integrates per-pixel depth from many camera-space renderings
that all converge on the actual surface, so the output is
surface-faithful and *partial-by-design* (open boundaries where
camera coverage stops). That matches the downstream Blender / 3D
media import use case — a scan of a tree stump produces the visible
stump + the ground the camera saw, with the unseen underside left
open rather than invented.

Spawned by ``mesh.py``'s low-tier dispatcher so the heartbeat task
in ``app.jobs.runner`` can SIGKILL the long-running native work via
``_running.kill_for_job(job_id)``.

Usage::

    python -m app.pipeline._mesh_subprocess \\
        --src-ply <path> \\
        --staging-dir <path> \\
        --params '<json>' \\
        [--tier low]

Outputs (stdout, line-buffered):
    ``PROGRESS <fraction> <message>`` lines for the parent's
    progress callback. Other lines are arbitrary log text the
    parent appends to ``mesh.log`` verbatim.

Files written into ``--staging-dir``:
    ``scene.obj``  — always on success; OBJ with per-vertex RGB.
    ``scene.glb``  — best-effort via trimesh; missing on conversion
                     failure (parent treats absence as "obj only").

Post-processing is **floater removal only**. We deliberately do
not close holes, simplify, decimate, or compute a convex hull. The
goal is a usable partial surface, not a watertight one.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

import numpy as np
import open3d as o3d

from app.pipeline import ply_render
from app.pipeline.thumbnail import _look_at, _ply_bbox


def _emit(progress: float, msg: str) -> None:
    # Bound to [0, 1] just in case a stage label miscounts; the
    # parent's progress callback would clamp anyway, but emitting
    # in-range values keeps the WS payload tidy.
    p = max(0.0, min(1.0, float(progress)))
    print(f"PROGRESS {p:.4f} {msg}", flush=True)


def _log(msg: str) -> None:
    print(msg, flush=True)


# Camera framing matches the orbit step (``orbit.py:DEFAULT_FOV_DEG``)
# so the TSDF integration sees roughly the same FOV the user trains
# on. Hard-coding rather than param-exposing — depth integration is
# not particularly FOV-sensitive within ±10°.
_FOV_DEG = 50.0
_RENDER_W = 720
_RENDER_H = 720


def _dome_camera_path(
    centroid: tuple[float, float, float],
    extent: float,
    *,
    n_views: int,
    view_elevations: list[float],
) -> list[list[float]]:
    """Generate camera_to_world poses around the splat centroid.

    ``view_elevations`` are normalized values in [-1, 1] that map to
    angles in [-π/2, π/2]; 0 is the equator, 1 is straight down from
    above. Each elevation gets an evenly-spaced azimuth ring with
    ``n_views // len(view_elevations)`` cameras. The orbit radius
    auto-fits from the splat's robust extent the same way the orbit
    step does (``orbit.py:_orbit_camera_path``) so depth values stay
    within ``ScalableTSDFVolume.sdf_trunc``.

    Up-axis is +Y, matching every other camera path the worker emits
    (thumbnail / orbit). ``_look_at`` returns row-major 16-float
    OpenGL c2w (the convention ``ply_render`` consumes).
    """
    cx, cy, cz = centroid
    fov_rad = math.radians(_FOV_DEG)
    # Fit so the bbox fills ~70% of the render — matches orbit.py.
    fit_distance = (extent / max(0.01, math.tan(fov_rad * 0.5))) * 1.4
    distance = max(fit_distance, extent * 2.0, 1.5)

    n_per_ring = max(1, n_views // max(1, len(view_elevations)))
    poses: list[list[float]] = []
    for elev_frac in view_elevations:
        phi = max(-1.0, min(1.0, float(elev_frac))) * (math.pi / 2.0)
        horizontal = distance * math.cos(phi)
        vertical = distance * math.sin(phi)
        for i in range(n_per_ring):
            theta = 2.0 * math.pi * i / n_per_ring
            eye = (
                cx + horizontal * math.sin(theta),
                cy + vertical,
                cz + horizontal * math.cos(theta),
            )
            target = (cx, cy, cz)
            poses.append(_look_at(eye, target, up=(0.0, 1.0, 0.0)))
    return poses


def _intrinsic_for(width: int, height: int) -> o3d.camera.PinholeCameraIntrinsic:
    fov_rad = math.radians(_FOV_DEG)
    fy = height / (2.0 * math.tan(fov_rad * 0.5))
    fx = fy
    cx = width / 2.0
    cy = height / 2.0
    return o3d.camera.PinholeCameraIntrinsic(width, height, fx, fy, cx, cy)


def _run_tsdf(*, src_ply: Path, staging_dir: Path, params: dict) -> int:
    """Low-tier TSDF fusion. Renders dome views from the splat,
    integrates RGB+depth into ``ScalableTSDFVolume``, extracts a
    vertex-colored mesh, writes OBJ + GLB into ``staging_dir``.
    Returns 0 on success, non-zero on failure (caller surfaces the
    parent log tail in the job error)."""
    n_views = int(params.get("n_views", 96))
    view_elevations = list(params.get("view_elevations", [0.2, 0.6]))
    voxel_size_frac = float(params.get("voxel_size", 0.005))
    sdf_trunc_mult = float(params.get("sdf_trunc_mult", 4.0))
    depth_trunc_mult = float(params.get("depth_trunc", 8.0))
    remove_outliers = bool(params.get("remove_outliers", True))
    use_bounding_box = bool(params.get("use_bounding_box", False))

    _log(f"tsdf fusion on {src_ply.name} params={params}")

    _emit(0.02, "prepare splat")
    prepared = ply_render.prepare_scene(src_ply)
    centroid, extent = _ply_bbox(src_ply)
    _log(f"centroid={centroid} extent={extent:.4f}")

    # Scale voxel size by the scene extent so the same default works
    # across captures of wildly different physical scales (a tree
    # stump and a building both produce reasonable voxel grids).
    #
    # We honor the user's ``voxel_size`` directly. The API validator
    # already restricts the value to ``(0, 0.1]``; the default 0.005
    # (i.e. 1/200 of extent) gives ~200 voxels per side which is a
    # sensible mid-density mesh. The floor below is a permissive
    # safety net — only fires for absurdly small fractions
    # (< 1/2048 of extent, ~5000 voxels per side dense, far past
    # what marching cubes can hold even with ScalableTSDFVolume's
    # sparse hashing). When it does fire we log loudly so the
    # operator notices the override rather than wondering why their
    # detail knob was ignored.
    safe_voxel_frac = voxel_size_frac
    if safe_voxel_frac < 1.0 / 2048.0:
        _log(
            f"voxel_size={voxel_size_frac} too small; clamping to "
            f"1/2048 of extent to avoid marching-cubes OOM"
        )
        safe_voxel_frac = 1.0 / 2048.0
    voxel = safe_voxel_frac * extent
    sdf_trunc = sdf_trunc_mult * voxel
    depth_trunc = depth_trunc_mult * extent
    _log(
        f"voxel_size={voxel:.5f} (frac={safe_voxel_frac}) "
        f"sdf_trunc={sdf_trunc:.5f} depth_trunc={depth_trunc:.4f}"
    )

    tsdf = o3d.pipelines.integration.ScalableTSDFVolume(
        voxel_length=voxel,
        sdf_trunc=sdf_trunc,
        color_type=o3d.pipelines.integration.TSDFVolumeColorType.RGB8,
    )

    poses = _dome_camera_path(
        centroid, extent,
        n_views=n_views, view_elevations=view_elevations,
    )
    intrinsic = _intrinsic_for(_RENDER_W, _RENDER_H)
    n = len(poses)
    _log(f"rendering + fusing {n} views @ {_RENDER_W}x{_RENDER_H}")

    # Progress allocation: fuse loop occupies [0.05, 0.70]. Emit a
    # PROGRESS line every ~5% (i.e. every n//20 frames, min 1) to
    # keep the parent's 1%-throttled callback well-fed without
    # flooding stdout with 96 lines.
    tick_every = max(1, n // 20)
    for i, c2w in enumerate(poses):
        rgb, depth = ply_render.render_one_rgbd(
            prepared, c2w,
            fov_deg=_FOV_DEG, width=_RENDER_W, height=_RENDER_H,
        )
        # Open3D wants a contiguous C-order array for the depth
        # image. ``render_one_rgbd`` returns float32 already.
        rgb_img = o3d.geometry.Image(np.ascontiguousarray(rgb))
        depth_img = o3d.geometry.Image(np.ascontiguousarray(depth))
        rgbd = o3d.geometry.RGBDImage.create_from_color_and_depth(
            rgb_img, depth_img,
            depth_scale=1.0,
            depth_trunc=depth_trunc,
            convert_rgb_to_intensity=False,
        )
        extrinsic = ply_render.opencv_extrinsic_from_opengl(c2w)
        tsdf.integrate(rgbd, intrinsic, extrinsic.astype(np.float64))
        if (i + 1) % tick_every == 0 or i == n - 1:
            frac = 0.05 + 0.65 * ((i + 1) / n)
            _emit(frac, f"fuse view {i + 1}/{n}")

    _emit(0.75, "extract mesh")
    mesh = tsdf.extract_triangle_mesh()
    mesh.compute_vertex_normals()
    n_verts = len(mesh.vertices)
    n_tris = len(mesh.triangles)
    _log(f"extracted {n_verts} verts, {n_tris} tris")
    if n_tris == 0:
        _log("ERROR: TSDF produced an empty mesh — depth integration "
             "found no surface. Check splat quality / view coverage.")
        return 2

    if remove_outliers:
        # Floater removal only: drop connected components whose
        # triangle count is < 1% of the largest cluster's. This
        # cleans up speckle from noisy silhouette-edge depth without
        # touching the open boundaries we want to keep — small
        # legitimate islands of geometry (a separated fence post,
        # etc.) survive as long as they're at least 1% of the main
        # body. DO NOT add hole-fill or watertight passes here; the
        # partial-surface output is the design goal.
        _emit(0.85, "cluster prune")
        tri_clusters, counts, _areas = mesh.cluster_connected_triangles()
        tri_clusters = np.asarray(tri_clusters)
        counts = np.asarray(counts)
        if counts.size > 0:
            keep_min = max(1, int(counts.max() * 0.01))
            per_tri_size = counts[tri_clusters]
            drop_mask = per_tri_size < keep_min
            n_drop = int(drop_mask.sum())
            if n_drop > 0:
                mesh.remove_triangles_by_mask(drop_mask)
                mesh.remove_unreferenced_vertices()
                _log(
                    f"cluster prune: dropped {n_drop}/{n_tris} tris "
                    f"(keep_min={keep_min})"
                )

    if use_bounding_box:
        _emit(0.88, "crop bbox")
        verts = np.asarray(mesh.vertices)
        if verts.size > 0:
            lo = np.percentile(verts, 1, axis=0)
            hi = np.percentile(verts, 99, axis=0)
            bbox = o3d.geometry.AxisAlignedBoundingBox(lo, hi)
            mesh = mesh.crop(bbox)
            _log(
                f"bbox crop: kept {len(mesh.vertices)} verts, "
                f"{len(mesh.triangles)} tris"
            )

    _emit(0.92, "write obj")
    staged_obj = staging_dir / "scene.obj"
    # ``write_vertex_colors=True`` emits per-vertex RGB lines after
    # each ``v`` entry. Open3D's OBJ writer respects the colors set
    # by TSDF integration (the ``RGB8`` color type populates
    # ``mesh.vertex_colors`` automatically).
    o3d.io.write_triangle_mesh(
        str(staged_obj), mesh,
        write_ascii=False,
        write_vertex_normals=True,
        write_vertex_colors=True,
        write_triangle_uvs=False,
        print_progress=False,
    )
    _log(f"wrote {staged_obj.name}")

    _emit(0.96, "write glb")
    staged_glb = staging_dir / "scene.glb"
    try:
        # Build the trimesh directly from the Open3D arrays rather
        # than round-tripping through OBJ — preserves full color
        # precision and is faster.
        import trimesh

        vertices = np.asarray(mesh.vertices)
        triangles = np.asarray(mesh.triangles)
        vcols = np.asarray(mesh.vertex_colors)
        if vcols.shape[0] == vertices.shape[0] and vcols.size > 0:
            vcols_u8 = (vcols.clip(0.0, 1.0) * 255.0).astype(np.uint8)
            mesh_t = trimesh.Trimesh(
                vertices=vertices,
                faces=triangles,
                vertex_colors=vcols_u8,
                process=False,
            )
        else:
            mesh_t = trimesh.Trimesh(
                vertices=vertices, faces=triangles, process=False,
            )
        mesh_t.export(staged_glb)
        if staged_glb.exists():
            _log(f"wrote {staged_glb.name}")
        else:
            _log("glb conversion produced no file; obj only")
    except Exception as exc:  # noqa: BLE001
        _log(f"glb conversion failed: {exc}; obj only")

    _emit(1.0, "done")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src-ply", required=True)
    ap.add_argument("--staging-dir", required=True)
    ap.add_argument("--params", required=True, help="JSON-encoded params dict")
    ap.add_argument(
        "--tier",
        default="low",
        choices=("low",),
        help=(
            "Mesh tier. Only 'low' is implemented in this subprocess. "
            "Standard / higher tiers are rejected in the parent "
            "(mesh.py) before spawning so they don't reach here."
        ),
    )
    args = ap.parse_args()

    src_ply = Path(args.src_ply)
    staging_dir = Path(args.staging_dir)
    params = json.loads(args.params)

    if args.tier != "low":
        _log(f"ERROR: tier '{args.tier}' not implemented in subprocess")
        return 3

    return _run_tsdf(
        src_ply=src_ply, staging_dir=staging_dir, params=params,
    )


if __name__ == "__main__":
    sys.exit(main())

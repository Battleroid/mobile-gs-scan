"""Poisson mesh extraction from a trained Gaussian-splatting scene.

We DON'T use nerfstudio's ``ns-export poisson`` here. As of
nerfstudio 1.1.5 that exporter asserts on a
``pipeline.datamanager.train_pixel_sampler`` that exists on the
ray-based managers (vanilla nerf etc) but NOT on
``FullImageDatamanager`` — which is what splatfacto uses. Result:
``ns-export poisson`` against any splatfacto-trained scene crashes
with ``AttributeError: 'FullImageDatamanager' object has no
attribute 'train_pixel_sampler'``. There's no flag to opt out.

Instead, run Open3D's Poisson reconstruction in-process against
the gaussian-splat ``.ply`` the export step already produced. The
splat PLY is a point cloud of gaussian centres + per-vertex
attributes — exactly the input the surface reconstruction needs.

Pipeline:
  1. Load the splat PLY into an Open3D PointCloud (xyz + optional
     normals + optional colours from the SH DC band).
  2. Subsample / outlier-prune.
  3. Estimate normals if the PLY didn't carry them, or use the
     existing normals when ``normal_method == 'model_output'``.
  4. ``create_from_point_cloud_poisson(depth=…)`` for the surface.
  5. Density-prune low-confidence triangles (default: drop the
     bottom 1%) so the mesh isn't smeared out into the empty
     space around the subject.
  6. Export ``scene.obj`` + ``scene.glb`` via Open3D / trimesh,
     atomically swapping in from a per-job staging dir so a
     concurrent re-extract doesn't clobber the live artefacts.

Output:
  scene_dir/mesh/scene.obj    — canonical Wavefront mesh
  scene_dir/mesh/scene.glb    — gltf binary (when trimesh's writer
                                succeeds); rendered directly by
                                three.js's GLTFLoader on the web.
  scene_dir/mesh/mesh.log     — per-step trace surfaced via
                                JobLogPanel.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
import time
from pathlib import Path
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

ProgressCb = Callable[[float, str], Awaitable[None]]

# Defaults tuned for the studio scenes we get out of phone captures
# (~1M splats, 5–10 m extent). The user can override any of these
# via POST /api/scenes/{id}/mesh's ``params`` body.
DEFAULT_PARAMS: dict = {
    # Target sample count after subsampling. Open3D's Poisson scales
    # roughly linearly with input size up to ~1M points; beyond
    # that the marginal density gain is dwarfed by runtime.
    "num_points": 1_000_000,
    # Statistical-outlier removal pass before normal estimation.
    # Splatfacto's gaussians sometimes drift outside the subject;
    # outlier removal stops them from polluting the surface.
    "remove_outliers": True,
    # Normal estimation. ``open3d`` runs PCA on each point's
    # k-nearest neighbours; ``model_output`` reuses normals already
    # written into the PLY by splatfacto (if present). Fallback to
    # PCA when model_output is asked but no nx/ny/nz attribute
    # exists.
    "normal_method": "open3d",
    # Whether to crop input to a tight bounding box derived from
    # the point cloud's robust 1st/99th percentile range. Helps
    # when stray gaussians sit far from the subject; turn off to
    # keep the full extent.
    "use_bounding_box": False,
    # Octree depth for the Poisson solver. Higher = finer detail
    # but quadratic memory. 9 is a good balance for 1M points.
    "depth": 9,
    # Quantile threshold for density-based vertex pruning after
    # reconstruction. Drops the lowest-density triangles (typically
    # spurious surfaces in empty space). 0 disables pruning.
    "density_quantile": 0.01,
}


async def run_mesh(
    *,
    scene_dir: Path,
    src_ply: Path | None = None,
    params: dict | None = None,
    progress: ProgressCb,
    job_id: str | None = None,
) -> dict:
    mesh_dir = scene_dir / "mesh"
    mesh_dir.mkdir(parents=True, exist_ok=True)

    merged = {**DEFAULT_PARAMS, **(params or {})}

    # Stub-friendly: if the runner couldn't pass a real .ply (e.g.
    # synthetic / stub-trained scenes from the test suite) we drop
    # a placeholder cube so the web side has something to render.
    train_dir = scene_dir / "train"
    if (
        src_ply is None
        or not src_ply.exists()
        or (train_dir / "synthetic.json").exists()
    ):
        return await _run_stub(
            mesh_dir=mesh_dir,
            params=merged,
            progress=progress,
            reason=(
                "synthetic train output"
                if (train_dir / "synthetic.json").exists()
                else f"source .ply missing at {src_ply}"
            ),
        )

    return await _run_poisson(
        src_ply=src_ply,
        mesh_dir=mesh_dir,
        params=merged,
        progress=progress,
        job_id=job_id,
    )


async def _run_poisson(
    *,
    src_ply: Path,
    mesh_dir: Path,
    params: dict,
    progress: ProgressCb,
    job_id: str | None,
) -> dict:
    # Per-job staging dir + atomic swap on success. Same pattern as
    # the filter step — old mesh stays addressable until the new
    # one is fully written, and a crash mid-run can't half-overwrite
    # the prior artefacts.
    staging_dir = mesh_dir / f".staging-{job_id or 'anon'}"
    if staging_dir.exists():
        shutil.rmtree(staging_dir, ignore_errors=True)
    staging_dir.mkdir(parents=True)

    log_path = mesh_dir / "mesh.log"
    log_path.write_text("")

    def _log(msg: str) -> None:
        try:
            with log_path.open("a") as f:
                f.write(f"[{time.strftime('%H:%M:%S')}] {msg}\n")
        except OSError:
            pass

    _log(
        f"open3d poisson on {src_ply.name} "
        f"params={params}"
    )

    # Each stage runs in a worker thread so the heartbeat task on
    # the event loop can observe a cancellation between stages and
    # SIGKILL / cancel us. Open3D's Poisson is one big native call
    # that we can't preempt mid-flight, but the awaits between
    # stages give cancel-and-replace a chance to land at every
    # boundary. Without to_thread, the entire reconstruction would
    # block the loop and ignore cancel requests until the very end.
    target = int(params.get("num_points", DEFAULT_PARAMS["num_points"]))
    depth = int(params.get("depth", DEFAULT_PARAMS["depth"]))
    density_quantile = float(
        params.get("density_quantile", DEFAULT_PARAMS["density_quantile"])
    )

    try:
        await progress(0.05, "load splat ply")
        pc = await asyncio.to_thread(_load_pointcloud, src_ply, params, _log)
        n0 = len(pc.points)
        _log(f"loaded {n0} points")

        if params.get("remove_outliers", True):
            await progress(0.20, "remove outliers")
            pc, n_kept = await asyncio.to_thread(_remove_outliers, pc)
            _log(f"removed {n0 - n_kept} outliers ({n0} → {n_kept})")

        if params.get("use_bounding_box", False):
            await progress(0.28, "crop bbox")
            pc = await asyncio.to_thread(_crop_robust_bbox, pc)
            _log(f"cropped to robust bbox: {len(pc.points)} points")

        if len(pc.points) > target:
            await progress(0.35, f"subsample {target} points")
            pc = await asyncio.to_thread(_subsample, pc, target)
            _log(f"subsampled to {len(pc.points)} points")

        normal_method = params.get("normal_method")
        if not pc.has_normals() or normal_method == "open3d":
            await progress(0.5, "estimate normals")
            await asyncio.to_thread(_estimate_normals, pc, _log)
        else:
            _log("using normals already on the PLY")

        await progress(0.65, "poisson reconstruction")
        mesh, densities = await asyncio.to_thread(_poisson_reconstruct, pc, depth)
        _log(
            f"poisson @ depth={depth}: "
            f"{len(mesh.vertices)} verts, {len(mesh.triangles)} tris"
        )

        if density_quantile > 0:
            await progress(0.85, "prune low-density verts")
            kept = await asyncio.to_thread(
                _density_prune, mesh, densities, density_quantile,
            )
            _log(
                f"density prune @ q={density_quantile}: "
                f"kept {kept[0]}/{kept[1]} verts"
            )

        await progress(0.92, "write obj")
        staged_obj = staging_dir / "scene.obj"
        await asyncio.to_thread(_write_obj, mesh, staged_obj)
        _log(f"wrote {staged_obj.name}")

        await progress(0.96, "write glb")
        staged_glb = staging_dir / "scene.glb"
        has_glb = await asyncio.to_thread(_obj_to_glb, staged_obj, staged_glb)
        if has_glb:
            _log(f"wrote {staged_glb.name}")
        else:
            _log("glb conversion failed; obj only")

    except Exception:
        # Drop staging on failure so the prior mesh in mesh_dir is
        # untouched and the next attempt starts clean.
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise

    # Atomic swap: replace canonical mesh files in mesh_dir from
    # staging. Path.replace is atomic on POSIX within the same
    # filesystem; we're staging inside mesh_dir/.staging-<job_id>
    # so we're always on the same device.
    obj_dst = mesh_dir / "scene.obj"
    glb_dst = mesh_dir / "scene.glb"
    (staging_dir / "scene.obj").replace(obj_dst)
    if has_glb:
        (staging_dir / "scene.glb").replace(glb_dst)
    elif glb_dst.exists():
        try:
            glb_dst.unlink()
        except OSError:
            pass

    shutil.rmtree(staging_dir, ignore_errors=True)
    _log("done")

    result: dict[str, str | int] = {"obj": str(obj_dst)}
    if has_glb:
        result["glb"] = str(glb_dst)
    await progress(1.0, "mesh: done")
    return result


# ─── sync stage helpers (each invoked via asyncio.to_thread) ────
#
# These do the actual CPU work. Keeping them as plain sync
# functions makes it easy to dispatch them through the executor
# without coroutine plumbing, AND they're callable from the test
# suite directly without an event loop.

def _remove_outliers(pc):
    """Statistical outlier removal pass. Returns (cloud, n_kept)."""
    pc, ind = pc.remove_statistical_outlier(nb_neighbors=20, std_ratio=2.0)
    return pc, len(ind)


def _crop_robust_bbox(pc):
    """Crop the cloud to its 1st/99th-percentile axis-aligned bbox.

    Robust to a handful of outlier gaussians far from the subject;
    a min/max bbox would be dominated by them.
    """
    import numpy as np
    import open3d as o3d

    xyz = np.asarray(pc.points)
    lo = np.percentile(xyz, 1, axis=0)
    hi = np.percentile(xyz, 99, axis=0)
    bbox = o3d.geometry.AxisAlignedBoundingBox(lo, hi)
    return pc.crop(bbox)


def _subsample(pc, target_count: int):
    ratio = target_count / len(pc.points)
    return pc.random_down_sample(ratio)


def _estimate_normals(pc, _log) -> None:
    """PCA-based normal estimation + tangent-plane consistency pass.

    The orientation pass occasionally fails on disjoint clouds; we
    log + skip rather than crash since Poisson can still
    reconstruct from un-oriented normals (they get sign-flipped on
    the fly).
    """
    import open3d as o3d

    pc.estimate_normals(
        search_param=o3d.geometry.KDTreeSearchParamHybrid(
            radius=0.1, max_nn=30,
        ),
    )
    try:
        pc.orient_normals_consistent_tangent_plane(k=20)
    except Exception as exc:  # noqa: BLE001
        _log(f"normal orientation skipped: {exc}")


def _poisson_reconstruct(pc, depth: int):
    """Run Open3D's screened-Poisson reconstruction.

    This is the single longest-running call in the pipeline (10s
    to a few minutes depending on point count + depth). It's a
    native C++ entrypoint so it doesn't release the GIL meaningfully
    — running it inside asyncio.to_thread is what keeps the worker
    event loop responsive to heartbeats / cancellations. Cancel
    landing mid-call still has to wait for this to finish though;
    finer granularity would need to fork a subprocess.
    """
    import open3d as o3d

    return o3d.geometry.TriangleMesh.create_from_point_cloud_poisson(
        pc, depth=depth,
    )


def _density_prune(mesh, densities, quantile: float) -> tuple[int, int]:
    """Drop triangles whose vertex density is below the quantile
    threshold. Returns (kept, total) for the log line."""
    import numpy as np

    densities_np = np.asarray(densities)
    threshold = np.quantile(densities_np, quantile)
    keep_mask = densities_np >= threshold
    mesh.remove_vertices_by_mask(np.logical_not(keep_mask))
    return int(keep_mask.sum()), int(len(keep_mask))


def _write_obj(mesh, dst: Path) -> None:
    """ASCII OBJ — readable, standards-compliant. Poisson output
    has no UVs so the writer skips that stage."""
    import open3d as o3d

    o3d.io.write_triangle_mesh(str(dst), mesh, write_ascii=True)


def _load_pointcloud(src_ply: Path, params: dict, _log):
    """Load the splat PLY into an Open3D PointCloud.

    splatfacto's PLY layout has gaussian centres in x/y/z and the
    DC SH band in f_dc_0/1/2 — Open3D ignores the SH attributes,
    we only need positions. Some PLYs carry nx/ny/nz; Open3D
    picks those up automatically.
    """
    import numpy as np
    import open3d as o3d

    pc = o3d.io.read_point_cloud(str(src_ply))
    if len(pc.points) == 0:
        # Open3D returns an empty cloud rather than raising on a
        # malformed file. Fall back to plyfile + manual construct.
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
        # If nx/ny/nz present, copy them too.
        if {"nx", "ny", "nz"}.issubset(set(v.dtype.names or ())):
            normals = np.column_stack([
                np.asarray(v["nx"], dtype=np.float64),
                np.asarray(v["ny"], dtype=np.float64),
                np.asarray(v["nz"], dtype=np.float64),
            ])
            if not np.allclose(normals, 0):
                pc.normals = o3d.utility.Vector3dVector(normals)
    return pc


def _obj_to_glb(src_obj: Path, dst_glb: Path) -> bool:
    """Best-effort .obj → .glb conversion via trimesh.

    Returns False (not raise) on failure: GLB is a nicer format for
    the three.js side but the OBJ is the authoritative mesh, so a
    quirk in trimesh's gltf writer shouldn't fail the whole job.
    """
    try:
        import trimesh

        mesh = trimesh.load(src_obj, force="mesh")
        mesh.export(dst_glb)
        return dst_glb.exists()
    except Exception as exc:  # noqa: BLE001
        log.warning("glb conversion failed: %s", exc)
        return False


async def _run_stub(
    *,
    mesh_dir: Path,
    params: dict,
    progress: ProgressCb,
    reason: str,
) -> dict:
    """Emit a placeholder OBJ + status note so the web side has
    something to render. The OBJ describes a unit cube — picked
    over e.g. a single triangle so the viewer's bounding sphere
    isn't degenerate."""
    await progress(0.4, f"mesh: synthetic ({reason})")
    obj = mesh_dir / "scene.obj"
    obj.write_text(_STUB_OBJ)
    note = mesh_dir / "mesh.log"
    note.write_text(
        f"stub run — {reason}\n"
        f"params: {params}\n"
        "no source .ply to mesh; emitted unit cube as placeholder.\n"
    )
    await progress(1.0, "mesh: done (stub)")
    return {"obj": str(obj), "stub": True, "reason": reason}


_STUB_OBJ = """\
# Synthetic placeholder cube — generated when no source .ply
# is available to mesh.
v -1.0 -1.0 -1.0
v  1.0 -1.0 -1.0
v  1.0  1.0 -1.0
v -1.0  1.0 -1.0
v -1.0 -1.0  1.0
v  1.0 -1.0  1.0
v  1.0  1.0  1.0
v -1.0  1.0  1.0
f 1 2 3 4
f 5 6 7 8
f 1 2 6 5
f 2 3 7 6
f 3 4 8 7
f 4 1 5 8
"""

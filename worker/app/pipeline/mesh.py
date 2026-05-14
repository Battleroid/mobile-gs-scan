"""Mesh extraction from a trained Gaussian-splatting scene.

Tiered: the user's ``mesh_params.tier`` field picks between
implementations. Only the **low** tier ships today; standard and
higher are scaffolded so the UI + API can preview their existence
without us needing to land OpenMVS / 2DGS / SuGaR in the same
change.

Low tier — TSDF fusion of rendered RGB+depth views
==================================================

The earlier Open3D-Poisson-from-splat-PLY path (now replaced) had
two fundamental issues that made its output unusable: gaussian
centers aren't on the surface (so PCA-normalled Poisson over them
forms a blobby closed shell around the subject), and the SH-DC
colors on the PLY were silently dropped (no vertex colors, no
textures). The low tier replaces both:

  1. Render a dome of orbital camera views from the trained
     splatfacto checkpoint via ``gsplat.rasterization`` (RGB+ED
     mode — alpha-weighted expected depth in a single forward pass).
  2. Integrate each (color, depth) frame into an Open3D
     ``ScalableTSDFVolume`` with ``RGB8`` color type, so per-voxel
     RGB accumulates alongside the truncated signed distance.
  3. Marching-cubes-extract a triangle mesh; vertices carry per-
     vertex RGB integrated across the views.
  4. Cluster-prune floaters (small disconnected components) without
     attempting to close holes or simplify — the partial surface is
     the design goal (see below).

This makes the output *surface-faithful* (TSDF sees the true
surface because rendered depth from many views converges on it)
and *partial-by-design*: regions the camera never saw are left as
open boundaries rather than invented as a closed back face.
Downstream import into Blender / other 3D media tooling is the
target workflow, where partial-but-correct beats whole-but-wrong.

We DON'T use nerfstudio's ``ns-export poisson`` here. As of
nerfstudio 1.1.5 that exporter asserts on a
``pipeline.datamanager.train_pixel_sampler`` that exists on the
ray-based managers but NOT on ``FullImageDatamanager`` (splatfacto).
``ns-export tsdf`` shares the same datamanager-coupled bug. Driving
``gsplat.rasterization`` directly bypasses both — same renderer
the orbit/thumbnail steps already use.

The actual Open3D + gsplat work runs in a CHILD PROCESS
(``app.pipeline._mesh_subprocess``) so the heartbeat task can
SIGKILL it via ``_running.kill_for_job``. Earlier versions ran
stages directly via ``asyncio.to_thread``: that fixed event-loop
blocking but left native C++ calls running on cancel, starving the
replacement job. The subprocess fork keeps hard-kill semantics.

Standard tier — OpenMVS textured mesh (sketch, not implemented)
===============================================================

Bolt OpenMVS onto the SfM output: ``DensifyPointCloud →
ReconstructMesh → RefineMesh → TextureMesh``. Produces a textured
OBJ + MTL + JPG bundle. Requires Dockerfile additions (OpenMVS +
VCG from source) and a new artifact-bundle serving route. Surfaces
when ``mesh_params.tier == "standard"`` — currently raises
``NotImplementedError`` from this module's dispatcher.

Higher tier — Mesh-aware splat retraining (sketch, not implemented)
===================================================================

2DGS or SuGaR — flatten / regularize the splat onto a surface,
then export. Multi-minute retraining pass. Surfaces when
``mesh_params.tier == "higher"`` — currently raises
``NotImplementedError``.

Output (all tiers, same contract):
  scene_dir/mesh/scene.obj    — canonical Wavefront mesh
  scene_dir/mesh/scene.glb    — glTF binary (when trimesh's writer
                                succeeds); rendered directly by
                                three.js's GLTFLoader on the web.
  scene_dir/mesh/mesh.log     — per-step trace surfaced via
                                JobLogPanel.
"""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import sys
from pathlib import Path
from typing import Awaitable, Callable

from app.pipeline import _running
from app.pipeline._logtail import format_subprocess_error, tail_file

log = logging.getLogger(__name__)

ProgressCb = Callable[[float, str], Awaitable[None]]

# Defaults tuned for the studio scenes we get out of phone captures
# (~1M splats, 5–10 m extent). The user can override any of these
# via POST /api/scenes/{id}/mesh's ``params`` body.
DEFAULT_PARAMS: dict = {
    # Which tier to run. "low" = TSDF-fusion (this PR). "standard"
    # = OpenMVS textured (future). "higher" = 2DGS/SuGaR retrain
    # (future). The dispatcher raises NotImplementedError for the
    # unimplemented tiers; the API validation surfaces a clean 400
    # before the job even queues.
    "tier": "low",
    # ─── low-tier (TSDF) knobs ──────────────────────────────────
    # Dome-camera view count. Spread evenly across
    # ``view_elevations`` rings; 96 views with two rings ≈ 48
    # azimuths per ring, dense enough to fill the TSDF for typical
    # scene coverage without taking minutes to render.
    "n_views": 96,
    # Normalized elevation angles in [-1, 1] mapped to [-π/2, π/2].
    # 0.2 ≈ 18°, 0.6 ≈ 54° — a low ring near the equator plus an
    # overhead ring gives reasonable dome coverage of a subject
    # the user captured by walking around it at chest height.
    "view_elevations": [0.2, 0.6],
    # Voxel edge length as a *fraction of scene extent*. The
    # subprocess scales by the splat's robust extent so the same
    # value gives a sensible voxel grid across captures of wildly
    # different physical scales. The default ~ 1/200 of extent
    # ≈ 200 voxels per side, a sensible mid-density mesh. Smaller
    # values (down to the API's (0, 0.1] range) give more detail at
    # higher memory cost; the subprocess clamps absurdly small
    # values (< 1/2048 of extent) only as a memory safety net.
    "voxel_size": 0.005,
    # SDF truncation distance, expressed as a multiple of the
    # (already extent-scaled) voxel size. 4 voxels of margin is
    # the Open3D / KinectFusion default and works well across
    # camera-distance regimes.
    "sdf_trunc_mult": 4.0,
    # Maximum depth to integrate, expressed as a multiple of scene
    # extent. Anything farther than this from a camera is treated
    # as "no surface" and skipped — avoids integrating background
    # gaussians far behind the subject. Tightened from 8.0 → 3.0
    # in the quality-knobs follow-up: 8× extent let every dome
    # camera reach the far wall of room-scale captures, fusing
    # background depth into the volume (the original "bubble"
    # artifact). 3× extent comfortably exceeds the dome radius
    # (~2× extent from centroid) without admitting room-far-wall
    # depth. Users can override.
    "depth_trunc": 3.0,
    # Floater removal pass after marching cubes: drop connected
    # components smaller than 1% of the largest cluster's triangle
    # count. DOES NOT close holes — partial-surface output is the
    # design goal. Turn off if a legitimate isolated island of
    # geometry < 1% of the main body gets dropped.
    "remove_outliers": True,
    # Crop final mesh to the splat's robust 1st/99th percentile
    # AABB. Off by default; flip on for scenes where a few far-out
    # gaussians dragged the mesh into empty space.
    "use_bounding_box": False,
    # Prefer the filter-edited splat (``scene.edited_ply_path``)
    # when available — without this, the mesh re-introduces every
    # floater the user just cleaned up. Default-on; flip off to
    # force the raw splatfacto export.
    "use_edited_splat": True,
    # Depth-validity gate. Pixels where the camera's accumulated
    # alpha falls below this threshold have their depth zeroed out
    # before TSDF integrate — the "expected depth" reported there
    # is the weighted average of stray floaters, not a real
    # surface. 0.5 is the empirical sweet spot for phone captures;
    # 0 disables the gate.
    "alpha_min": 0.5,
    # Robust-bbox percentile range used for: the dome camera-path
    # auto-fit, the depth_trunc reference extent, and the post-mesh
    # bbox crop (when ``use_bounding_box`` is on). Tighter than the
    # 5/95 default ``_ply_bbox`` uses for the orbit/thumbnail steps
    # because mesh quality is more sensitive to far-out floaters
    # being framed in.
    "bbox_percentile_low": 10.0,
    "bbox_percentile_high": 90.0,
    # Pre-render floater prune. ``floater_opacity_min`` drops
    # gaussians whose post-sigmoid opacity is below this value (the
    # same threshold the splat editor treats as background noise).
    # ``floater_scale_max_pct`` drops the top percentile of
    # gaussians by their largest scale axis — the wildly stretched
    # "sheet" gaussians that produce smeared depth. Set both to 0 /
    # 100 to disable.
    "floater_opacity_min": 0.05,
    "floater_scale_max_pct": 95.0,
    # ─── standard-tier (OpenMVS) knobs ─────────────────────────
    # Number of views fused at each densification step. Higher =
    # cleaner dense cloud but more expensive. OpenMVS recommends
    # 3–5 for typical photogrammetry workloads.
    "mvs_dense_views": 3,
    # Square texture-atlas page size in pixels. Power of 2; OpenMVS
    # picks the smallest power of 2 ≥ this value internally.
    # 4096 hits a reasonable sharpness / download-size balance for
    # phone-resolution captures; 8192 starts giving diminishing
    # returns above 4K input.
    "mvs_texture_size": 4096,
    # Number of RefineMesh iterations. 0 skips the photo-consistency
    # refinement pass entirely (the documented opt-out for RAM-
    # constrained hosts; the mesh remains usable, just less
    # detailed). 2 is the documented default — comfortable for the
    # 1080p / 4K capture ceiling this pipeline targets.
    "mvs_refine_iters": 2,
    # ─── legacy keys, accepted-but-ignored ──────────────────────
    # These were the Poisson-tier knobs. Persisted on scenes
    # extracted before the TSDF switch; we accept them so older
    # mesh_params rows don't 422 on the next extract, but the
    # subprocess no longer reads them.
    "num_points": 1_000_000,
    "depth": 9,
    "density_quantile": 0.01,
    "normal_method": "open3d",
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

    tier = merged.get("tier") or "low"
    if tier == "low":
        return await _run_low_tier(
            src_ply=src_ply,
            mesh_dir=mesh_dir,
            params=merged,
            progress=progress,
            job_id=job_id,
        )
    if tier == "standard":
        return await _run_mvs(
            scene_dir=scene_dir,
            mesh_dir=mesh_dir,
            params=merged,
            progress=progress,
            job_id=job_id,
        )
    if tier == "higher":
        # Surfaced as a RuntimeError-shaped exception below — the
        # runner catches any exception out of run_mesh and writes
        # ``mesh_error``. The API also rejects this tier at
        # validation time so this branch only fires for jobs
        # already in flight when a future tier is added.
        raise NotImplementedError(
            f"mesh tier '{tier}' is not yet implemented; "
            f"active tiers: 'low' (TSDF fusion), 'standard' "
            f"(OpenMVS textured)."
        )
    raise ValueError(f"unknown mesh tier '{tier}'")


async def _run_low_tier(
    *,
    src_ply: Path,
    mesh_dir: Path,
    params: dict,
    progress: ProgressCb,
    job_id: str | None,
) -> dict:
    """Dispatch the TSDF subprocess. The parent owns staging-dir +
    atomic-swap hygiene and the subprocess SIGKILL contract; the
    child does all the actual gsplat + Open3D work.
    """
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

    # Spawn the TSDF pipeline as a child process and register it
    # with _running so the heartbeat can SIGKILL it on cancel. Use
    # sys.executable (parent's interpreter) so we inherit the same
    # virtualenv / conda env / system Python.
    cmd = [
        sys.executable, "-m", "app.pipeline._mesh_subprocess",
        "--src-ply", str(src_ply),
        "--staging-dir", str(staging_dir),
        "--params", json.dumps(params),
        "--tier", "low",
    ]

    await progress(0.0, "spawn tsdf worker")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    if job_id is not None:
        _running.register(job_id, proc)

    try:
        rc = await _stream_progress(proc, log_path, progress)
    finally:
        if job_id is not None:
            _running.unregister(job_id)

    if rc != 0:
        # Non-zero exit covers both organic failures and SIGKILL
        # from the heartbeat. The runner's cancel path checks the
        # DB row to decide whether to treat this as cancel vs.
        # crash, so the same RuntimeError shape is fine for both.
        shutil.rmtree(staging_dir, ignore_errors=True)
        tail = tail_file(log_path)
        raise RuntimeError(
            format_subprocess_error("tsdf mesh", rc, log_path, tail)
        )

    # Atomic swap from staging into mesh_dir. Path.replace is
    # atomic on POSIX within the same filesystem; we're staging
    # inside mesh_dir/.staging-<job_id> so we're guaranteed same
    # device. Until this point /artifacts/{obj,glb} still serves
    # the prior mesh.
    obj_dst = mesh_dir / "scene.obj"
    glb_dst = mesh_dir / "scene.glb"
    has_glb = False
    try:
        staged_obj = staging_dir / "scene.obj"
        staged_glb = staging_dir / "scene.glb"
        if not staged_obj.exists():
            raise RuntimeError(
                "tsdf mesh exited 0 but produced no scene.obj"
            )
        staged_obj.replace(obj_dst)
        if staged_glb.exists():
            staged_glb.replace(glb_dst)
            has_glb = True
        elif glb_dst.exists():
            # New run produced no glb (trimesh hiccup) but a prior
            # glb still sits next to obj_dst. That's stale — drop
            # it so we don't serve a glb derived from an older obj.
            try:
                glb_dst.unlink()
            except OSError:
                pass
        # Standard-tier sidecars (scene.mtl + scene_tex*.{jpg,png})
        # from a prior extraction would otherwise sit next to the
        # new low-tier vertex-colored OBJ. The MTLLoader pre-pass on
        # the web client probes ``scene.mtl`` first and applies it
        # whenever the fetch returns 200 — leaving stale textures
        # would paint the new geometry with the old run's atlas.
        # Clean them defensively; the low-tier output has no MTL
        # reference of its own, so dropping these is always safe.
        stale_mtl = mesh_dir / "scene.mtl"
        if stale_mtl.exists():
            try:
                stale_mtl.unlink()
            except OSError:
                pass
        for stale_tex in list(mesh_dir.glob("scene_tex*.jpg")) + list(
            mesh_dir.glob("scene_tex*.png")
        ):
            try:
                stale_tex.unlink()
            except OSError:
                pass
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)

    result: dict[str, str | int] = {"obj": str(obj_dst)}
    if has_glb:
        result["glb"] = str(glb_dst)
    await progress(1.0, "mesh: done")
    return result


async def _run_mvs(
    *,
    scene_dir: Path,
    mesh_dir: Path,
    params: dict,
    progress: ProgressCb,
    job_id: str | None,
) -> dict:
    """Dispatch the OpenMVS textured-mesh orchestrator. The parent
    owns staging + atomic-swap hygiene and the subprocess SIGKILL
    contract via the process-group cancel path; the child shells
    out to the OpenMVS binaries in sequence.

    Differences from ``_run_low_tier``:
      * The subprocess takes ``--transforms-json`` + ``--images-src-dir``
        instead of ``--src-ply``. OpenMVS works from the SfM step's
        outputs directly, not the trained splat.
      * The subprocess is spawned with ``start_new_session=True`` so
        ``proc.pid`` is also its process-group id. ``_running.register``
        is told to use ``pgid=True`` so ``os.killpg`` reaches every
        OpenMVS grandchild on cancel (without this a user cancel
        leaves a multi-GB ``DensifyPointCloud`` running for minutes).
      * The bundle output is N files (OBJ + MTL + 1+ JPGs + optional
        GLB), not 2; the swap is per-file with explicit cleanup of
        stale ``scene*`` siblings under ``mesh_dir`` before move.
        Brief multi-file inconsistency window during the swap is
        acceptable — the WS-driven UI doesn't request artifacts
        mid-swap, and any HTTP request that lands in the gap gets a
        clean 404 from the new mesh_assets route.
    """
    # Sibling staging so the subprocess can freely create whatever
    # intermediate workspace files it wants (colmap/, scene.mvs,
    # scene_dense.mvs, ...) without polluting mesh_dir. The
    # subprocess GCs its intermediates before exiting; only the
    # canonical bundle (obj/mtl/jpg/glb) lands here.
    staging_dir = scene_dir / f".mesh-staging-{job_id or 'anon'}"
    if staging_dir.exists():
        shutil.rmtree(staging_dir, ignore_errors=True)
    staging_dir.mkdir(parents=True)

    mesh_dir.mkdir(parents=True, exist_ok=True)
    log_path = mesh_dir / "mesh.log"
    log_path.write_text("")

    transforms_path = scene_dir / "sfm" / "transforms.json"
    images_src_dir = scene_dir / "sfm" / "images"
    if not transforms_path.exists():
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise RuntimeError(
            f"standard-tier mesh requires {transforms_path}; "
            f"SfM step has not run or produced no transforms.json"
        )

    cmd = [
        sys.executable, "-m", "app.pipeline._mvs_subprocess",
        "--transforms-json", str(transforms_path),
        "--images-src-dir", str(images_src_dir),
        "--staging-dir", str(staging_dir),
        "--params", json.dumps(params),
    ]

    await progress(0.0, "spawn openmvs worker")

    # ``start_new_session=True`` makes proc.pid the leader of a new
    # process group; the OpenMVS binaries the orchestrator spawns
    # inherit that pgid, so a single ``os.killpg`` from the
    # heartbeat reaches every grandchild on cancel.
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
        start_new_session=True,
    )
    if job_id is not None:
        _running.register(job_id, proc, pgid=True)

    try:
        rc = await _stream_progress(proc, log_path, progress)
    finally:
        if job_id is not None:
            _running.unregister(job_id)

    if rc != 0:
        shutil.rmtree(staging_dir, ignore_errors=True)
        tail = tail_file(log_path)
        raise RuntimeError(
            format_subprocess_error("openmvs", rc, log_path, tail)
        )

    staged_obj = staging_dir / "scene.obj"
    staged_mtl = staging_dir / "scene.mtl"
    # The subprocess emits texture pages with whichever extension
    # OpenMVS's TextureMesh picked (JPG by default, PNG on some
    # build configs). Glob both — the artifact-route allowlist
    # already serves both extensions.
    staged_tex = sorted(
        list(staging_dir.glob("scene_tex*.jpg"))
        + list(staging_dir.glob("scene_tex*.png"))
    )
    staged_glb = staging_dir / "scene.glb"
    if not staged_obj.exists() or not staged_mtl.exists() or not staged_tex:
        shutil.rmtree(staging_dir, ignore_errors=True)
        raise RuntimeError(
            "openmvs exited 0 but the bundle is incomplete: "
            f"obj={staged_obj.exists()} mtl={staged_mtl.exists()} "
            f"tex_count={len(staged_tex)}"
        )

    # Clean any stale ``scene*`` files from mesh_dir before moving
    # the new bundle in. Stale textures from a prior run (e.g. the
    # previous mesh atlas had 3 pages and the new one has 1) would
    # otherwise sit alongside the new MTL forever, never referenced
    # but consuming disk. Preserve ``mesh.log`` and any non-scene
    # files. The new bundle's content is verified above so this
    # cleanup is safe — we have something to move in.
    for old in mesh_dir.iterdir():
        if old.is_file() and old.name.startswith("scene"):
            try:
                old.unlink()
            except OSError:
                pass

    staged_obj.replace(mesh_dir / "scene.obj")
    staged_mtl.replace(mesh_dir / "scene.mtl")
    tex_paths: list[str] = []
    for tex in staged_tex:
        dst = mesh_dir / tex.name
        tex.replace(dst)
        tex_paths.append(str(dst))
    has_glb = False
    if staged_glb.exists():
        staged_glb.replace(mesh_dir / "scene.glb")
        has_glb = True

    shutil.rmtree(staging_dir, ignore_errors=True)

    result: dict = {
        "obj": str(mesh_dir / "scene.obj"),
        "mtl": str(mesh_dir / "scene.mtl"),
        "tex": tex_paths,
    }
    if has_glb:
        result["glb"] = str(mesh_dir / "scene.glb")
    await progress(1.0, "mesh: done")
    return result


async def _stream_progress(
    proc: asyncio.subprocess.Process,
    log_path: Path,
    progress: ProgressCb,
) -> int:
    """Pump the subprocess's stdout into the .log file, surfacing
    ``PROGRESS <fraction> <msg>`` lines via the progress callback.

    Throttles callback invocations to ~1% increments so a chatty
    subprocess can't flood the WS layer.
    """
    last_pct = -1.0
    with log_path.open("wb") as logf:
        assert proc.stdout is not None
        async for raw in proc.stdout:
            logf.write(raw)
            line = raw.decode("utf-8", errors="replace").strip()
            if not line.startswith("PROGRESS "):
                continue
            parts = line.split(" ", 2)
            if len(parts) < 2:
                continue
            try:
                pct = float(parts[1])
            except ValueError:
                continue
            msg = parts[2] if len(parts) > 2 else ""
            if pct - last_pct >= 0.01 or pct >= 1.0:
                await progress(pct, msg)
                last_pct = pct
    return await proc.wait()


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

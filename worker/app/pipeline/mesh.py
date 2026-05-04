"""Poisson mesh extraction from a trained Gaussian-splatting scene.

We DON'T use nerfstudio's ``ns-export poisson`` here. As of
nerfstudio 1.1.5 that exporter asserts on a
``pipeline.datamanager.train_pixel_sampler`` that exists on the
ray-based managers (vanilla nerf etc) but NOT on
``FullImageDatamanager`` — which is what splatfacto uses. Result:
``ns-export poisson`` against any splatfacto-trained scene crashes
with ``AttributeError: 'FullImageDatamanager' object has no
attribute 'train_pixel_sampler'``. There's no flag to opt out.

Instead, run Open3D's Poisson reconstruction against the gaussian-
splat ``.ply`` the export step already produced. The splat PLY is
a point cloud of gaussian centres + per-vertex attributes — exactly
the input the surface reconstruction needs.

The actual Open3D work runs in a CHILD PROCESS
(``app.pipeline._mesh_subprocess``) so the heartbeat task can
SIGKILL it via ``_running.kill_for_job``. Earlier versions ran the
stages directly in the worker via ``asyncio.to_thread``: that fixed
event-loop blocking but left the native C++ Poisson call running
in a background thread on cancel, so cancel/replace flows could
overlap multiple long reconstructions and starve the replacement
job. The subprocess fork restores hard-kill semantics that the
original (nerfstudio-based) implementation had.

Pipeline (executed inside the subprocess):
  1. Load the splat PLY into an Open3D PointCloud (xyz + optional
     normals + optional colours from the SH DC band).
  2. Subsample / outlier-prune.
  3. Estimate normals via PCA on the local k-NN. (An older
     ``normal_method='model_output'`` option that trusted the PLY's
     own nx/ny/nz was removed — splatfacto exports those as zero,
     so trusting them silently degraded Poisson. The runner now
     coerces any legacy persisted value back to ``open3d``.)
  4. ``create_from_point_cloud_poisson(depth=…)`` for the surface.
  5. Density-prune low-confidence triangles (default: drop the
     bottom 1%) so the mesh isn't smeared out into the empty
     space around the subject.
  6. Export ``scene.obj`` + ``scene.glb`` via Open3D / trimesh.

The parent (this module) handles:
  - Per-job staging dir + atomic swap into the canonical mesh_dir
    so a concurrent re-extract doesn't clobber the live artefacts.
  - PROGRESS-line parsing from the subprocess's stdout.
  - Subprocess registration with ``_running`` for cancel-via-kill.

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
    # Target sample count after subsampling. Open3D's Poisson scales
    # roughly linearly with input size up to ~1M points; beyond
    # that the marginal density gain is dwarfed by runtime.
    "num_points": 1_000_000,
    # Statistical-outlier removal pass before normal estimation.
    # Splatfacto's gaussians sometimes drift outside the subject;
    # outlier removal stops them from polluting the surface.
    "remove_outliers": True,
    # Normal estimation method. Only ``open3d`` (PCA on each
    # point's k-nearest neighbours) is supported end-to-end; the
    # old ``model_output`` value was removed because splatfacto's
    # PLY normals are zero. _run_poisson coerces any legacy
    # persisted value back to "open3d" before dispatching to the
    # subprocess.
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

    # Normalize legacy normal_method here so the subprocess only
    # ever sees a supported value. _ALLOWED_NORMAL_METHODS in the
    # API rejects anything else on new requests, but the runner
    # uses scene.mesh_params verbatim when no overrides are
    # provided — bypassing that allowlist for legacy persisted
    # rows. See the equivalent guard in PR #67's review history.
    normalized = dict(params)
    nm = normalized.get("normal_method") or "open3d"
    if nm != "open3d":
        normalized["normal_method"] = "open3d"

    # Spawn the Open3D pipeline as a child process and register it
    # with _running so the heartbeat can SIGKILL it on cancel. Use
    # sys.executable (parent's interpreter) so we inherit the same
    # virtualenv / conda env / system Python.
    cmd = [
        sys.executable, "-m", "app.pipeline._mesh_subprocess",
        "--src-ply", str(src_ply),
        "--staging-dir", str(staging_dir),
        "--params", json.dumps(normalized),
    ]

    await progress(0.0, "spawn open3d worker")

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
            format_subprocess_error("open3d poisson", rc, log_path, tail)
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
                "open3d poisson exited 0 but produced no scene.obj"
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
    finally:
        shutil.rmtree(staging_dir, ignore_errors=True)

    result: dict[str, str | int] = {"obj": str(obj_dst)}
    if has_glb:
        result["glb"] = str(glb_dst)
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

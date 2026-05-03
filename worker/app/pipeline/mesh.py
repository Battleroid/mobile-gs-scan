"""Poisson mesh extraction from a trained Gaussian-splatting scene.

Wraps Nerfstudio's `ns-export poisson` against the latest config.yml
under the scene's train/ directory. Falls back to a synthetic stub
when nerfstudio isn't installed (mirrors the train + export step
fallbacks so the end-to-end pipeline stays runnable on a host
without the CUDA stack).

`ns-export poisson` defaults are reasonable for the studio scenes
we get out of phone captures (~1M splats, 5–10 m extent), but we
expose a couple of tunables on the recipe payload so the user can
re-run with a different sample count or remove-outliers threshold
without editing the worker config.

Output:
  scene_dir/mesh/scene.obj    — canonical Wavefront mesh
  scene_dir/mesh/scene.glb    — gltf binary (when trimesh is
                                installed); rendered directly by the
                                three.js GLTFLoader on the web side.
  scene_dir/mesh/mesh.log     — subprocess output, surfaced via
                                JobLogPanel.
"""
from __future__ import annotations

import asyncio
import logging
import re
import shutil
from pathlib import Path
from typing import Awaitable, Callable

from app.pipeline import _running
from app.pipeline._logtail import format_subprocess_error, tail_file

log = logging.getLogger(__name__)

ProgressCb = Callable[[float, str], Awaitable[None]]

PROGRESS_RE = re.compile(rb"(\d+(?:\.\d+)?)%")

# Defaults match nerfstudio's ns-export poisson out of the box. The
# user can override any of these via POST /api/scenes/{id}/mesh's
# `params` body to re-run with denser sampling, looser outlier
# pruning, etc.
DEFAULT_PARAMS: dict = {
    "num_points": 1_000_000,
    "remove_outliers": True,
    "normal_method": "open3d",
    "use_bounding_box": False,
}


async def run_mesh(
    *,
    scene_dir: Path,
    params: dict | None = None,
    progress: ProgressCb,
    job_id: str | None = None,
) -> dict:
    train_dir = scene_dir / "train"
    mesh_dir = scene_dir / "mesh"
    mesh_dir.mkdir(parents=True, exist_ok=True)

    merged = {**DEFAULT_PARAMS, **(params or {})}

    # Stub-friendly: falls through to a placeholder OBJ when the
    # synthetic-train marker exists or nerfstudio isn't installed.
    if (train_dir / "synthetic.json").exists() or not shutil.which("ns-export"):
        return await _run_stub(
            mesh_dir=mesh_dir,
            params=merged,
            progress=progress,
            reason=(
                "synthetic train output"
                if (train_dir / "synthetic.json").exists()
                else "ns-export not on PATH"
            ),
        )

    return await _run_poisson(
        train_dir=train_dir,
        mesh_dir=mesh_dir,
        params=merged,
        progress=progress,
        job_id=job_id,
    )


async def _run_poisson(
    *,
    train_dir: Path,
    mesh_dir: Path,
    params: dict,
    progress: ProgressCb,
    job_id: str | None,
) -> dict:
    candidates = sorted(train_dir.rglob("config.yml"))
    if not candidates:
        raise RuntimeError("no nerfstudio config.yml under train/")
    config = candidates[-1]

    # Sweep stale artefacts from prior runs before invoking ns-export.
    # The post-run lookup uses ``next(glob('*.obj'), None)`` to pick
    # whichever file the binary produced, but if ns-export writes only
    # .ply (versions vary), a leftover scene.obj from an earlier run
    # would get returned and the job would report success while
    # serving stale geometry. Clean every potential output file so
    # the glob below can't see anything but freshly-written content.
    for stale in (
        *mesh_dir.glob("*.obj"),
        *mesh_dir.glob("*.ply"),
        *mesh_dir.glob("*.glb"),
        *mesh_dir.glob("*.mtl"),
    ):
        try:
            stale.unlink()
        except OSError:
            pass

    await progress(0.05, "ns-export poisson")
    log_path = mesh_dir / "mesh.log"

    cmd: list[str] = [
        "ns-export", "poisson",
        "--load-config", str(config),
        "--output-dir", str(mesh_dir),
        "--num-points", str(int(params["num_points"])),
        "--remove-outliers", str(bool(params["remove_outliers"])),
        "--normal-method", str(params["normal_method"]),
        "--use-bounding-box", str(bool(params["use_bounding_box"])),
    ]

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    if job_id is not None:
        _running.register(job_id, proc)
    try:
        last_pct = 0.0
        with log_path.open("wb") as logf:
            assert proc.stdout is not None
            async for raw in proc.stdout:
                logf.write(raw)
                pm = PROGRESS_RE.search(raw)
                if pm:
                    try:
                        percent = float(pm.group(1))
                    except ValueError:
                        continue
                    # Map ns-export's stdout percentages into the
                    # 0.05 → 0.90 portion of our progress bar so the
                    # tail (obj/glb conversion) has room left.
                    pct = max(0.0, min(0.90, 0.05 + (percent / 100.0) * 0.85))
                    if pct - last_pct >= 0.01:
                        await progress(pct, f"poisson: {percent:.1f}%")
                        last_pct = pct
        rc = await proc.wait()
    finally:
        if job_id is not None:
            _running.unregister(job_id)

    if rc != 0:
        tail = tail_file(log_path)
        raise RuntimeError(
            format_subprocess_error("ns-export poisson", rc, log_path, tail)
        )

    obj = next(mesh_dir.glob("*.obj"), None)
    if obj is None:
        # ns-export poisson has flipped between writing .ply and .obj
        # across nerfstudio versions; convert ply → obj via trimesh
        # so the viewer always gets the canonical .obj artefact.
        ply_candidates = sorted(mesh_dir.glob("*.ply"))
        if not ply_candidates:
            raise RuntimeError("ns-export poisson produced no mesh artifact")
        await progress(0.92, "convert ply → obj")
        obj = mesh_dir / "scene.obj"
        _ply_to_obj(ply_candidates[-1], obj)

    obj_dst = mesh_dir / "scene.obj"
    if obj.resolve() != obj_dst.resolve():
        obj.replace(obj_dst)

    result: dict[str, str | int] = {"obj": str(obj_dst)}

    await progress(0.96, "convert obj → glb")
    glb_dst = mesh_dir / "scene.glb"
    if _obj_to_glb(obj_dst, glb_dst):
        result["glb"] = str(glb_dst)

    await progress(1.0, "mesh: done")
    return result


def _ply_to_obj(src_ply: Path, dst_obj: Path) -> None:
    import trimesh

    mesh = trimesh.load(src_ply, force="mesh")
    mesh.export(dst_obj)


def _obj_to_glb(src_obj: Path, dst_glb: Path) -> bool:
    """Best-effort .obj → .glb conversion via trimesh.

    Returns False (not raise) on failure: GLB is a nicer format for
    the three.js side but the OBJ is the authoritative mesh, so we
    don't want a quirk in trimesh's gltf writer to fail the whole
    job.
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
    something to render. The OBJ describes a unit cube — picked over
    e.g. a single triangle so the viewer's bounding sphere isn't
    degenerate."""
    await progress(0.4, f"mesh: synthetic ({reason})")
    obj = mesh_dir / "scene.obj"
    obj.write_text(_STUB_OBJ)
    note = mesh_dir / "mesh.log"
    note.write_text(
        f"stub run — {reason}\n"
        f"params: {params}\n"
        "no nerfstudio checkpoint to mesh; emitted unit cube as placeholder.\n"
    )
    await progress(1.0, "mesh: done (stub)")
    return {"obj": str(obj), "stub": True, "reason": reason}


_STUB_OBJ = """\
# Synthetic placeholder cube — generated when no nerfstudio
# checkpoint is available to mesh.
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

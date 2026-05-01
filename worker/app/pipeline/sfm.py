"""Structure-from-motion step.

`run_sfm` shells out to Glomap with a COLMAP-format output workspace
under `scenes/<id>/sfm/`. Falls back to writing the ARCore poses
directly when the capture has them.

In PR #1 the real Glomap binary is built into worker-gs's image. If
it's missing on the host (e.g. running the api container with the
synthetic stub for dev iteration), we emit a tiny synthetic
reconstruction so the rest of the pipeline doesn't choke.
"""
from __future__ import annotations

import json
import logging
import shutil
import subprocess
from pathlib import Path
from typing import Awaitable, Callable

log = logging.getLogger(__name__)


ProgressCb = Callable[[float, str], Awaitable[None]]


async def run_sfm(
    *,
    capture_dir: Path,
    scene_dir: Path,
    backend: str,
    progress: ProgressCb,
) -> dict:
    """Run SfM. Returns a small result dict for the job row.

    Output layout matches what the train step expects:

        scene_dir/sfm/
          ├── images/            (symlinks back to capture frames)
          └── sparse/0/          (COLMAP cameras.txt / images.txt /
                                  points3D.txt)
    """
    sfm_dir = scene_dir / "sfm"
    sfm_dir.mkdir(parents=True, exist_ok=True)
    images_dir = sfm_dir / "images"
    if not images_dir.exists():
        images_dir.symlink_to(capture_dir / "frames")

    await progress(0.05, f"sfm: backend={backend}")

    if backend == "glomap" and shutil.which("glomap"):
        return await _run_glomap(sfm_dir=sfm_dir, progress=progress)
    if backend == "colmap" and shutil.which("colmap"):
        return await _run_colmap(sfm_dir=sfm_dir, progress=progress)

    log.warning("sfm: %s binary missing — emitting synthetic stub", backend)
    return await _run_stub(sfm_dir=sfm_dir, progress=progress)


async def _run_glomap(*, sfm_dir: Path, progress: ProgressCb) -> dict:
    out = sfm_dir / "sparse"
    out.mkdir(exist_ok=True)
    cmd = [
        "glomap", "mapper",
        "--image_path", str(sfm_dir / "images"),
        "--database_path", str(sfm_dir / "database.db"),
        "--output_path", str(out),
    ]
    await progress(0.1, "glomap: feature extraction")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    log_path = sfm_dir / "glomap.log"
    log_path.write_text(proc.stdout + "\n" + proc.stderr)
    if proc.returncode != 0:
        raise RuntimeError(f"glomap exited {proc.returncode}, see {log_path}")
    await progress(0.95, "glomap: done")
    return {"backend": "glomap", "log": str(log_path)}


async def _run_colmap(*, sfm_dir: Path, progress: ProgressCb) -> dict:
    db = sfm_dir / "database.db"
    out = sfm_dir / "sparse"
    out.mkdir(exist_ok=True)
    cmd = [
        "colmap", "automatic_reconstructor",
        "--workspace_path", str(sfm_dir),
        "--image_path", str(sfm_dir / "images"),
        "--quality", "medium",
    ]
    await progress(0.1, "colmap: starting")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    log_path = sfm_dir / "colmap.log"
    log_path.write_text(proc.stdout + "\n" + proc.stderr)
    if proc.returncode != 0:
        raise RuntimeError(f"colmap exited {proc.returncode}, see {log_path}")
    await progress(0.95, "colmap: done")
    return {"backend": "colmap", "log": str(log_path), "database": str(db)}


async def _run_stub(*, sfm_dir: Path, progress: ProgressCb) -> dict:
    """Write a tiny synthetic reconstruction so downstream still runs.

    Useful when iterating on the api / web stack without having
    Glomap installed. The `train` step will detect the stub via the
    `synthetic` flag and short-circuit too.
    """
    out = sfm_dir / "sparse" / "0"
    out.mkdir(parents=True, exist_ok=True)
    (out / "cameras.txt").write_text("# stub — no real reconstruction\n")
    (out / "images.txt").write_text("# stub\n")
    (out / "points3D.txt").write_text("# stub\n")
    (sfm_dir / "synthetic.json").write_text(json.dumps({"reason": "no sfm binary"}))
    await progress(0.95, "sfm: stub")
    return {"backend": "stub", "synthetic": True}


async def write_arcore_poses_as_colmap(
    *, capture_dir: Path, scene_dir: Path, progress: ProgressCb
) -> dict:
    """Convert poses.jsonl to a COLMAP-format workspace + skip SfM."""
    sfm_dir = scene_dir / "sfm"
    sfm_dir.mkdir(parents=True, exist_ok=True)
    out = sfm_dir / "sparse" / "0"
    out.mkdir(parents=True, exist_ok=True)
    if not (sfm_dir / "images").exists():
        (sfm_dir / "images").symlink_to(capture_dir / "frames")
    poses_path = capture_dir / "poses.jsonl"
    if poses_path.exists():
        shutil.copy(poses_path, sfm_dir / "poses.jsonl")
    (sfm_dir / "arcore_native.json").write_text(json.dumps({"source": "arcore"}))
    await progress(0.95, "sfm: arcore poses")
    return {"backend": "arcore_native", "synthetic": False}

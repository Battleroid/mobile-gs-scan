"""Structure-from-motion step.

`run_sfm` shells out to Glomap with a COLMAP-format output workspace
under `scenes/<id>/sfm/`. Three backends:

  * ``glomap`` / ``colmap`` — real feature-based solvers; used when
    the capture lacks per-frame pose (web upload, drag-drop).
  * ``arcore_native`` — used when the phone streamed ARCore poses
    alongside the frames. Skips the solver and writes a synthetic
    marker so the train step falls back to its stub path. Real
    pose-conditioned training (writing valid COLMAP cameras.txt /
    images.txt + a triangulated points3D.txt) is a follow-up.

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

    if backend == "arcore_native":
        # Phone capture path. Don't run a real feature-based solver
        # — use the poses ARCore already gave us.
        return await write_arcore_poses_as_colmap(
            capture_dir=capture_dir, scene_dir=scene_dir, progress=progress
        )
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
    """ARCore-pose path. Wires the workspace + flags downstream.

    Currently writes:

      * symlink ``scene_dir/sfm/images`` → ``capture_dir/frames``.
      * a copy of the streamed ``poses.jsonl`` so the data lives
        under the scene rather than only the capture.
      * ``arcore_native.json`` marker so it's obvious which path
        produced this scene.
      * ``synthetic.json`` marker so train.py's stub fallback fires
        and the pipeline completes end-to-end. Real pose-conditioned
        training (write a valid COLMAP cameras.txt / images.txt +
        a triangulated sparse points3D.txt, then ns-train against
        that) is the follow-up; landing it here would balloon the
        scope of a fix that's just trying to stop the cascade
        crash on the phone-capture path.
    """
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
    # Triggers the stub branch in train.py until real ARCore-pose
    # training lands. Without this the train step would try to run
    # ns-train against an empty cameras.txt / points3D.txt and crash.
    (sfm_dir / "synthetic.json").write_text(
        json.dumps({
            "reason": "arcore_native",
            "todo": "emit valid COLMAP cameras.txt + triangulated points3D.txt",
        })
    )
    await progress(0.95, "sfm: arcore poses")
    return {"backend": "arcore_native", "synthetic": True}

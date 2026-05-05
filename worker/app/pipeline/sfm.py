"""Structure-from-motion step.

`run_sfm` produces the dataset workspace under `scenes/<id>/sfm/` that
the train step reads. Three backends:

  * ``glomap`` / ``colmap`` — real feature-based solvers; emit a
    COLMAP-shaped sparse/0/ workspace. Used when the capture lacks
    per-frame pose (web upload, drag-drop).
  * ``arcore_native`` — used when the phone streamed ARCore poses
    alongside the frames. Emits a *nerfstudio-native* dataset:
    transforms.json + points3D.ply. nerfstudio's splatfacto uses
    the ``nerfstudio-data`` dataparser by default, which expects
    transforms.json; emitting that directly is simpler than
    forcing the colmap dataparser via tyro overrides AND makes the
    math much easier (transforms.json wants cam-to-world in OpenGL
    convention, which is exactly what ARCore gives us — no axis
    flip, no inversion, no quaternion conversion).

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

import numpy as np

from app.pipeline._logtail import format_subprocess_error, tail_text

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
          ├── transforms.json    (arcore_native path)
          ├── points3D.ply       (arcore_native path)
          └── sparse/0/          (glomap / colmap path: COLMAP
                                  cameras.txt / images.txt /
                                  points3D.txt)
    """
    sfm_dir = scene_dir / "sfm"
    sfm_dir.mkdir(parents=True, exist_ok=True)
    images_dir = sfm_dir / "images"
    if not images_dir.exists():
        images_dir.symlink_to(capture_dir / "frames")

    await progress(0.05, f"sfm: backend={backend}")

    if backend == "arcore_native":
        return await write_arcore_transforms_json(
            capture_dir=capture_dir, scene_dir=scene_dir, progress=progress
        )
    if backend == "glomap" and shutil.which("glomap"):
        return await _run_glomap(sfm_dir=sfm_dir, progress=progress)
    if backend == "colmap" and shutil.which("colmap"):
        return await _run_colmap(sfm_dir=sfm_dir, progress=progress)

    log.warning("sfm: %s binary missing — emitting synthetic stub", backend)
    return await _run_stub(sfm_dir=sfm_dir, progress=progress)


async def _run_glomap(*, sfm_dir: Path, progress: ProgressCb) -> dict:
    """Real SfM via COLMAP feature pipeline + glomap mapper.

    Glomap's `mapper` operates on a populated COLMAP database. The
    upstream tool doesn't ship its own feature extractor / matcher;
    you're expected to run COLMAP's first. The previous version
    skipped both and called `glomap mapper` against a
    non-existent ``database.db`` — which surfaces as the unhelpful
    "`database_path` is not a file" abort.

    The flow:
      1. ``colmap feature_extractor`` → populates database.db
         with per-image keypoints + descriptors.
      2. ``colmap sequential_matcher`` → matches features between
         images that are close in filename order. Sequential is the
         right shape for video-extracted frames (and for the typical
         walk-around-the-object phone capture); for unordered drone
         / DSLR sets we'd need exhaustive_matcher, deferred until
         that ingestion path lands.
      3. ``glomap mapper`` → reconstructs cameras + sparse points
         into ``sfm_dir/sparse/``.

    All three subprocesses append to ``sfm_dir/glomap.log`` so the
    JobLogPanel sees a single combined log. Failure at any step
    raises with the tail of the combined log.
    """
    images_dir = sfm_dir / "images"
    db = sfm_dir / "database.db"
    out = sfm_dir / "sparse"
    out.mkdir(exist_ok=True)
    log_path = sfm_dir / "glomap.log"
    log_path.write_text("")

    # COLMAP's feature extractor / matcher live in /usr/local/bin
    # alongside glomap (both built from the same FETCH_COLMAP source
    # tree in Dockerfile.gs). The system-image's apt colmap is too
    # old; that's why glomap fetches its own copy.
    if not shutil.which("colmap"):
        raise RuntimeError(
            "colmap binary missing — required by the glomap pipeline. "
            "rebuild worker-gs (it's built alongside glomap)."
        )

    await progress(0.10, "glomap: feature extraction")
    _glomap_step(
        cmd=[
            "colmap", "feature_extractor",
            "--database_path", str(db),
            "--image_path", str(images_dir),
            "--ImageReader.single_camera", "1",
            "--SiftExtraction.use_gpu", "1",
        ],
        log_path=log_path,
        step_name="colmap feature_extractor",
    )

    await progress(0.35, "glomap: feature matching")
    _glomap_step(
        cmd=[
            "colmap", "sequential_matcher",
            "--database_path", str(db),
            # 25-frame overlap window: at typical 5–8 fps phone /
            # video extraction rates that's ~3-5s of motion, well
            # within the overlap any reasonable capture motion will
            # have. Bumping past 25 gets diminishing returns at O(n)
            # cost-per-extra-window-step.
            "--SequentialMatching.overlap", "25",
            "--SiftMatching.use_gpu", "1",
        ],
        log_path=log_path,
        step_name="colmap sequential_matcher",
    )

    await progress(0.65, "glomap: mapper")
    _glomap_step(
        cmd=[
            "glomap", "mapper",
            "--image_path", str(images_dir),
            "--database_path", str(db),
            "--output_path", str(out),
        ],
        log_path=log_path,
        step_name="glomap mapper",
    )

    await progress(0.95, "glomap: done")
    return {"backend": "glomap", "log": str(log_path), "database": str(db)}


def _glomap_step(*, cmd: list[str], log_path: Path, step_name: str) -> None:
    """Run one glomap-pipeline subprocess, appending to the shared log.

    Raises RuntimeError with the log tail on non-zero exit so the
    caller can surface a single error to the job row regardless of
    which step bailed.
    """
    proc = subprocess.run(cmd, capture_output=True, text=True)
    with log_path.open("a") as f:
        f.write(f"\n=== {step_name} ===\n")
        f.write(proc.stdout)
        f.write("\n")
        f.write(proc.stderr)
    if proc.returncode != 0:
        tail = tail_text(proc.stdout + "\n" + proc.stderr)
        raise RuntimeError(
            format_subprocess_error(step_name, proc.returncode, log_path, tail)
        )


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
        tail = tail_text(proc.stdout + "\n" + proc.stderr)
        raise RuntimeError(
            format_subprocess_error("colmap", proc.returncode, log_path, tail)
        )
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
    (sfm_dir / "synthetic.json").write_text(
        json.dumps({"reason": "no sfm binary"})
    )
    await progress(0.95, "sfm: stub")
    return {"backend": "stub", "synthetic": True}


# ─── ARCore → transforms.json ──────────────────────────────
#
# nerfstudio's transforms.json format — the native dataset shape its
# splatfacto method reads via the default ``nerfstudio-data``
# dataparser. Two reasons we emit this instead of COLMAP files:
#
# 1. splatfacto auto-picks ``nerfstudio-data`` as its dataparser.
#    PR #23 emitted COLMAP files but ns-train ignored them and
#    crashed looking for transforms.json. Forcing the colmap
#    dataparser via tyro override (``ns-train splatfacto colmap
#    --data ...``) is finicky to thread through; emitting what
#    nerfstudio expects is simpler.
# 2. transforms.json's per-frame ``transform_matrix`` is
#    cam-to-world in OpenGL convention (+X right, +Y up, +Z back)
#    — EXACTLY what ARCore writes to its 16-float pose buffer.
#    No axis flip, no inversion, no quaternion conversion. Just
#    transpose the column-major buffer to row-major and serialize.


async def write_arcore_transforms_json(
    *, capture_dir: Path, scene_dir: Path, progress: ProgressCb
) -> dict:
    """Convert ARCore poses.jsonl to a nerfstudio-native dataset.

    Output layout under ``scene_dir/sfm/``:

      images/                   symlink to capture_dir/frames
      poses.jsonl               copy of the streamed poses (debug)
      arcore_native.json        marker so it's obvious which path ran
      transforms.json           per-frame cam-to-world (OpenGL) +
                                shared OPENCV pinhole intrinsics +
                                ply_file_path -> points3D.ply
      points3D.ply              5000 random ASCII-PLY seed points
                                sampled in a sphere around the
                                centroid of camera positions, used
                                by splatfacto's gaussian init.

    Falls back to the synthetic stub if poses.jsonl is missing or
    has no usable entries — the pipeline still completes via stubs.
    """
    sfm_dir = scene_dir / "sfm"
    sfm_dir.mkdir(parents=True, exist_ok=True)
    if not (sfm_dir / "images").exists():
        (sfm_dir / "images").symlink_to(capture_dir / "frames")

    poses_path = capture_dir / "poses.jsonl"
    if not poses_path.exists():
        return await _arcore_synthetic_fallback(
            sfm_dir=sfm_dir, progress=progress, reason="no poses.jsonl"
        )
    shutil.copy(poses_path, sfm_dir / "poses.jsonl")

    intrinsics: dict | None = None
    with poses_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            if entry.get("pose") is None:
                continue
            if intrinsics is None and entry.get("intrinsics"):
                intrinsics = entry["intrinsics"]

    if intrinsics is None:
        return await _arcore_synthetic_fallback(
            sfm_dir=sfm_dir, progress=progress, reason="no usable poses"
        )

    fx = float(intrinsics["fx"])
    fy = float(intrinsics["fy"])
    cx = float(intrinsics["cx"])
    cy = float(intrinsics["cy"])
    w = int(intrinsics["w"])
    h = int(intrinsics["h"])

    frames: list[dict] = []
    cam_positions = np.empty((128, 3), dtype=np.float64)
    cam_positions_count = 0
    written = 0
    with poses_path.open() as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                entry = json.loads(line)
            except json.JSONDecodeError:
                continue
            pose16 = entry.get("pose")
            if pose16 is None:
                continue
            idx = int(entry["idx"])
            if not isinstance(pose16, list) or len(pose16) != 16:
                log.warning("arcore: skipping pose at idx=%s (bad shape)", idx)
                continue
            # ARCore writes 4x4 column-major into the 16-float buffer.
            # numpy's reshape is row-major, so .T gives the canonical
            # 4x4 mathematical matrix. nerfstudio wants this verbatim:
            # cam-to-world, OpenGL camera convention, row-major nested
            # list with the bottom row [0, 0, 0, 1].
            M = np.array(pose16, dtype=np.float64).reshape(4, 4).T
            # Sanity: bottom row should be (~0, 0, 0, 1) for a rigid
            # transform. If it isn't, the pose is junk; skip.
            bottom = M[3, :]
            if abs(bottom[3] - 1.0) > 1e-3 or np.linalg.norm(bottom[:3]) > 1e-3:
                log.warning("arcore: skipping pose at idx=%s (non-affine)", idx)
                continue
            frames.append({
                "file_path": f"images/{idx:06d}.jpg",
                "transform_matrix": M.tolist(),
            })
            if cam_positions_count == cam_positions.shape[0]:
                grown = np.empty((cam_positions.shape[0] * 2, 3), dtype=np.float64)
                grown[:cam_positions_count] = cam_positions
                cam_positions = grown
            cam_positions[cam_positions_count] = M[:3, 3]
            cam_positions_count += 1
            written += 1

    if written == 0:
        return await _arcore_synthetic_fallback(
            sfm_dir=sfm_dir, progress=progress, reason="all poses unparseable"
        )

    # Build the nerfstudio transforms.json. camera_model="OPENCV"
    # without distortion params is equivalent to a clean PINHOLE
    # camera — ARCore's reported intrinsics are already pinhole.
    transforms = {
        "camera_model": "OPENCV",
        "fl_x": fx,
        "fl_y": fy,
        "cx": cx,
        "cy": cy,
        "w": w,
        "h": h,
        "frames": frames,
        "ply_file_path": "points3D.ply",
    }
    (sfm_dir / "transforms.json").write_text(
        json.dumps(transforms, indent=2)
    )

    # Seed points3D.ply: random points in a sphere around the
    # centroid of camera positions. splatfacto's nerfstudio-data
    # dataparser reads ply_file_path and uses these as initial
    # gaussian centers + colors. Splatfacto densifies + prunes
    # during training, so the seed only needs to be roughly in
    # the right region.
    cam_positions_arr = cam_positions[:cam_positions_count]
    center = cam_positions_arr.mean(axis=0)
    spread = float(np.linalg.norm(cam_positions_arr - center, axis=1).max())
    radius = max(0.5, spread)
    n_seeds = 5_000
    rng = np.random.default_rng(42)
    directions = rng.normal(size=(n_seeds, 3))
    directions /= np.linalg.norm(directions, axis=1, keepdims=True)
    radii = rng.uniform(0.0, 1.0, size=n_seeds) ** (1.0 / 3.0) * radius
    seed_pts = center + directions * radii[:, None]
    seed_colors = rng.integers(0, 256, size=(n_seeds, 3))
    _write_ascii_ply(sfm_dir / "points3D.ply", seed_pts, seed_colors)

    (sfm_dir / "arcore_native.json").write_text(
        json.dumps(
            {
                "source": "arcore",
                "frames": written,
                "seed_points": n_seeds,
                "intrinsics": {"fx": fx, "fy": fy, "cx": cx, "cy": cy, "w": w, "h": h},
            }
        )
    )

    log.info(
        "arcore: wrote nerfstudio dataset (%d frames, %d seed points)",
        written,
        n_seeds,
    )
    await progress(
        0.95,
        f"sfm: arcore poses → transforms.json ({written} frames, {n_seeds} seeds)",
    )
    return {
        "backend": "arcore_native",
        "synthetic": False,
        "frames": written,
        "seed_points": n_seeds,
    }


def _write_ascii_ply(
    path: Path, points: np.ndarray, colors: np.ndarray
) -> None:
    """Minimal ASCII PLY: vertex element with float xyz + uchar rgb.

    splatfacto reads this via plyfile / open3d — either accepts the
    format. ASCII is a few KB larger than binary at 5000 points but
    much easier to inspect and we're not bottlenecked on disk here.
    """
    n = len(points)
    lines: list[str] = [
        "ply\n",
        "format ascii 1.0\n",
        f"element vertex {n}\n",
        "property float x\n",
        "property float y\n",
        "property float z\n",
        "property uchar red\n",
        "property uchar green\n",
        "property uchar blue\n",
        "end_header\n",
    ]
    for pt, col in zip(points, colors):
        lines.append(
            f"{float(pt[0])} {float(pt[1])} {float(pt[2])} "
            f"{int(col[0])} {int(col[1])} {int(col[2])}\n"
        )
    path.write_text("".join(lines))


async def _arcore_synthetic_fallback(
    *, sfm_dir: Path, progress: ProgressCb, reason: str
) -> dict:
    """Used when the ARCore conversion can't proceed (missing
    poses.jsonl, no usable entries, etc). Drops a synthetic stub so
    train.py's stub branch fires and the pipeline still completes
    end-to-end.
    """
    out = sfm_dir / "sparse" / "0"
    out.mkdir(parents=True, exist_ok=True)
    (out / "cameras.txt").write_text("# stub — arcore conversion bailed\n")
    (out / "images.txt").write_text("# stub\n")
    (out / "points3D.txt").write_text("# stub\n")
    (sfm_dir / "arcore_native.json").write_text(
        json.dumps({"source": "arcore", "failed": True, "reason": reason})
    )
    (sfm_dir / "synthetic.json").write_text(
        json.dumps({"reason": f"arcore_native: {reason}"})
    )
    log.warning("arcore: %s — falling through to synthetic stub", reason)
    await progress(0.95, f"sfm: arcore stub ({reason})")
    return {
        "backend": "arcore_native_stub",
        "synthetic": True,
        "reason": reason,
    }

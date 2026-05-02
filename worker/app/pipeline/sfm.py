"""Structure-from-motion step.

`run_sfm` shells out to Glomap with a COLMAP-format output workspace
under `scenes/<id>/sfm/`. Three backends:

  * ``glomap`` / ``colmap`` — real feature-based solvers; used when
    the capture lacks per-frame pose (web upload, drag-drop).
  * ``arcore_native`` — used when the phone streamed ARCore poses
    alongside the frames. Converts poses.jsonl into a COLMAP-shaped
    workspace (cameras.txt + images.txt + seed points3D.txt) so
    splatfacto can train against the known poses without running a
    real solver.

In PR #1 the real Glomap binary is built into worker-gs's image. If
it's missing on the host (e.g. running the api container with the
synthetic stub for dev iteration), we emit a tiny synthetic
reconstruction so the rest of the pipeline doesn't choke.
"""
from __future__ import annotations

import json
import logging
import math
import shutil
import subprocess
from pathlib import Path
from typing import Awaitable, Callable

import numpy as np

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
    (sfm_dir / "synthetic.json").write_text(
        json.dumps({"reason": "no sfm binary"})
    )
    await progress(0.95, "sfm: stub")
    return {"backend": "stub", "synthetic": True}


# ─── ARCore → COLMAP ──────────────────────────────────────
#
# Convert the per-frame ARCore poses streamed by the phone into
# the COLMAP workspace ns-train splatfacto needs.
#
# The math:
#
# 1. ARCore writes its 4x4 pose matrices in COLUMN-major order to
#    the float[16] buffer, so we have to transpose after reshape
#    (numpy defaults to row-major / C order).
#
# 2. Each ARCore pose is camera-to-world in the OpenGL camera
#    convention: +X right, +Y up, +Z back (camera looks down -Z).
#
# 3. COLMAP's images.txt expects WORLD-to-camera in the OpenCV
#    camera convention: +X right, +Y down, +Z forward (camera looks
#    down +Z). Two conversions:
#      a) Switch camera-frame convention via right-multiply by
#         diag(1, -1, -1, 1) — flip Y and Z axes of the camera frame
#         (world frame is unchanged).
#      b) Invert the resulting cam-to-world to get world-to-cam.
#         For a rigid transform, the inverse is the transpose of the
#         rotation block plus a recomputed translation — no
#         numerical inversion needed.
#
# 4. COLMAP's quaternion is Hamilton convention with (qw, qx, qy, qz)
#    field order in the file.

_GL_CAM_TO_CV_CAM = np.diag([1.0, -1.0, -1.0, 1.0])


def _arcore_pose_to_world_to_cam(pose16: list) -> tuple[np.ndarray, np.ndarray]:
    """ARCore cam-to-world (OpenGL camera, column-major float[16])
    → COLMAP world-to-cam (OpenCV camera). Returns (R, t) where R
    is 3x3 and t is length-3.
    """
    if len(pose16) != 16:
        raise ValueError(f"expected 16-float pose, got {len(pose16)}")
    # Transpose because ARCore writes column-major.
    M_c2w_gl = np.array(pose16, dtype=np.float64).reshape(4, 4).T
    # Switch camera convention.
    M_c2w_cv = M_c2w_gl @ _GL_CAM_TO_CV_CAM
    # Rigid inverse: cam_to_world R, t → world_to_cam R^T, -R^T t.
    R_c2w = M_c2w_cv[:3, :3]
    t_c2w = M_c2w_cv[:3, 3]
    R_w2c = R_c2w.T
    t_w2c = -R_w2c @ t_c2w
    return R_w2c, t_w2c


def _rot_to_quat_wxyz(R: np.ndarray) -> tuple[float, float, float, float]:
    """Rotation matrix → (qw, qx, qy, qz) Hamilton quaternion.

    Standard Shepperd's method — picks the largest diagonal element
    branch to avoid numerical issues near gimbal poses.
    """
    trace = R[0, 0] + R[1, 1] + R[2, 2]
    if trace > 0:
        s = math.sqrt(trace + 1.0) * 2
        qw = 0.25 * s
        qx = (R[2, 1] - R[1, 2]) / s
        qy = (R[0, 2] - R[2, 0]) / s
        qz = (R[1, 0] - R[0, 1]) / s
    elif R[0, 0] > R[1, 1] and R[0, 0] > R[2, 2]:
        s = math.sqrt(1.0 + R[0, 0] - R[1, 1] - R[2, 2]) * 2
        qw = (R[2, 1] - R[1, 2]) / s
        qx = 0.25 * s
        qy = (R[0, 1] + R[1, 0]) / s
        qz = (R[0, 2] + R[2, 0]) / s
    elif R[1, 1] > R[2, 2]:
        s = math.sqrt(1.0 + R[1, 1] - R[0, 0] - R[2, 2]) * 2
        qw = (R[0, 2] - R[2, 0]) / s
        qx = (R[0, 1] + R[1, 0]) / s
        qy = 0.25 * s
        qz = (R[1, 2] + R[2, 1]) / s
    else:
        s = math.sqrt(1.0 + R[2, 2] - R[0, 0] - R[1, 1]) * 2
        qw = (R[1, 0] - R[0, 1]) / s
        qx = (R[0, 2] + R[2, 0]) / s
        qy = (R[1, 2] + R[2, 1]) / s
        qz = 0.25 * s
    return qw, qx, qy, qz


async def write_arcore_poses_as_colmap(
    *, capture_dir: Path, scene_dir: Path, progress: ProgressCb
) -> dict:
    """Convert poses.jsonl to a COLMAP-format workspace.

    Output layout under ``scene_dir/sfm/``:

      images/                   symlink to capture_dir/frames
      poses.jsonl               copy of the streamed poses (debug)
      arcore_native.json        marker so it's obvious which path ran
      sparse/0/cameras.txt      single PINHOLE camera, intrinsics from
                                the first frame's `intrinsics` payload
      sparse/0/images.txt       one entry per pose row, with the
                                converted (qw, qx, qy, qz, tx, ty, tz)
      sparse/0/points3D.txt     5000 random points sampled in a
                                sphere around the centroid of camera
                                positions, to seed splatfacto's
                                gaussian initialization. (Without
                                this splatfacto either errors or
                                produces unstable training; we have
                                no real sparse reconstruction since
                                no SfM solver ran.)

    Falls back to the synthetic stub if poses.jsonl is missing or
    has no usable entries — the pipeline still completes via stubs.
    """
    sfm_dir = scene_dir / "sfm"
    sfm_dir.mkdir(parents=True, exist_ok=True)
    out = sfm_dir / "sparse" / "0"
    out.mkdir(parents=True, exist_ok=True)
    if not (sfm_dir / "images").exists():
        (sfm_dir / "images").symlink_to(capture_dir / "frames")

    poses_path = capture_dir / "poses.jsonl"
    if not poses_path.exists():
        return await _arcore_synthetic_fallback(
            sfm_dir=sfm_dir, progress=progress, reason="no poses.jsonl"
        )
    shutil.copy(poses_path, sfm_dir / "poses.jsonl")

    poses: list[dict] = []
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
            poses.append(entry)

    if not poses or intrinsics is None:
        return await _arcore_synthetic_fallback(
            sfm_dir=sfm_dir, progress=progress, reason="no usable poses"
        )

    fx = float(intrinsics["fx"])
    fy = float(intrinsics["fy"])
    cx = float(intrinsics["cx"])
    cy = float(intrinsics["cy"])
    w = int(intrinsics["w"])
    h = int(intrinsics["h"])

    # cameras.txt: one shared PINHOLE camera id=1.
    (out / "cameras.txt").write_text(
        "# Camera list with one line of data per camera:\n"
        "#   CAMERA_ID, MODEL, WIDTH, HEIGHT, PARAMS[]\n"
        f"1 PINHOLE {w} {h} {fx} {fy} {cx} {cy}\n"
    )

    # images.txt: two lines per image — the data row, then an empty
    # POINTS2D row (we have no detected features to associate).
    images_lines = [
        "# Image list with two lines of data per image:\n",
        "#   IMAGE_ID, QW, QX, QY, QZ, TX, TY, TZ, CAMERA_ID, NAME\n",
        "#   POINTS2D[] as (X, Y, POINT3D_ID)\n",
    ]
    cam_positions: list[np.ndarray] = []
    written = 0
    for image_id, entry in enumerate(poses, start=1):
        idx = int(entry["idx"])
        try:
            R, t = _arcore_pose_to_world_to_cam(entry["pose"])
        except Exception:  # noqa: BLE001
            log.warning("arcore: skipping unparseable pose at idx=%s", idx)
            continue
        qw, qx, qy, qz = _rot_to_quat_wxyz(R)
        name = f"{idx:06d}.jpg"
        images_lines.append(
            f"{image_id} {qw} {qx} {qy} {qz} {t[0]} {t[1]} {t[2]} 1 {name}\n"
        )
        images_lines.append("\n")  # empty POINTS2D line
        # Camera position in world space, for centroid-based seed sampling.
        cam_positions.append(-R.T @ t)
        written += 1

    if written == 0:
        return await _arcore_synthetic_fallback(
            sfm_dir=sfm_dir, progress=progress, reason="all poses unparseable"
        )

    (out / "images.txt").write_text("".join(images_lines))

    # points3D.txt: splatfacto's COLMAP dataparser uses these as
    # initial gaussian centers. With no SfM solver we have no real
    # sparse reconstruction; seed with random points distributed in
    # a sphere around the centroid of camera positions. Splatfacto
    # densifies / prunes during training, so the kickoff just needs
    # to be roughly in the right region.
    cam_positions_arr = np.array(cam_positions)
    center = cam_positions_arr.mean(axis=0)
    spread = float(np.linalg.norm(cam_positions_arr - center, axis=1).max())
    radius = max(0.5, spread)
    n_seeds = 5_000
    rng = np.random.default_rng(42)
    # Uniform sampling inside a ball: random direction * (uniform^(1/3) * radius).
    directions = rng.normal(size=(n_seeds, 3))
    directions /= np.linalg.norm(directions, axis=1, keepdims=True)
    radii = rng.uniform(0.0, 1.0, size=n_seeds) ** (1.0 / 3.0) * radius
    seeds = center + directions * radii[:, None]
    colors = rng.integers(0, 256, size=(n_seeds, 3))
    p3d_lines = [
        "# 3D point list with one line of data per point:\n",
        "#   POINT3D_ID, X, Y, Z, R, G, B, ERROR, TRACK[] as (IMAGE_ID, POINT2D_IDX)\n",
    ]
    for i, (pt, color) in enumerate(zip(seeds, colors), start=1):
        p3d_lines.append(
            f"{i} {pt[0]} {pt[1]} {pt[2]} "
            f"{int(color[0])} {int(color[1])} {int(color[2])} 0\n"
        )
    (out / "points3D.txt").write_text("".join(p3d_lines))

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
    # Intentionally NO synthetic.json — train.py runs the real
    # ns-train splatfacto path against this workspace.

    log.info(
        "arcore: wrote COLMAP workspace (%d images, %d seed points)",
        written,
        n_seeds,
    )
    await progress(
        0.95,
        f"sfm: arcore poses → COLMAP ({written} frames, {n_seeds} seeds)",
    )
    return {
        "backend": "arcore_native",
        "synthetic": False,
        "frames": written,
        "seed_points": n_seeds,
    }


async def _arcore_synthetic_fallback(
    *, sfm_dir: Path, progress: ProgressCb, reason: str
) -> dict:
    """Used when the ARCore conversion can't proceed (missing
    poses.jsonl, no usable entries, etc). Drops a synthetic stub so
    train.py still completes and the user sees the failure mode in
    the job result + arcore_native.json marker.
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

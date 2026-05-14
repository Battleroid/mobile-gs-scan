"""Translator from nerfstudio ``transforms.json`` to a COLMAP text-
format workspace.

The standard-tier (OpenMVS) mesh pipeline needs a COLMAP-shape
input for ``InterfaceCOLMAP`` to consume. Our SfM step writes a
nerfstudio-native ``transforms.json`` (per-frame OpenGL camera-to-
world + shared intrinsics under either the OPENCV or PINHOLE camera
model). This module flips that into the three text files COLMAP /
OpenMVS read:

* ``cameras.txt`` — one row, the shared intrinsics.
* ``images.txt`` — one row per frame (world-from-camera quaternion +
  translation + image file basename) followed by a blank POINTS2D
  line (we have no 2D feature observations; OpenMVS densifies from
  scratch so the empty list is fine).
* ``points3D.txt`` — header only. We have no sparse 3D points from
  the ARCore-native SfM path; OpenMVS doesn't require them.

Axis convention: nerfstudio's ``transform_matrix`` is OpenGL c2w
(camera +Z = backward, +Y = up). COLMAP / OpenCV / OpenMVS use the
opposite axis-flip — camera +Z = forward, +Y = down. The conversion
is ``R_cv = R_opengl @ diag(1, -1, -1)`` followed by inverting to
get world-from-camera (what COLMAP serialises). The matrix-math is
verified by the round-trip test in ``tests/test_mesh_mvs.py``.

Pure Python + numpy. Import-safe from any test host.
"""
from __future__ import annotations

import json
from pathlib import Path

import numpy as np


def write_colmap_text(
    *,
    transforms_path: Path,
    images_src_dir: Path,
    out_dir: Path,
) -> dict:
    """Translate ``transforms_path`` into a COLMAP-text workspace
    at ``out_dir``.

    ``out_dir`` must be a writable directory; the writer creates
    ``sparse/cameras.txt``, ``sparse/images.txt``,
    ``sparse/points3D.txt`` (OpenMVS's ``InterfaceCOLMAP`` reads
    the model from ``<workspace>/sparse/``, not the workspace
    root) and an ``images/`` symlink tree at the workspace root
    containing each frame referenced by ``transforms.json``
    (resolved against ``images_src_dir``, which is typically the
    scene's ``sfm/images/`` symlink-to-capture-frames directory).

    Returns a dict ``{"n_cameras": int, "n_images": int}`` for the
    caller's log line.
    """
    data = json.loads(transforms_path.read_text())
    frames = data.get("frames") or []
    if not frames:
        raise ValueError(
            f"transforms.json at {transforms_path} has no frames",
        )

    width = int(data["w"])
    height = int(data["h"])
    fx = float(data["fl_x"])
    fy = float(data["fl_y"])
    cx = float(data["cx"])
    cy = float(data["cy"])

    # Pick the camera model line. transforms.json's
    # ``camera_model`` is "OPENCV" or "PINHOLE" in practice. We
    # emit PINHOLE when no distortion keys are present (ARCore
    # path), OPENCV otherwise. OpenMVS InterfaceCOLMAP accepts
    # both.
    has_distortion = any(
        k in data for k in ("k1", "k2", "p1", "p2")
    )
    if has_distortion:
        model = "OPENCV"
        k1 = float(data.get("k1", 0.0))
        k2 = float(data.get("k2", 0.0))
        p1 = float(data.get("p1", 0.0))
        p2 = float(data.get("p2", 0.0))
        params = f"{fx} {fy} {cx} {cy} {k1} {k2} {p1} {p2}"
    else:
        model = "PINHOLE"
        params = f"{fx} {fy} {cx} {cy}"

    out_dir.mkdir(parents=True, exist_ok=True)
    # COLMAP / OpenMVS expect the sparse model under a ``sparse/``
    # subdir of the workspace, not the workspace root. OpenMVS's
    # ``InterfaceCOLMAP`` probes ``<workspace>/sparse/cameras.txt``
    # (then ``.bin``) and exits 1 if neither resolves. Writing the
    # three model files directly under ``out_dir`` produced a
    # workspace that looked valid (the per-step log read "colmap
    # workspace: 1 cameras, N images") but the very next OpenMVS
    # invocation always failed with "unable to open file
    # '.../colmap/sparse/cameras.txt'".
    sparse_dir = out_dir / "sparse"
    sparse_dir.mkdir(exist_ok=True)
    images_link_dir = out_dir / "images"
    images_link_dir.mkdir(exist_ok=True)

    # cameras.txt — one row, camera id 1. COLMAP's text format
    # tolerates the leading "#" comments; we emit them for human-
    # debuggability since these files are sometimes hand-inspected.
    cam_lines = [
        "# Camera list with one line of data per camera:",
        "#   CAMERA_ID, MODEL, WIDTH, HEIGHT, PARAMS[]",
        "# Number of cameras: 1",
        f"1 {model} {width} {height} {params}",
    ]
    (sparse_dir / "cameras.txt").write_text("\n".join(cam_lines) + "\n")

    # images.txt — two text lines per image (pose + observations).
    img_lines = [
        "# Image list with two lines of data per image:",
        "#   IMAGE_ID, QW, QX, QY, QZ, TX, TY, TZ, CAMERA_ID, NAME",
        "#   POINTS2D[] as (X, Y, POINT3D_ID)",
        f"# Number of images: {len(frames)}",
    ]
    for image_id, frame in enumerate(frames, start=1):
        m = np.asarray(frame["transform_matrix"], dtype=np.float64).reshape(4, 4)
        # transforms.json uses OpenGL c2w; convert to OpenCV
        # camera-from-world. The ``diag(1, -1, -1)`` post-multiply
        # flips Y and Z in camera space (nerfstudio↔colmap axis
        # convention). Then invert to get w2c.
        c2w_cv = m @ np.diag([1.0, -1.0, -1.0, 1.0])
        w2c = np.linalg.inv(c2w_cv)
        R = w2c[:3, :3]
        t = w2c[:3, 3]
        qw, qx, qy, qz = _rotation_to_quaternion_wxyz(R)
        basename = Path(frame["file_path"]).name
        # Stage the frame under ``out_dir/images/`` so OpenMVS's
        # ``--image-folder`` finds it by basename, decoupled from
        # wherever the SfM step's ``images/`` symlink points.
        src = images_src_dir / basename
        link = images_link_dir / basename
        if not link.exists():
            try:
                link.symlink_to(src.resolve())
            except OSError:
                # Some filesystems (e.g. tmpfs without nodev) reject
                # symlinks; copy as a fallback. Frames are JPEGs in
                # the low-MB range, so the duplication is tolerable.
                link.write_bytes(src.read_bytes())
        img_lines.append(
            f"{image_id} {qw:.10f} {qx:.10f} {qy:.10f} {qz:.10f} "
            f"{t[0]:.10f} {t[1]:.10f} {t[2]:.10f} 1 {basename}"
        )
        # Empty observations line — OpenMVS doesn't require linked
        # 2D features.
        img_lines.append("")
    (sparse_dir / "images.txt").write_text("\n".join(img_lines) + "\n")

    # points3D.txt — header only.
    pts_lines = [
        "# 3D point list with one line of data per point:",
        "#   POINT3D_ID, X, Y, Z, R, G, B, ERROR, TRACK[] as (IMAGE_ID, POINT2D_IDX)",
        "# Number of points: 0",
    ]
    (sparse_dir / "points3D.txt").write_text("\n".join(pts_lines) + "\n")

    return {"n_cameras": 1, "n_images": len(frames)}


def _rotation_to_quaternion_wxyz(R: np.ndarray) -> tuple[float, float, float, float]:
    """Convert a 3×3 rotation matrix to a unit quaternion in
    (w, x, y, z) ordering. Uses Shepperd's method (the same
    numerically-stable algorithm scipy's
    ``Rotation.from_matrix.as_quat`` uses), expressed in pure numpy
    so this module doesn't need scipy as a worker dep.

    For valid rotation matrices the result is unit-norm to within
    float precision; the caller can sanity-check via
    ``np.linalg.norm`` if paranoid.
    """
    m00, m01, m02 = float(R[0, 0]), float(R[0, 1]), float(R[0, 2])
    m10, m11, m12 = float(R[1, 0]), float(R[1, 1]), float(R[1, 2])
    m20, m21, m22 = float(R[2, 0]), float(R[2, 1]), float(R[2, 2])
    trace = m00 + m11 + m22
    if trace > 0.0:
        s = 0.5 / np.sqrt(trace + 1.0)
        qw = 0.25 / s
        qx = (m21 - m12) * s
        qy = (m02 - m20) * s
        qz = (m10 - m01) * s
    elif m00 > m11 and m00 > m22:
        s = 2.0 * np.sqrt(1.0 + m00 - m11 - m22)
        qw = (m21 - m12) / s
        qx = 0.25 * s
        qy = (m01 + m10) / s
        qz = (m02 + m20) / s
    elif m11 > m22:
        s = 2.0 * np.sqrt(1.0 + m11 - m00 - m22)
        qw = (m02 - m20) / s
        qx = (m01 + m10) / s
        qy = 0.25 * s
        qz = (m12 + m21) / s
    else:
        s = 2.0 * np.sqrt(1.0 + m22 - m00 - m11)
        qw = (m10 - m01) / s
        qx = (m02 + m20) / s
        qy = (m12 + m21) / s
        qz = 0.25 * s
    # Normalise — Shepperd is numerically stable but the final
    # quaternion can drift by ~1e-7 from unit-norm on noisy R.
    n = float(np.sqrt(qw * qw + qx * qx + qy * qy + qz * qz))
    return (qw / n, qx / n, qy / n, qz / n)

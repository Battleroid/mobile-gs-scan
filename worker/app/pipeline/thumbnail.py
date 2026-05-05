"""Render a single PNG thumbnail of the trained splat.

Runs after export. The web home page's ``CaptureCard`` consumes
``Scene.thumb_url`` when present and falls back to a deterministic
gradient placeholder otherwise — so a missing thumbnail is a soft
failure: the scene still completes, the user just sees a placeholder
tile until they re-trigger or re-train.

Implementation: write a single-keyframe ``camera_path.json`` derived
from the splat's bounding box, then run ``ns-render camera-path``
against the trained nerfstudio config. The resulting PNG is staged
to ``<scene_dir>/thumb.png``.

We use ``ns-render``, not ``ns-export``, because:
  * ``ns-export poisson`` is documented broken on splatfacto-trained
    scenes (see worker/app/pipeline/mesh.py); ``ns-render camera-path``
    is the camera-rendering subcommand and works against the same
    splatfacto checkpoint.
  * It re-uses the same nerfstudio install the worker already has on
    PATH for training, so no extra dependency.

If ``ns-render`` is unavailable (synthetic stub training, missing
binary), we skip silently and return an empty result. The caller
(``runner._run_thumbnail``) treats the missing path as "no thumbnail
yet" — same as a brand-new capture.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import shutil
from pathlib import Path
from typing import Awaitable, Callable

from app.pipeline import _running
from app.pipeline._logtail import format_subprocess_error, tail_bytes

log = logging.getLogger(__name__)

ProgressCb = Callable[[float, str], Awaitable[None]]

# Output dimensions for the rendered thumbnail. 800×600 keeps the
# file under ~600 KB at PNG zlib defaults and matches the
# ``CaptureCard`` thumb container's 4:3-ish aspect (180 px tall on
# the web grid). Lossy is fine; this isn't a delivery artifact.
THUMB_W = 800
THUMB_H = 600
# fov + camera distance defaults. The bbox-fit logic below picks a
# distance based on the splat's actual extents, but we cap at this
# fov so a near-degenerate scene (single point at origin) doesn't
# zoom in past usefulness.
DEFAULT_FOV_DEG = 50.0


async def run_thumbnail(
    *,
    scene_dir: Path,
    src_ply: Path,
    progress: ProgressCb,
    job_id: str | None = None,
) -> dict:
    """Produce ``<scene_dir>/thumb.png`` from the trained splat.

    Returns ``{"thumbnail": <path>}`` on success, ``{}`` when the
    scene is a stub or ``ns-render`` isn't available.
    """
    train_dir = scene_dir / "train"
    if (train_dir / "synthetic.json").exists():
        log.info("thumbnail: stub scene, skipping render")
        await progress(1.0, "thumbnail: skipped (stub scene)")
        return {}
    if not shutil.which("ns-render"):
        log.info("thumbnail: ns-render not on PATH, skipping render")
        await progress(1.0, "thumbnail: skipped (ns-render unavailable)")
        return {}

    candidates = sorted(train_dir.rglob("config.yml"))
    if not candidates:
        log.warning("thumbnail: no nerfstudio config.yml under train/")
        return {}
    config = candidates[-1]

    if not src_ply.exists():
        log.warning("thumbnail: source .ply missing at %s", src_ply)
        return {}

    await progress(0.05, "thumbnail: computing camera")
    camera_to_world = _camera_for_ply(src_ply)

    # ns-render writes its outputs into <output_path>/. With
    # output-format images that's a directory of frame PNGs; with
    # video, an mp4. We use images and keep just the single frame.
    work_dir = scene_dir / "thumb_work"
    if work_dir.exists():
        shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    camera_path_file = work_dir / "camera_path.json"
    camera_path_file.write_text(_render_camera_path_json(camera_to_world))

    log_path = scene_dir / "thumbnail.log"
    cmd = [
        "ns-render", "camera-path",
        "--load-config", str(config),
        "--camera-path-filename", str(camera_path_file),
        "--output-path", str(work_dir / "frames"),
        "--output-format", "images",
        "--image-format", "png",
    ]
    log.info("thumbnail: %s", " ".join(cmd))

    await progress(0.1, "thumbnail: ns-render")
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    if job_id is not None:
        _running.register(job_id, proc)
    tail_limit_bytes = 64 * 1024
    tail = bytearray()
    try:
        try:
            with log_path.open("wb") as log_f:
                if proc.stdout:
                    while True:
                        chunk = await proc.stdout.read(8192)
                        if not chunk:
                            break
                        log_f.write(chunk)
                        tail.extend(chunk)
                        if len(tail) > tail_limit_bytes:
                            del tail[:-tail_limit_bytes]
            rc = await proc.wait()
        except BaseException:
            # Streaming failed (cancel, decode, ENOSPC, …). Reap
            # ns-render so it doesn't outlive us, then re-raise.
            if proc.returncode is None:
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                try:
                    await proc.wait()
                except BaseException:
                    pass
            raise
    finally:
        if job_id is not None:
            _running.unregister(job_id)

    if rc != 0:
        raise RuntimeError(
            format_subprocess_error("ns-render", rc, log_path, tail_bytes(bytes(tail)))
        )

    # ns-render's `images` output drops PNGs into <output_path>/ —
    # one per camera_path keyframe. Our path has one keyframe so we
    # expect exactly one frame; pick the first PNG that exists. The
    # exact subpath has varied across nerfstudio versions, so we
    # rglob to find any PNG under work_dir.
    rendered = next(iter(sorted(work_dir.rglob("*.png"))), None)
    if rendered is None:
        raise RuntimeError("ns-render produced no PNG output")

    thumb_path = scene_dir / "thumb.png"
    rendered.replace(thumb_path)
    shutil.rmtree(work_dir, ignore_errors=True)

    await progress(1.0, "thumbnail: done")
    return {"thumbnail": str(thumb_path)}


def _render_camera_path_json(camera_to_world: list[float]) -> str:
    """Serialize a single-keyframe nerfstudio camera_path JSON.

    Format mirrors what nerfstudio's web-app camera-path editor
    emits — only the ``camera_path`` list matters for ns-render
    (``keyframes`` is editor-only metadata). One keyframe → one
    frame rendered.
    """
    aspect = THUMB_W / THUMB_H
    return json.dumps(
        {
            "render_height": THUMB_H,
            "render_width": THUMB_W,
            "fps": 1,
            "seconds": 1,
            "smoothness_value": 0,
            "is_cycle": False,
            "crop": None,
            "camera_path": [
                {
                    "camera_to_world": camera_to_world,
                    "fov": DEFAULT_FOV_DEG,
                    "aspect": aspect,
                    "file_path": "frames/00000.png",
                }
            ],
            "keyframes": [],
            "camera_type": "perspective",
        },
        indent=2,
    )


def _camera_for_ply(src_ply: Path) -> list[float]:
    """Compute a flattened 4×4 camera_to_world matrix that frames
    the splat from a fixed three-quarter angle.

    Reads gaussian positions from the .ply (only the x/y/z
    properties — fast, no need to materialise the full splatfacto
    record), takes the 5th–95th percentile bounding box (so a
    handful of stray gaussians don't pull the camera way out), and
    places the camera at ``centroid + offset`` looking back at
    ``centroid``. Matrix layout follows nerfstudio / OpenGL: column
    0 = right, column 1 = up, column 2 = -forward, column 3 =
    position. Returned as a flat 16-float list (row-major) ready to
    drop into the camera_path JSON.

    Falls back to a sane default at origin if anything in the .ply
    parse goes sideways — a blank frame is preferable to crashing
    the whole job for the UI's optional thumbnail.
    """
    try:
        center, extent = _ply_bbox(src_ply)
    except Exception:
        log.warning("thumbnail: ply bbox parse failed; using fallback camera")
        center = (0.0, 0.0, 0.0)
        extent = 1.0

    cx, cy, cz = center
    # Distance: enough to see the whole bbox + a margin, with a
    # floor so single-point or tiny-extent scenes still frame
    # something rather than zooming into the gaussian's interior.
    fov_rad = math.radians(DEFAULT_FOV_DEG)
    fit_distance = (extent / max(0.01, math.tan(fov_rad * 0.5))) * 1.4
    distance = max(fit_distance, extent * 2.0, 1.5)

    # Three-quarter view: pull the camera back along +Z, slightly
    # up along +Y. Splatfacto poses tend to put +Y as up so this
    # reads as a comfortable "look-down-at" angle most of the time.
    eye = (cx, cy + distance * 0.35, cz + distance)
    target = (cx, cy, cz)
    return _look_at(eye, target, up=(0.0, 1.0, 0.0))


def _ply_bbox(src_ply: Path) -> tuple[tuple[float, float, float], float]:
    """Return (centroid, max half-extent) of the .ply positions.

    Uses plyfile (already a worker dep for the filter pipeline) so
    we don't pull in another reader. The 5th/95th-percentile bbox
    drops outlier gaussians that often sit far from the subject in
    splatfacto outputs — without it, a single floater can make the
    camera frame the empty space around the actual scene instead.
    """
    from plyfile import PlyData
    import numpy as np

    data = PlyData.read(str(src_ply))
    v = data["vertex"]
    xs = np.asarray(v["x"], dtype=np.float32)
    ys = np.asarray(v["y"], dtype=np.float32)
    zs = np.asarray(v["z"], dtype=np.float32)
    if xs.size == 0:
        return (0.0, 0.0, 0.0), 1.0
    pts = np.stack([xs, ys, zs], axis=-1)
    lo = np.percentile(pts, 5, axis=0)
    hi = np.percentile(pts, 95, axis=0)
    cx, cy, cz = ((lo + hi) * 0.5).tolist()
    half = float(((hi - lo) * 0.5).max())
    return (float(cx), float(cy), float(cz)), max(half, 0.1)


def _look_at(
    eye: tuple[float, float, float],
    target: tuple[float, float, float],
    *,
    up: tuple[float, float, float],
) -> list[float]:
    """Standard look-at producing a row-major 4×4 nerfstudio camera
    pose. nerfstudio (OpenGL convention) puts -Z forward, +Y up,
    +X right in camera space; the third column of camera_to_world
    is therefore -forward."""
    import numpy as np

    eye_a = np.array(eye, dtype=np.float64)
    tgt_a = np.array(target, dtype=np.float64)
    up_a = np.array(up, dtype=np.float64)

    forward = tgt_a - eye_a
    fnorm = float(np.linalg.norm(forward))
    if fnorm < 1e-6:
        forward = np.array([0.0, 0.0, -1.0])
    else:
        forward = forward / fnorm

    right = np.cross(forward, up_a)
    rnorm = float(np.linalg.norm(right))
    if rnorm < 1e-6:
        # eye and up are colinear; nudge the up direction.
        right = np.cross(forward, np.array([0.0, 0.0, 1.0]))
        rnorm = float(np.linalg.norm(right))
    right = right / rnorm

    cam_up = np.cross(right, forward)

    # Row-major flatten, OpenGL convention (negate forward to get -Z fwd).
    m = np.eye(4, dtype=np.float64)
    m[:3, 0] = right
    m[:3, 1] = cam_up
    m[:3, 2] = -forward
    m[:3, 3] = eye_a
    return [float(x) for x in m.flatten().tolist()]

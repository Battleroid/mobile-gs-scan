"""Render a 24-frame MP4 orbit of the trained splat.

Two-stage thumbnail companion: the still PNG produced by
[run_thumbnail] lands first (cheap render, flips the scene to
``completed``) and this step backfills the richer motion variant
asynchronously. The web home grid's ``CaptureCard`` prefers the
orbit MP4 when present, falls back to the PNG, then to the
gradient placeholder.

Implementation: build a 24-keyframe ``camera_path.json`` whose
camera circles ``centroid`` at a bbox-fit radius, run
``ns-render camera-path`` to produce 24 PNGs, then pipe them
through ``ffmpeg`` to ``orbit.mp4``. The PNG-then-ffmpeg pipeline
keeps the orbit step using the same nerfstudio bits that already
power the still PNG — no new renderer to maintain. ``libx264 +
yuv420p + faststart`` gives a `<video>`-element-friendly MP4
that streams progressively over the home grid's small thumbnail
tile.

Same soft-failure semantics as ``run_thumbnail``: if ``ns-render``
or ``ffmpeg`` are missing on PATH we skip silently. The orbit
job's failure does NOT demote the scene; the PNG already did the
finalize.
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
from app.pipeline.thumbnail import _camera_for_ply, _look_at, _ply_bbox  # noqa: F401

log = logging.getLogger(__name__)

ProgressCb = Callable[[float, str], Awaitable[None]]

# Render dimensions. 480x320 keeps each MP4 in the ~200-500 KB band
# at CRF 28 — small enough to stream a dozen capture cards' orbits
# on a busy home grid without saturating LAN, big enough to look
# like more than a postage stamp at 180 px tall in the grid layout.
ORBIT_W = 480
ORBIT_H = 320
# Frame budget: 24 frames at 16 fps gives a 1.5 s loop. Slow enough
# that ns-render's per-frame cost stays bounded, fast enough that
# the orbit reads as motion rather than a slide-show. The video tag
# loops natively so the last frame transitions cleanly into the
# first (the orbit is a closed circle).
ORBIT_FRAMES = 24
ORBIT_FPS = 16
ORBIT_SECONDS = ORBIT_FRAMES / ORBIT_FPS  # 1.5
DEFAULT_FOV_DEG = 50.0


async def run_orbit(
    *,
    scene_dir: Path,
    src_ply: Path,
    progress: ProgressCb,
    job_id: str | None = None,
) -> dict:
    """Produce ``<scene_dir>/orbit.mp4`` from the trained splat.

    Returns one of three result shapes — mirrors [run_thumbnail]'s
    contract so the runner-side soft-failure logic is identical:
    * ``{"orbit": <path>}`` on a successful render.
    * ``{"permanent_skip": "<reason>"}`` when the scene structurally
      can't render on this host: stub training, ns-render / ffmpeg
      missing, or no nerfstudio config under ``train/``.
    * ``{}`` when the source ``.ply`` is missing — transient skip,
      eligible for a backfill retry once export catches up.
    """
    train_dir = scene_dir / "train"
    if (train_dir / "synthetic.json").exists():
        log.info("orbit: stub scene, skipping render")
        await progress(1.0, "orbit: skipped (stub scene)")
        return {"permanent_skip": "synthetic stub scene"}
    if not shutil.which("ns-render"):
        log.info("orbit: ns-render not on PATH, skipping render")
        await progress(1.0, "orbit: skipped (ns-render unavailable)")
        return {"permanent_skip": "ns-render unavailable"}
    if not shutil.which("ffmpeg"):
        # ffmpeg is in Dockerfile.base; if it's gone the dev env is
        # broken in a way that needs operator attention, but don't
        # take down the rest of the pipeline over a missing optional.
        log.info("orbit: ffmpeg not on PATH, skipping render")
        await progress(1.0, "orbit: skipped (ffmpeg unavailable)")
        return {"permanent_skip": "ffmpeg unavailable"}

    candidates = sorted(train_dir.rglob("config.yml"))
    if not candidates:
        log.warning("orbit: no nerfstudio config.yml under train/")
        return {"permanent_skip": "no nerfstudio config under train/"}
    config = candidates[-1]

    if not src_ply.exists():
        log.warning("orbit: source .ply missing at %s", src_ply)
        return {}

    await progress(0.05, "orbit: computing camera path")
    camera_to_worlds = _orbit_camera_path(src_ply)

    work_dir = scene_dir / "orbit_work"
    if work_dir.exists():
        shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True, exist_ok=True)

    camera_path_file = work_dir / "camera_path.json"
    camera_path_file.write_text(_orbit_camera_path_json(camera_to_worlds))

    log_path = scene_dir / "orbit.log"
    frames_dir = work_dir / "frames"
    cmd = [
        "ns-render", "camera-path",
        "--load-config", str(config),
        "--camera-path-filename", str(camera_path_file),
        "--output-path", str(frames_dir),
        "--output-format", "images",
        "--image-format", "png",
    ]
    log.info("orbit: %s", " ".join(cmd))

    await progress(0.1, "orbit: ns-render (rendering frames)")
    rc, tail = await _stream_subprocess(cmd, log_path, job_id)
    if rc != 0:
        raise RuntimeError(
            format_subprocess_error("ns-render", rc, log_path, tail_bytes(tail))
        )

    # ns-render's `images` output drops PNGs into <output_path>/.
    # nerfstudio's exact subpath has varied across versions, so glob
    # under work_dir and sort by name. The camera_path's `file_path`
    # entries below pin the filenames to ``frames/<i:05d>.png``.
    pngs = sorted(work_dir.rglob("*.png"))
    if len(pngs) < ORBIT_FRAMES:
        raise RuntimeError(
            f"ns-render produced {len(pngs)} PNGs; expected {ORBIT_FRAMES}"
        )

    # Renormalize filenames into work_dir/seq/00000.png .. so ffmpeg's
    # `%05d.png` glob matches no matter what subdir nerfstudio used.
    seq_dir = work_dir / "seq"
    seq_dir.mkdir(parents=True, exist_ok=True)
    for i, png in enumerate(pngs[:ORBIT_FRAMES]):
        png.replace(seq_dir / f"{i:05d}.png")

    await progress(0.85, "orbit: ffmpeg (encoding MP4)")
    mp4_path = work_dir / "orbit.mp4"
    ffmpeg_cmd = [
        "ffmpeg", "-y",
        "-framerate", str(ORBIT_FPS),
        "-i", str(seq_dir / "%05d.png"),
        # yuv420p for max <video>-element compatibility (Safari is
        # the strict one). +faststart pushes the moov atom to the
        # start of the file so the browser can begin decoding while
        # still downloading the tail — important for autoplay on
        # the home grid.
        "-vf", "format=yuv420p",
        "-c:v", "libx264",
        "-preset", "medium",
        "-crf", "28",
        "-movflags", "+faststart",
        # No audio track — `<video>` autoplay rules treat
        # silent + muted videos identically, but skipping the audio
        # stream keeps the file ~5-10% smaller and the encoder cost
        # marginally lower.
        "-an",
        str(mp4_path),
    ]
    log.info("orbit: %s", " ".join(ffmpeg_cmd))
    rc, tail = await _stream_subprocess(ffmpeg_cmd, log_path, job_id, append=True)
    if rc != 0:
        raise RuntimeError(
            format_subprocess_error("ffmpeg", rc, log_path, tail_bytes(tail))
        )

    out_path = scene_dir / "orbit.mp4"
    mp4_path.replace(out_path)
    shutil.rmtree(work_dir, ignore_errors=True)

    await progress(1.0, "orbit: done")
    return {"orbit": str(out_path)}


async def _stream_subprocess(
    cmd: list[str],
    log_path: Path,
    job_id: str | None,
    *,
    append: bool = False,
) -> tuple[int, bytes]:
    """Spawn ``cmd``, stream stdout+stderr to ``log_path``, keep the
    last 64 KiB in memory for error reporting. Returns (returncode,
    tail-bytes). Mirrors the streaming dance in thumbnail.py — same
    shape, just factored out so the two ns-render / ffmpeg passes
    here don't duplicate it.
    """
    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    if job_id is not None:
        _running.register(job_id, proc)
    tail_limit_bytes = 64 * 1024
    tail = bytearray()
    mode = "ab" if append else "wb"
    try:
        try:
            with log_path.open(mode) as log_f:
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
    return rc, bytes(tail)


def _orbit_camera_path(src_ply: Path) -> list[list[float]]:
    """24 camera_to_world matrices, evenly distributed in a circle
    around the splat's centroid. Camera tilt + radius match the
    still PNG's three-quarter angle so the orbit reads as the same
    framing in motion.
    """
    try:
        center, extent = _ply_bbox(src_ply)
    except Exception:
        log.warning("orbit: ply bbox parse failed; using fallback camera")
        center = (0.0, 0.0, 0.0)
        extent = 1.0

    cx, cy, cz = center
    fov_rad = math.radians(DEFAULT_FOV_DEG)
    fit_distance = (extent / max(0.01, math.tan(fov_rad * 0.5))) * 1.4
    distance = max(fit_distance, extent * 2.0, 1.5)

    # Camera circles at constant radius around the centroid in the
    # XZ plane, lifted slightly along +Y to match the still's
    # three-quarter "look-down-at" tilt. 24 frames @ 360° = 15° per
    # frame; frame 24 is identical to frame 0 modulo wrap, so the
    # <video> loop transition is seamless.
    poses: list[list[float]] = []
    for i in range(ORBIT_FRAMES):
        theta = 2.0 * math.pi * i / ORBIT_FRAMES
        eye = (
            cx + distance * math.sin(theta),
            cy + distance * 0.35,
            cz + distance * math.cos(theta),
        )
        target = (cx, cy, cz)
        poses.append(_look_at(eye, target, up=(0.0, 1.0, 0.0)))
    return poses


def _orbit_camera_path_json(camera_to_worlds: list[list[float]]) -> str:
    """Serialize an N-keyframe nerfstudio camera_path JSON. Format
    mirrors the thumbnail step's single-frame shape; the only
    difference is N camera_path entries instead of one. Each entry
    pins a deterministic ``file_path`` so the post-render glob can
    rename them into a ffmpeg-friendly ``%05d.png`` sequence.
    """
    aspect = ORBIT_W / ORBIT_H
    return json.dumps(
        {
            "render_height": ORBIT_H,
            "render_width": ORBIT_W,
            "fps": ORBIT_FPS,
            "seconds": ORBIT_SECONDS,
            "smoothness_value": 0,
            # is_cycle hints that the path loops, which nerfstudio
            # uses for smoothness-interpolation if smoothness_value
            # > 0. We set smoothness=0 so it's a no-op either way,
            # but keep is_cycle=true for documentation.
            "is_cycle": True,
            "crop": None,
            "camera_path": [
                {
                    "camera_to_world": pose,
                    "fov": DEFAULT_FOV_DEG,
                    "aspect": aspect,
                    "file_path": f"frames/{i:05d}.png",
                }
                for i, pose in enumerate(camera_to_worlds)
            ],
            "keyframes": [],
            "camera_type": "perspective",
        },
        indent=2,
    )

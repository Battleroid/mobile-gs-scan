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
# Frame budget: 48 frames at 16 fps gives a 3.0 s loop. Slow
# enough that the viewer can actually parse the scene as it
# rotates (the original 24-frame / 1.5 s variant felt strobe-y on
# anything denser than a single hero subject), fast enough that
# the orbit reads as motion rather than a slide-show. 16 fps
# kept — matches typical phone-display refresh divisors and the
# H.264 encoder's GOP defaults. Doubled frame count → roughly
# doubled render cost per scene; still bounded at a few minutes
# even on the heaviest splats, and it's a backfill so the home
# grid's first paint isn't gated on it.
ORBIT_FRAMES = 48
ORBIT_FPS = 16
ORBIT_SECONDS = ORBIT_FRAMES / ORBIT_FPS  # 3.0
DEFAULT_FOV_DEG = 50.0


async def run_orbit(
    *,
    scene_dir: Path,
    src_ply: Path,
    progress: ProgressCb,
    job_id: str | None = None,
    use_ply_renderer: bool = False,
) -> dict:
    """Produce ``<scene_dir>/orbit.mp4`` from the trained splat.

    Two renderer paths — mirrors run_thumbnail:
    * ``use_ply_renderer=False`` (default): ns-render against the
      splatfacto checkpoint to produce the PNG frames, then ffmpeg.
    * ``use_ply_renderer=True`` (filter-triggered regen): gsplat
      rasterizer reads the (edited) PLY directly to produce frames,
      then ffmpeg. Necessary because ns-render reads the checkpoint
      which the filter doesn't touch.

    Returns mirror run_thumbnail's contract:
    * ``{"orbit": <path>}`` on success.
    * ``{"permanent_skip": "<reason>"}`` for structural inability
      to render (stub training, missing renderer toolchain).
    * ``{}`` when the source PLY is missing — transient.
    """
    train_dir = scene_dir / "train"
    if (train_dir / "synthetic.json").exists():
        log.info("orbit: stub scene, skipping render")
        await progress(1.0, "orbit: skipped (stub scene)")
        return {"permanent_skip": "synthetic stub scene"}
    if not shutil.which("ffmpeg"):
        # ffmpeg is required for both paths; it does the frames →
        # MP4 encode step at the end.
        log.info("orbit: ffmpeg not on PATH, skipping render")
        await progress(1.0, "orbit: skipped (ffmpeg unavailable)")
        return {"permanent_skip": "ffmpeg unavailable"}
    if not src_ply.exists():
        log.warning("orbit: source .ply missing at %s", src_ply)
        return {}

    work_dir = scene_dir / "orbit_work"
    if work_dir.exists():
        shutil.rmtree(work_dir, ignore_errors=True)
    work_dir.mkdir(parents=True, exist_ok=True)
    log_path = scene_dir / "orbit.log"
    seq_dir = work_dir / "seq"
    seq_dir.mkdir(parents=True, exist_ok=True)

    if use_ply_renderer:
        # Filter-regen path. gsplat reads the (edited) PLY directly.
        from app.pipeline import ply_render
        from PIL import Image

        ok, reason = ply_render.is_available()
        if not ok:
            shutil.rmtree(work_dir, ignore_errors=True)
            log.info("orbit: ply_render unavailable (%s)", reason)
            await progress(1.0, f"orbit: skipped ({reason})")
            return {"permanent_skip": reason}

        await progress(0.1, "orbit: ply-render (rasterizing frames)")
        c2ws = _orbit_camera_path(src_ply)
        frames = ply_render.render_frames(
            ply_path=src_ply,
            c2ws_opengl=c2ws,
            fov_deg=DEFAULT_FOV_DEG,
            width=ORBIT_W,
            height=ORBIT_H,
        )
        if len(frames) < ORBIT_FRAMES:
            raise RuntimeError(
                f"ply_render produced {len(frames)} frames; expected {ORBIT_FRAMES}"
            )
        await progress(0.65, "orbit: writing frame sequence")
        for i, frame in enumerate(frames[:ORBIT_FRAMES]):
            Image.fromarray(frame).save(
                seq_dir / f"{i:05d}.png", format="PNG", optimize=False,
            )
    else:
        # Default ns-render path (against splatfacto checkpoint).
        if not shutil.which("ns-render"):
            log.info("orbit: ns-render not on PATH, skipping render")
            shutil.rmtree(work_dir, ignore_errors=True)
            await progress(1.0, "orbit: skipped (ns-render unavailable)")
            return {"permanent_skip": "ns-render unavailable"}

        candidates = sorted(train_dir.rglob("config.yml"))
        if not candidates:
            log.warning("orbit: no nerfstudio config.yml under train/")
            shutil.rmtree(work_dir, ignore_errors=True)
            return {"permanent_skip": "no nerfstudio config under train/"}
        config = candidates[-1]

        await progress(0.05, "orbit: computing camera path")
        camera_to_worlds = _orbit_camera_path(src_ply)

        camera_path_file = work_dir / "camera_path.json"
        camera_path_file.write_text(_orbit_camera_path_json(camera_to_worlds))

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

        # Renormalize filenames into seq_dir so ffmpeg's `%05d.png`
        # glob matches no matter what subdir nerfstudio used.
        pngs = sorted(work_dir.rglob("*.png"))
        if len(pngs) < ORBIT_FRAMES:
            raise RuntimeError(
                f"ns-render produced {len(pngs)} PNGs; expected {ORBIT_FRAMES}"
            )
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

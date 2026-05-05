"""Frame extraction step.

Pipeline stage that runs *before* SfM when the capture's source is a
video. ffprobe + ffmpeg in a child process; the parent registers it
with ``_running`` so the heartbeat can SIGKILL on cancel like every
other step.

For image-set captures (``capture_dir/frames/`` already populated by
the upload route, no ``capture_dir/source/<video>`` present) this
step is a no-op success — the dispatcher still enqueues it so the
pipeline-list UI is shape-stable, but ``run_extract`` returns
immediately.

Why a dedicated pipeline step rather than extracting inline at
upload time:
  * The web UI gets to render live ffmpeg progress (frame N/M)
    via the same ``scene.<job>_progress`` event channel SfM /
    train / export use.
  * Cancellation works (kill the ffmpeg subprocess on
    user-requested cancel — same machinery as ns-train / glomap).
  * The HTTP upload request returns quickly; long extractions
    don't block the client.
"""
from __future__ import annotations

import asyncio
import logging
import re
from pathlib import Path
from typing import Awaitable, Callable

from app.pipeline import _running
from app.pipeline._logtail import format_subprocess_error, tail_file

log = logging.getLogger(__name__)

ProgressCb = Callable[[float, str], Awaitable[None]]

# Allowed source-video extensions. Match the set the api layer
# accepts in upload_to_capture so the two stay in sync.
VIDEO_SUFFIXES = (".mp4", ".mov", ".webm", ".mkv")

# Default fps when meta omits one. Conservative — phone captures of
# room-scale scenes are usable at 5–10 fps.
DEFAULT_EXTRACT_FPS = 8.0
# Default jpeg quality when meta omits one. Mapped to ffmpeg -q:v;
# 90 corresponds to q=2 in our translation below.
DEFAULT_JPEG_QUALITY = 90

# ffmpeg's `-progress pipe:1` writes plain `key=value` lines. We
# only care about ``frame=N`` and ``progress=end`` for done.
_PROGRESS_RE = re.compile(rb"frame=(\d+)")


async def run_extract(
    *,
    capture_dir: Path,
    params: dict,
    progress: ProgressCb,
    job_id: str | None = None,
) -> dict:
    """Extract frames from ``capture_dir/source/<video>`` into
    ``capture_dir/frames/``. Returns a result dict with the frame
    count + a ``stub`` flag when no video is present.
    """
    frames_dir = capture_dir / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)

    src = _find_video(capture_dir / "source")
    if src is None:
        # Image-set capture; nothing to extract. Still emit a
        # progress tick so the UI shows a green row.
        await progress(1.0, "extract: no video, skipped")
        return {"stub": True, "reason": "no source video"}

    requested_fps = float(params.get("extract_fps", DEFAULT_EXTRACT_FPS))
    requested_q = int(params.get("jpeg_quality", DEFAULT_JPEG_QUALITY))

    await progress(0.02, "extract: probe source")
    source_fps, source_total = await _probe(src)
    fps = min(requested_fps, source_fps) if source_fps > 0 else requested_fps
    fps = max(0.1, fps)
    qv = _quality_to_qv(requested_q)
    # Estimate the post-extraction frame count for a meaningful
    # percentage. ffprobe's nb_frames is the source's count; what we
    # actually emit is roughly source_total * (fps / source_fps).
    expected_total: int | None = None
    if source_total > 0 and source_fps > 0:
        expected_total = max(1, int(round(source_total * fps / source_fps)))

    log_path = capture_dir / "extract.log"
    log_path.write_text("")

    cmd = [
        "ffmpeg", "-y", "-loglevel", "error",
        "-i", str(src),
        "-vf", f"fps={fps:g}",
        "-q:v", str(qv),
        "-start_number", "0",
        str(frames_dir / "%06d.jpg"),
        "-progress", "pipe:1",
        "-nostats",
    ]

    await progress(0.05, f"extract: ffmpeg fps={fps:g}")

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    if job_id is not None:
        _running.register(job_id, proc)

    try:
        last_pct = 0.05
        with log_path.open("wb") as logf:
            assert proc.stdout is not None
            async for raw in proc.stdout:
                logf.write(raw)
                m = _PROGRESS_RE.search(raw)
                if not m:
                    continue
                try:
                    n = int(m.group(1))
                except ValueError:
                    continue
                if expected_total:
                    pct = max(0.05, min(0.99, 0.05 + 0.94 * (n / expected_total)))
                    label = f"extract: frame {n}/{expected_total}"
                else:
                    # Without a total we can't render a true %; surface
                    # the running count so the user still sees motion.
                    pct = min(0.95, last_pct + 0.01)
                    label = f"extract: frame {n}"
                if pct - last_pct >= 0.01:
                    await progress(pct, label)
                    last_pct = pct
        rc = await proc.wait()
    finally:
        if job_id is not None:
            _running.unregister(job_id)

    if rc != 0:
        tail = tail_file(log_path)
        raise RuntimeError(format_subprocess_error("ffmpeg", rc, log_path, tail))

    n_frames = sum(1 for _ in frames_dir.glob("*.jpg"))
    if n_frames == 0:
        raise RuntimeError("ffmpeg succeeded but produced no frames")

    await progress(1.0, f"extract: {n_frames} frames")
    return {"frames": n_frames, "fps": fps, "jpeg_quality": requested_q}


def _find_video(source_dir: Path) -> Path | None:
    if not source_dir.exists():
        return None
    for child in sorted(source_dir.iterdir()):
        if child.is_file() and child.suffix.lower() in VIDEO_SUFFIXES:
            return child
    return None


async def _probe(src: Path) -> tuple[float, int]:
    """Return (fps, total_frames) from ffprobe; (0.0, 0) on failure.

    ffprobe's r_frame_rate is a "<num>/<den>" rational. nb_frames is
    sometimes empty for streams without indexed counts; the worst
    case is "no progress percentage shown" — extraction still runs.
    """
    cmd = [
        "ffprobe", "-v", "error",
        "-select_streams", "v:0",
        "-show_entries", "stream=r_frame_rate,nb_frames",
        "-of", "default=noprint_wrappers=1:nokey=1",
        str(src),
    ]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, _ = await proc.communicate()
        if proc.returncode != 0:
            return (0.0, 0)
        text = out.decode("utf-8", errors="replace").strip().splitlines()
        fps_str = text[0] if len(text) > 0 else ""
        nb_str = text[1] if len(text) > 1 else ""
        fps = _parse_rational(fps_str)
        try:
            total = int(nb_str)
        except ValueError:
            total = 0
        return (fps, total)
    except Exception:  # noqa: BLE001
        return (0.0, 0)


def _parse_rational(s: str) -> float:
    if not s or "/" not in s:
        try:
            return float(s)
        except ValueError:
            return 0.0
    num, _, den = s.partition("/")
    try:
        n, d = float(num), float(den)
        return n / d if d else 0.0
    except ValueError:
        return 0.0


def _quality_to_qv(q: int) -> int:
    """Map a 1–100 user quality slider to ffmpeg's -q:v (2..31, lower
    is better). 100 → 2, 1 → 31. Linear interpolation."""
    q = max(1, min(100, int(q)))
    qv = round(31 - (q - 1) * 29 / 99)
    return max(2, min(31, qv))

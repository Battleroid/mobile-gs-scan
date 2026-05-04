"""3D Gaussian Splatting training step.

Wraps Nerfstudio's `ns-train splatfacto` when nerfstudio + gsplat are
installed. Falls back to a synthetic checkpoint when they aren't —
which keeps the scaffold runnable on a host without the CUDA stack
fully wired up. The export step understands both shapes.
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from pathlib import Path
from typing import Awaitable, Callable

from app.config import get_settings
from app.pipeline import _running
from app.pipeline._logtail import format_subprocess_error, tail_file

log = logging.getLogger(__name__)

ProgressCb = Callable[[float, str], Awaitable[None]]

PROGRESS_RE = re.compile(rb"(\d+)\s+\((\d+(?:\.\d+)?)%\)")
ITER_RE = re.compile(rb"\biter\s+(\d+)", re.IGNORECASE)
LATEST_CONFIG_MARKER = "latest_config.txt"


async def run_train(
    *,
    scene_dir: Path,
    iters: int,
    progress: ProgressCb,
    job_id: str | None = None,
) -> dict:
    train_dir = scene_dir / "train"
    train_dir.mkdir(parents=True, exist_ok=True)

    sfm_dir = scene_dir / "sfm"
    if (sfm_dir / "synthetic.json").exists():
        return await _run_stub(
            train_dir=train_dir,
            iters=iters,
            progress=progress,
            reason="sfm step produced no real reconstruction",
        )
    if not shutil.which("ns-train"):
        return await _run_stub(
            train_dir=train_dir,
            iters=iters,
            progress=progress,
            reason="ns-train not on PATH (nerfstudio not installed in worker image)",
        )

    return await _run_splatfacto(
        scene_dir=scene_dir,
        train_dir=train_dir,
        iters=iters,
        progress=progress,
        job_id=job_id,
    )


async def _run_splatfacto(
    *,
    scene_dir: Path,
    train_dir: Path,
    iters: int,
    progress: ProgressCb,
    job_id: str | None,
) -> dict:
    sfm_dir = scene_dir / "sfm"
    settings = get_settings()

    cmd = [
        "ns-train", "splatfacto",
        "--data", str(sfm_dir),
        "--max-num-iterations", str(iters),
        "--output-dir", str(train_dir),
        "--vis", "tensorboard",
        "--viewer.quit-on-train-completion", "True",
        # Force splatfacto's image cache to GPU (or whatever the
        # operator configured). Default in nerfstudio is ``gpu`` but
        # FullImagesDataManager auto-falls-back to ``cpu`` for
        # datasets > ~500 images, which costs a chunk of step time
        # to slow CPU → GPU copies. With a 24GB+ card we usually
        # have headroom; if this OOMs, set GS_TRAIN_CACHE_IMAGES=cpu
        # in env.
        "--pipeline.datamanager.cache-images",
        settings.train_cache_images,
    ]

    await progress(0.0, f"train: ns-train splatfacto ({iters} iters)")
    log_path = train_dir / "train.log"

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

                pct: float | None = None
                label: str | None = None

                pm = PROGRESS_RE.search(raw)
                if pm:
                    try:
                        current = int(pm.group(1))
                        percent = float(pm.group(2))
                    except ValueError:
                        current = None
                        percent = None
                    if percent is not None:
                        pct = max(0.0, min(0.99, percent / 100.0))
                        label = f"train: iter {current}/{iters} ({percent:.1f}%)"
                else:
                    im = ITER_RE.search(raw)
                    if im:
                        try:
                            current = int(im.group(1))
                        except (IndexError, ValueError):
                            current = None
                        if current is not None:
                            pct = max(0.0, min(0.99, current / max(iters, 1)))
                            label = f"train: iter {current}/{iters}"

                if pct is None:
                    continue

                if pct - last_pct >= 0.01:
                    await progress(pct, label or f"train: {int(pct * 100)}%")
                    last_pct = pct

        rc = await proc.wait()
    finally:
        if job_id is not None:
            _running.unregister(job_id)

    if rc != 0:
        tail = tail_file(log_path)
        raise RuntimeError(
            format_subprocess_error("ns-train", rc, log_path, tail)
        )

    config = _find_latest_config(train_dir)
    _write_latest_config_marker(scene_dir, train_dir, config)
    await progress(1.0, "train: done")
    return {"config": str(config) if config else None, "iters": iters}


def _find_latest_config(train_dir: Path) -> Path | None:
    candidates = sorted(train_dir.rglob("config.yml"))
    return candidates[-1] if candidates else None


def _write_latest_config_marker(
    scene_dir: Path, train_dir: Path, config: Path | None
) -> None:
    # Best-effort cache. Caller (a successful ns-train run, or an
    # export's fallback path) has already done the real work; a write
    # failure here (read-only train_dir, full disk, transient I/O)
    # would otherwise turn cache bookkeeping into a hard job failure.
    # On write failure we ALSO try to delete any pre-existing marker
    # so the next export doesn't trust a stale pointer to the prior
    # run's config — export's _load_latest_config has its own mtime
    # safety net, but this is the cheaper first line of defense.
    marker_path = train_dir / LATEST_CONFIG_MARKER
    try:
        if config is None:
            marker_path.unlink(missing_ok=True)
            return
        try:
            config_path = config.relative_to(scene_dir)
        except ValueError:
            config_path = config
        marker_path.write_text(f"{config_path}\n")
    except OSError as exc:
        log.warning(
            "could not update %s under %s: %s; skipping marker cache",
            LATEST_CONFIG_MARKER, train_dir, exc,
        )
        try:
            marker_path.unlink(missing_ok=True)
        except OSError:
            log.warning(
                "could not remove stale %s either; export will rely on its "
                "own mtime check",
                marker_path,
            )


async def _run_stub(
    *,
    train_dir: Path,
    iters: int,
    progress: ProgressCb,
    reason: str = "stub",
) -> dict:
    """Synthetic checkpoint. Walks the progress bar so the UI animates.

    `reason` is surfaced in both the progress message and the result
    blob so anyone debugging a stub run can see WHY it stubbed (sfm
    didn't produce real data vs. ns-train binary missing) without
    grepping container logs.
    """
    await progress(0.0, f"train: synthetic ({reason})")
    steps = 20
    for i in range(1, steps + 1):
        await asyncio.sleep(0.2)
        await progress(i / steps, f"train: synthetic step {i}/{steps}")
    marker = train_dir / "synthetic.json"
    marker.write_text(json.dumps({"iters": iters, "stub": True, "reason": reason}))
    return {"stub": True, "iters": iters, "reason": reason}

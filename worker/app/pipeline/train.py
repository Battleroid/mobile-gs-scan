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

from app.pipeline import _running
from app.pipeline._logtail import format_subprocess_error, tail_file

log = logging.getLogger(__name__)

ProgressCb = Callable[[float, str], Awaitable[None]]

ITER_RE = re.compile(rb"\biter\s+(\d+)", re.IGNORECASE)


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
    cmd = [
        "ns-train", "splatfacto",
        "--data", str(sfm_dir),
        "--max-num-iterations", str(iters),
        "--output-dir", str(train_dir),
        "--vis", "tensorboard",
        "--viewer.quit-on-train-completion", "True",
    ]

    await progress(0.0, f"train: ns-train splatfacto ({iters} iters)")
    log_path = train_dir / "train.log"

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    # Register so the worker heartbeat can SIGKILL us on cancel.
    if job_id is not None:
        _running.register(job_id, proc)
    try:
        last_pct = 0.0
        with log_path.open("wb") as logf:
            assert proc.stdout is not None
            async for raw in proc.stdout:
                logf.write(raw)
                m = ITER_RE.search(raw)
                if not m:
                    continue
                try:
                    current = int(m.group(1))
                except (IndexError, ValueError):
                    continue
                pct = max(0.0, min(0.99, current / max(iters, 1)))
                # Throttle to ~1 % steps so we don't flood the events
                # bus with every iter (splatfacto can emit several
                # lines per iteration).
                if pct - last_pct >= 0.01:
                    await progress(pct, f"train: iter {current}/{iters}")
                    last_pct = pct

        rc = await proc.wait()
    finally:
        if job_id is not None:
            _running.unregister(job_id)

    if rc != 0:
        # Surface the tail of the just-written log file in the
        # exception so it propagates into the job row's error and
        # renders inline on the native JobDetailActivity. No more
        # docker-exec-into-the-worker-and-cat-the-log dance.
        tail = tail_file(log_path)
        raise RuntimeError(
            format_subprocess_error("ns-train", rc, log_path, tail)
        )

    config = _find_latest_config(train_dir)
    await progress(1.0, "train: done")
    return {"config": str(config) if config else None, "iters": iters}


def _find_latest_config(train_dir: Path) -> Path | None:
    candidates = sorted(train_dir.rglob("config.yml"))
    return candidates[-1] if candidates else None


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

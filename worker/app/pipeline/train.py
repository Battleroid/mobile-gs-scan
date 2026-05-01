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
import shutil
from pathlib import Path
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

ProgressCb = Callable[[float, str], Awaitable[None]]


async def run_train(
    *,
    scene_dir: Path,
    iters: int,
    progress: ProgressCb,
) -> dict:
    train_dir = scene_dir / "train"
    train_dir.mkdir(parents=True, exist_ok=True)

    sfm_dir = scene_dir / "sfm"
    if (sfm_dir / "synthetic.json").exists() or not shutil.which("ns-train"):
        return await _run_stub(train_dir=train_dir, iters=iters, progress=progress)

    return await _run_splatfacto(
        scene_dir=scene_dir, train_dir=train_dir, iters=iters, progress=progress
    )


async def _run_splatfacto(
    *,
    scene_dir: Path,
    train_dir: Path,
    iters: int,
    progress: ProgressCb,
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

    iter_marker = b"iter "
    last_pct = 0.0
    with log_path.open("wb") as logf:
        assert proc.stdout is not None
        async for raw in proc.stdout:
            logf.write(raw)
            line = raw.strip()
            # Parse splatfacto's "iter NNN" progress lines so the web
            # UI gets a smooth bar instead of waiting on completion.
            ix = line.find(iter_marker)
            if ix >= 0:
                tail = line[ix + len(iter_marker):]
                try:
                    current = int(tail.split()[0])
                except (IndexError, ValueError):
                    continue
                pct = max(0.0, min(0.99, current / max(iters, 1)))
                if pct - last_pct >= 0.01:
                    await progress(pct, f"train: iter {current}/{iters}")
                    last_pct = pct

    rc = await proc.wait()
    if rc != 0:
        raise RuntimeError(f"ns-train exited {rc}, see {log_path}")

    config = _find_latest_config(train_dir)
    await progress(1.0, "train: done")
    return {"config": str(config) if config else None, "iters": iters}


def _find_latest_config(train_dir: Path) -> Path | None:
    candidates = sorted(train_dir.rglob("config.yml"))
    return candidates[-1] if candidates else None


async def _run_stub(
    *, train_dir: Path, iters: int, progress: ProgressCb
) -> dict:
    """Synthetic checkpoint. Walks the progress bar so the UI animates."""
    await progress(0.0, "train: synthetic (no nerfstudio installed)")
    steps = 20
    for i in range(1, steps + 1):
        await asyncio.sleep(0.2)
        await progress(i / steps, f"train: synthetic step {i}/{steps}")
    marker = train_dir / "synthetic.json"
    marker.write_text(json.dumps({"iters": iters, "stub": True}))
    return {"stub": True, "iters": iters}

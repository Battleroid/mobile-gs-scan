"""Poisson mesh reconstruction (PR #2).

In PR #1 this is a no-op — the runner short-circuits on the
`deferred=True` payload flag set by `pipeline.dispatch`. Stubbed out
here so the worker has a `JobKind.mesh` handler to dispatch to.
"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Awaitable, Callable

log = logging.getLogger(__name__)

ProgressCb = Callable[[float, str], Awaitable[None]]


async def run_mesh(
    *,
    scene_dir: Path,
    deferred: bool,
    progress: ProgressCb,
) -> dict:
    if deferred:
        await progress(1.0, "mesh: deferred to PR #2")
        return {"deferred": True}

    raise NotImplementedError("mesh path lands in PR #2")

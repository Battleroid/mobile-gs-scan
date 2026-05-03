"""Shared spz_pack subprocess helper.

Both the export step (initial splat → .ply + .spz) and the filter
step (edited .ply → edited .spz) need to invoke Niantic's `spz_pack`
binary the same way: read source PLY, write SPZ, capture stdout/stderr
into a log file. Centralised here so the two callers stay aligned.

If `spz_pack` isn't on PATH the helper returns False and the caller
should treat the SPZ output as optional. We never raise on a missing
binary because the rest of the pipeline can still serve the .ply.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

from app.pipeline import _running

log = logging.getLogger(__name__)


async def run_spz_pack(
    src_ply: Path,
    dst_spz: Path,
    *,
    log_path: Path | None = None,
    job_id: str | None = None,
) -> bool:
    """Pack `src_ply` to `dst_spz` via spz_pack.

    Returns True if the destination file exists after the run, False
    if spz_pack is missing or the binary failed to produce output.
    Stdout + stderr are captured into `log_path` when provided so the
    JobLogPanel surfaces the failure cause in the UI.
    """
    if not shutil.which("spz_pack"):
        log.info("spz_pack not on PATH; skipping spz pack for %s", dst_spz)
        return False

    proc = await asyncio.create_subprocess_exec(
        "spz_pack", str(src_ply), str(dst_spz),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    if job_id is not None:
        _running.register(job_id, proc)
    try:
        out = await proc.stdout.read() if proc.stdout else b""
        await proc.wait()
    finally:
        if job_id is not None:
            _running.unregister(job_id)
    if log_path is not None:
        try:
            log_path.write_bytes(out)
        except OSError:
            pass
    return dst_spz.exists()

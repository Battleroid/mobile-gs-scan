"""Shared spz_pack helper.

Both the export and filter pipelines emit a .spz alongside the canonical
.ply by shelling out to the upstream spz_pack CLI. spz_pack is built from
source in Dockerfile.gs; on hosts without it (api container, dev box)
the helper is a no-op and returns None so the caller can fall back to
serving just the .ply.
"""
from __future__ import annotations

import asyncio
import logging
import shutil
from pathlib import Path

log = logging.getLogger(__name__)


async def run_spz_pack(src_ply: Path, dst_spz: Path) -> Path | None:
    """Pack a .ply into a .spz. Returns the dst path on success, else None."""
    if not shutil.which("spz_pack"):
        return None
    proc = await asyncio.create_subprocess_exec(
        "spz_pack", str(src_ply), str(dst_spz),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out = await proc.stdout.read() if proc.stdout else b""
    rc = await proc.wait()
    log_path = dst_spz.with_suffix(".spz.log")
    try:
        log_path.write_bytes(out)
    except OSError:
        pass
    if rc != 0 or not dst_spz.exists():
        log.warning("spz_pack exited %d for %s (see %s)", rc, src_ply, log_path)
        return None
    return dst_spz

"""Export trained splat as .ply (canonical) + .spz (web/mobile)."""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
import struct
from pathlib import Path
from typing import Awaitable, Callable

from app.pipeline import _running
from app.pipeline._logtail import format_subprocess_error, tail_bytes
from app.pipeline._spz import run_spz_pack

log = logging.getLogger(__name__)

ProgressCb = Callable[[float, str], Awaitable[None]]


async def run_export(
    *,
    scene_dir: Path,
    formats: list[str],
    progress: ProgressCb,
    job_id: str | None = None,
) -> dict:
    export_dir = scene_dir / "export"
    export_dir.mkdir(parents=True, exist_ok=True)

    train_dir = scene_dir / "train"
    if (train_dir / "synthetic.json").exists() or not shutil.which("ns-export"):
        return await _run_stub(export_dir=export_dir, formats=formats, progress=progress)

    return await _run_real(
        train_dir=train_dir,
        export_dir=export_dir,
        formats=formats,
        progress=progress,
        job_id=job_id,
    )


async def _run_real(
    *,
    train_dir: Path,
    export_dir: Path,
    formats: list[str],
    progress: ProgressCb,
    job_id: str | None,
) -> dict:
    candidates = sorted(train_dir.rglob("config.yml"))
    if not candidates:
        raise RuntimeError("no nerfstudio config.yml under train/")
    config = candidates[-1]

    artifacts: dict[str, str] = {}

    if "ply" in formats:
        await progress(0.1, "export: ns-export gaussian-splat")
        cmd = [
            "ns-export", "gaussian-splat",
            "--load-config", str(config),
            "--output-dir", str(export_dir),
        ]
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
        if job_id is not None:
            _running.register(job_id, proc)
        try:
            out = await proc.stdout.read() if proc.stdout else b""
            rc = await proc.wait()
        finally:
            if job_id is not None:
                _running.unregister(job_id)
        log_path = export_dir / "export.log"
        log_path.write_bytes(out)
        if rc != 0:
            tail = tail_bytes(out)
            raise RuntimeError(
                format_subprocess_error("ns-export", rc, log_path, tail)
            )
        ply = next(export_dir.glob("*.ply"), None)
        if ply is None:
            raise RuntimeError("ns-export produced no .ply")
        ply_dst = export_dir / "scene.ply"
        if ply.resolve() != ply_dst.resolve():
            ply.replace(ply_dst)
        artifacts["ply"] = str(ply_dst)

    if "spz" in formats and "ply" in artifacts:
        await progress(0.7, "export: spz_pack")
        spz_dst = export_dir / "scene.spz"
        ok = await run_spz_pack(
            Path(artifacts["ply"]),
            spz_dst,
            log_path=export_dir / "spz_pack.log",
            job_id=job_id,
        )
        if ok:
            artifacts["spz"] = str(spz_dst)

    await progress(1.0, "export: done")
    return artifacts


async def _run_stub(
    *, export_dir: Path, formats: list[str], progress: ProgressCb
) -> dict:
    """Emit a tiny placeholder .ply so the viewer renders something."""
    await progress(0.5, "export: synthetic placeholder")
    ply = export_dir / "scene.ply"
    _write_stub_ply(ply)
    artifacts = {"ply": str(ply), "stub": "true"}
    if "spz" in formats:
        spz = export_dir / "scene.spz"
        if not spz.exists():
            try:
                spz.symlink_to(ply.name)
            except OSError:
                shutil.copy(ply, spz)
        artifacts["spz"] = str(spz)
    (export_dir / "synthetic.json").write_text(json.dumps({"stub": True}))
    await progress(1.0, "export: done (stub)")
    return artifacts


def _write_stub_ply(path: Path) -> None:
    """Minimal binary PLY of a single 3D Gaussian at the origin."""
    header = (
        "ply\n"
        "format binary_little_endian 1.0\n"
        "element vertex 1\n"
        "property float x\n"
        "property float y\n"
        "property float z\n"
        "property float f_dc_0\n"
        "property float f_dc_1\n"
        "property float f_dc_2\n"
        "property float opacity\n"
        "property float scale_0\n"
        "property float scale_1\n"
        "property float scale_2\n"
        "property float rot_0\n"
        "property float rot_1\n"
        "property float rot_2\n"
        "property float rot_3\n"
        "end_header\n"
    ).encode("ascii")

    body = struct.pack(
        "<fff fff f fff ffff",
        0.0, 0.0, 0.0,
        0.5, 0.5, 0.5,
        0.9,
        -2.3, -2.3, -2.3,
        1.0, 0.0, 0.0, 0.0,
    )
    path.write_bytes(header + body)

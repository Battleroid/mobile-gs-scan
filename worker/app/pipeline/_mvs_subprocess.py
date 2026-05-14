"""OpenMVS textured-mesh pipeline — child process orchestrator.

Drives the OpenMVS binaries in sequence to turn a nerfstudio-style
``transforms.json`` into a UV-mapped textured OBJ bundle:

    InterfaceCOLMAP    → staging/scene.mvs            (binary)
    DensifyPointCloud  → staging/scene_dense.mvs      (binary)
    ReconstructMesh    → staging/scene_mesh.mvs       (binary)
    RefineMesh         → staging/scene_refined.mvs    (binary, optional)
    TextureMesh        → staging/scene.{obj,mtl,jpg}  (canonical bundle)

Spawned by ``mesh.py``'s ``_run_mvs`` dispatcher. The parent owns
the staging-dir + atomic-swap hygiene; this orchestrator only does
the actual MVS work and emits PROGRESS lines for the parent's
``_stream_progress`` consumer.

Cancellation: the parent spawns this orchestrator with
``start_new_session=True`` and registers it with ``_running`` as a
process group leader. SIGKILL on the pgid reaches every in-flight
grandchild (``DensifyPointCloud`` in particular is the long-running
one and would otherwise survive a user cancel for minutes).

Usage::

    python -m app.pipeline._mvs_subprocess \\
        --transforms-json <path> \\
        --images-src-dir <path> \\
        --staging-dir <path> \\
        --params '<json>'

Outputs (stdout, line-buffered):
    ``PROGRESS <fraction> <message>`` lines for the parent's
    progress callback. All other lines are arbitrary log text the
    parent appends to ``mesh.log`` verbatim.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

from app.pipeline import _colmap_writer


def _emit(progress: float, msg: str) -> None:
    p = max(0.0, min(1.0, float(progress)))
    print(f"PROGRESS {p:.4f} {msg}", flush=True)


def _log(msg: str) -> None:
    print(msg, flush=True)


# OpenMVS binaries install to /usr/local/bin via the Dockerfile.gs
# step. ``shutil.which`` resolves them through $PATH so a developer
# can override with a local build if needed.
_REQUIRED_BINS = (
    "InterfaceCOLMAP",
    "DensifyPointCloud",
    "ReconstructMesh",
    "TextureMesh",
    # RefineMesh is *optional* in the sense that mvs_refine_iters=0
    # skips it entirely. We still verify presence here so a worker
    # built without the full OpenMVS suite fails loudly at startup
    # rather than mid-pipeline.
    "RefineMesh",
)


def _check_binaries() -> str | None:
    """Return the name of the first missing OpenMVS binary, or None
    if every required tool is on ``$PATH``."""
    for name in _REQUIRED_BINS:
        if shutil.which(name) is None:
            return name
    return None


def _run_step(
    name: str,
    cmd: list[str],
    *,
    cwd: Path,
) -> None:
    """Spawn one OpenMVS binary, stream its stdout/stderr into our
    own stdout (so the parent's log file picks it up), and raise on
    non-zero exit.

    Each step's full stdout is preserved verbatim under our PROGRESS
    lines so ``mesh.log`` ends up with a chronological multi-stage
    trace. The user can read it from ``JobLogPanel``.

    No PROGRESS lines are emitted from inside the OpenMVS binaries —
    they're chatty but don't expose a structured progress hook
    upstream-side. The orchestrator emits one PROGRESS at the
    start and end of each step so the parent's WS subscriber sees
    forward motion (see the milestones in ``main`` below).
    """
    _log(f"--- {name} ---")
    _log(f"$ {' '.join(cmd)}")
    proc = subprocess.run(
        cmd,
        cwd=cwd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    if proc.stdout:
        # Stream the captured output verbatim — preserves every
        # OpenMVS warning / progress percentage / final stats line.
        sys.stdout.write(proc.stdout)
        sys.stdout.flush()
    if proc.returncode != 0:
        raise RuntimeError(
            f"{name} exited {proc.returncode} (cwd={cwd})"
        )


def _run_mvs(
    *,
    transforms_path: Path,
    images_src_dir: Path,
    staging_dir: Path,
    params: dict,
) -> int:
    """Run the OpenMVS pipeline end-to-end. Returns 0 on success,
    non-zero on failure (caller surfaces the parent log tail in
    the job error).
    """
    dense_views = int(params.get("mvs_dense_views", 3))
    texture_size = int(params.get("mvs_texture_size", 4096))
    refine_iters = int(params.get("mvs_refine_iters", 2))

    missing = _check_binaries()
    if missing is not None:
        _log(
            f"ERROR: OpenMVS binary '{missing}' not on $PATH — "
            f"rebuild worker-gs with the OpenMVS Dockerfile step."
        )
        return 4

    # 1. COLMAP-text workspace from transforms.json. Pure Python,
    # no shell-out. We stage frames under ``colmap/images/`` so the
    # OpenMVS --image-folder flag picks them up by basename.
    _emit(0.02, "mvs: write colmap workspace")
    colmap_dir = staging_dir / "colmap"
    info = _colmap_writer.write_colmap_text(
        transforms_path=transforms_path,
        images_src_dir=images_src_dir,
        out_dir=colmap_dir,
    )
    _log(f"colmap workspace: {info['n_cameras']} cameras, {info['n_images']} images")

    # 2. InterfaceCOLMAP — converts COLMAP text → .mvs binary.
    _emit(0.05, "mvs: InterfaceCOLMAP")
    scene_mvs = staging_dir / "scene.mvs"
    _run_step(
        "InterfaceCOLMAP",
        [
            "InterfaceCOLMAP",
            "-i", str(colmap_dir),
            "-o", str(scene_mvs),
            "--image-folder", str(colmap_dir / "images"),
        ],
        cwd=staging_dir,
    )
    if not scene_mvs.exists():
        raise RuntimeError(
            "InterfaceCOLMAP exited 0 but produced no scene.mvs"
        )

    # 3. DensifyPointCloud — the heaviest step. PMVS-style patch
    # matching; CPU densifier (we built OpenMVS with CUDA off).
    _emit(0.10, "mvs: DensifyPointCloud")
    dense_mvs = staging_dir / "scene_dense.mvs"
    _run_step(
        "DensifyPointCloud",
        [
            "DensifyPointCloud",
            str(scene_mvs),
            "--number-views-fuse", str(dense_views),
            "--resolution-level", "1",
            "-o", str(dense_mvs),
        ],
        cwd=staging_dir,
    )
    if not dense_mvs.exists():
        raise RuntimeError(
            "DensifyPointCloud exited 0 but produced no scene_dense.mvs"
        )

    # 4. ReconstructMesh — Delaunay tetrahedralisation + min-cut.
    # Fast (seconds) on top of an already-dense cloud.
    _emit(0.55, "mvs: ReconstructMesh")
    mesh_mvs = staging_dir / "scene_mesh.mvs"
    _run_step(
        "ReconstructMesh",
        [
            "ReconstructMesh",
            str(dense_mvs),
            "-o", str(mesh_mvs),
        ],
        cwd=staging_dir,
    )
    if not mesh_mvs.exists():
        raise RuntimeError(
            "ReconstructMesh exited 0 but produced no scene_mesh.mvs"
        )

    # 5. RefineMesh — multi-view photo-consistency refinement.
    # Skip entirely on ``mvs_refine_iters=0`` (the documented
    # opt-out for RAM-constrained hosts; the mesh remains usable
    # without refine, just less detailed).
    if refine_iters > 0:
        _emit(0.62, f"mvs: RefineMesh ({refine_iters} iters)")
        refined_mvs = staging_dir / "scene_refined.mvs"
        _run_step(
            "RefineMesh",
            [
                "RefineMesh",
                str(mesh_mvs),
                "--max-iters", str(refine_iters),
                "-o", str(refined_mvs),
            ],
            cwd=staging_dir,
        )
        if not refined_mvs.exists():
            raise RuntimeError(
                "RefineMesh exited 0 but produced no scene_refined.mvs"
            )
        texture_in = refined_mvs
    else:
        _log("RefineMesh: skipped (mvs_refine_iters=0)")
        texture_in = mesh_mvs

    # 6. TextureMesh — bake per-triangle UVs + atlas the input
    # frames into JPG texture pages. ``--export-type obj`` is
    # essential — OpenMVS otherwise defaults to PLY which doesn't
    # carry UV mappings cleanly.
    _emit(0.85, f"mvs: TextureMesh ({texture_size}px)")
    final_mvs = staging_dir / "scene_textured.mvs"
    _run_step(
        "TextureMesh",
        [
            "TextureMesh",
            str(texture_in),
            "--texture-size", str(texture_size),
            "--export-type", "obj",
            "-o", str(final_mvs),
        ],
        cwd=staging_dir,
    )

    # OpenMVS's TextureMesh writes the OBJ + MTL + JPGs alongside
    # the .mvs output it was given. Filenames are derived from the
    # ``-o`` argument's stem. Verify all three pieces exist before
    # signalling success to the parent.
    obj_path = staging_dir / "scene_textured.obj"
    mtl_path = staging_dir / "scene_textured.mtl"
    tex_globs = sorted(staging_dir.glob("scene_textured*.jpg"))
    if not obj_path.exists() or not mtl_path.exists() or not tex_globs:
        raise RuntimeError(
            f"TextureMesh exited 0 but the bundle is incomplete: "
            f"obj={obj_path.exists()} mtl={mtl_path.exists()} "
            f"tex_count={len(tex_globs)}"
        )

    # Rename ``scene_textured.*`` → ``scene.*`` so the parent's
    # atomic-swap doesn't need to learn OpenMVS's naming. The
    # MTL's internal ``map_Kd scene_textured0.jpg`` reference
    # also gets rewritten in-place so the relative-URL lookup
    # from the web's MTLLoader resolves to ``scene_tex0.jpg``
    # (matching the artifact route's allowlist regex).
    _emit(0.93, "mvs: rename bundle")
    rename_map: dict[str, str] = {}
    for i, tex_src in enumerate(tex_globs):
        # TextureMesh emits ``scene_textured0.jpg``, ``scene_textured1.jpg``,
        # etc. Renaming to ``scene_tex0.jpg`` etc. matches the
        # artifact route's allowlist + keeps the legacy single-
        # texture naming readable.
        tex_dst = staging_dir / f"scene_tex{i}.jpg"
        tex_src.rename(tex_dst)
        rename_map[tex_src.name] = tex_dst.name
    final_obj = staging_dir / "scene.obj"
    final_mtl = staging_dir / "scene.mtl"
    obj_path.rename(final_obj)
    mtl_path.rename(final_mtl)
    rename_map[obj_path.name] = final_obj.name
    rename_map[mtl_path.name] = final_mtl.name

    # Rewrite the MTL's ``map_Kd`` line and (defensively) the
    # OBJ's ``mtllib`` line to point at the renamed files. The
    # rest of each file is opaque numeric data we don't touch.
    mtl_text = final_mtl.read_text()
    for old, new in rename_map.items():
        mtl_text = mtl_text.replace(old, new)
    final_mtl.write_text(mtl_text)
    obj_text = final_obj.read_text()
    for old, new in rename_map.items():
        obj_text = obj_text.replace(old, new)
    final_obj.write_text(obj_text)
    _log(f"renamed {len(rename_map)} bundle files")

    # 7. Best-effort GLB pack — embeds the textures so the web's
    # GLTFLoader-based path still works as a single-file fallback.
    # trimesh's OBJ+MTL→glTF round-trip occasionally chokes on
    # multi-material outputs; the OBJ bundle is the canonical
    # output so a GLB miss is non-fatal.
    _emit(0.96, "mvs: glb-pack")
    try:
        import trimesh

        mesh_t = trimesh.load(final_obj, force="mesh")
        staged_glb = staging_dir / "scene.glb"
        mesh_t.export(staged_glb)
        if staged_glb.exists():
            _log(f"wrote {staged_glb.name}")
    except Exception as exc:  # noqa: BLE001
        _log(f"glb conversion failed: {exc}; obj+mtl+jpg only")

    # Garbage-collect the staging COLMAP workspace + intermediate
    # .mvs files so the parent's atomic-swap doesn't haul them
    # into the canonical mesh_dir.
    _emit(0.98, "mvs: cleanup")
    for stem in (
        "scene", "scene_dense", "scene_mesh", "scene_refined",
        "scene_textured",
    ):
        for p in staging_dir.glob(f"{stem}.mvs"):
            p.unlink(missing_ok=True)
        for p in staging_dir.glob(f"{stem}_dense.ply"):
            p.unlink(missing_ok=True)
    if (staging_dir / "colmap").exists():
        shutil.rmtree(staging_dir / "colmap", ignore_errors=True)

    _emit(1.0, "done")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--transforms-json", required=True)
    ap.add_argument("--images-src-dir", required=True)
    ap.add_argument("--staging-dir", required=True)
    ap.add_argument("--params", required=True, help="JSON-encoded params dict")
    args = ap.parse_args()

    # We're already running inside the new process group the
    # parent spawned us with (``start_new_session=True``). Belt-
    # and-suspenders setsid() in case the parent ever forgets — a
    # no-op if we're already a group leader.
    try:
        os.setsid()
    except (OSError, PermissionError):
        # Already a session leader.
        pass

    return _run_mvs(
        transforms_path=Path(args.transforms_json),
        images_src_dir=Path(args.images_src_dir),
        staging_dir=Path(args.staging_dir),
        params=json.loads(args.params),
    )


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as exc:
        # Surface a clean tail-friendly message rather than a
        # Python traceback in mesh.log; the parent's error path
        # picks up the stdout tail verbatim.
        _log(f"ERROR: {exc}")
        sys.exit(5)

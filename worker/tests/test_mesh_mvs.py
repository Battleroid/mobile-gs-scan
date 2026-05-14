"""Tests for the standard-tier (OpenMVS) mesh extraction surface.

The pytest environment can't run the actual OpenMVS binaries (no
``DensifyPointCloud`` / ``TextureMesh`` on CI hosts; CUDA-adjacent
worker image required), so this file focuses on the surfaces that
DON'T need those binaries:

* Validator: ``tier="standard"`` now accepted; new knobs
  (``mvs_dense_views`` / ``mvs_texture_size`` / ``mvs_refine_iters``)
  enforce their ranges + power-of-2 / boolean-rejection invariants.
* ``trigger_mesh`` endpoint: persisted ``tier="standard"`` no longer
  trips the inactive-tier guard.
* COLMAP-text writer (``_colmap_writer.write_colmap_text``): a
  synthetic 3-frame ``transforms.json`` round-trips through the
  writer; ``cameras.txt`` has the expected single row, ``images.txt``
  has the expected per-frame lines, ``points3D.txt`` is empty,
  every emitted quaternion is unit-norm, and the W2C decomposition
  invariant ``c2w = inv(diag(1,-1,-1,1) @ inv(w2c))`` round-trips.
* Tier dispatch: ``run_mesh`` with ``tier="standard"`` no longer
  raises ``NotImplementedError`` at the dispatcher (the
  subprocess-binary-missing path is exercised in CI via the
  ``_check_binaries`` guard inside the subprocess; tested
  separately by the dispatcher writing ``mesh.log`` with the
  expected ERROR line if the binaries are absent).
* Mesh-assets artifact route: filename allowlist regex rejects
  traversal attempts, accepts canonical bundle names.

End-to-end (real captures → textured mesh) verification happens
via the dev ``make up`` smoke test described in the plan / PR.
"""
from __future__ import annotations

import asyncio
import json
from pathlib import Path

import numpy as np
import pytest
from fastapi import HTTPException

from app.api.scenes import (
    _MESH_ASSET_RE,
    MeshRequest,
    _validate_mesh_params,
    trigger_mesh,
)
from app.config import Settings
from app.jobs import store
from app.jobs.schema import CaptureStatus, MeshStatus
from app.pipeline import _colmap_writer


def _run(coro):
    return asyncio.run(coro)


@pytest.fixture
def isolated_store(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path,
        db_filename="test_mesh_mvs.sqlite",
    )

    async def setup():
        await store.init_store(settings)

    async def teardown():
        await store.shutdown_store()

    asyncio.run(setup())
    yield
    asyncio.run(teardown())


# ─── validation: standard tier knobs ─────────────────────────


def test_validate_mesh_params_accepts_standard_tier():
    out = _validate_mesh_params({"tier": "standard"})
    assert out == {"tier": "standard"}


def test_validate_mesh_params_accepts_full_mvs_knob_set():
    out = _validate_mesh_params({
        "tier": "standard",
        "mvs_dense_views": 5,
        "mvs_texture_size": 2048,
        "mvs_refine_iters": 0,
    })
    assert out["tier"] == "standard"
    assert out["mvs_dense_views"] == 5
    assert out["mvs_texture_size"] == 2048
    assert out["mvs_refine_iters"] == 0


@pytest.mark.parametrize("bad", [1, 8, 0, -1, True, "5"])
def test_validate_mesh_params_rejects_bad_mvs_dense_views(bad):
    with pytest.raises(HTTPException) as exc:
        _validate_mesh_params({"mvs_dense_views": bad})
    assert exc.value.status_code == 422


@pytest.mark.parametrize("bad", [3000, 512, 0, -1024, True, "4096", 16384])
def test_validate_mesh_params_rejects_bad_mvs_texture_size(bad):
    with pytest.raises(HTTPException) as exc:
        _validate_mesh_params({"mvs_texture_size": bad})
    assert exc.value.status_code == 422


@pytest.mark.parametrize("bad", [-1, 5, 100, True, "2"])
def test_validate_mesh_params_rejects_bad_mvs_refine_iters(bad):
    with pytest.raises(HTTPException) as exc:
        _validate_mesh_params({"mvs_refine_iters": bad})
    assert exc.value.status_code == 422


def test_validate_mesh_params_accepts_zero_refine_iters():
    """Zero is the documented opt-out (skip RefineMesh entirely);
    range check must permit it."""
    out = _validate_mesh_params({"mvs_refine_iters": 0})
    assert out["mvs_refine_iters"] == 0


# ─── trigger endpoint: standard is no longer a 422 path ──────


def test_trigger_accepts_persisted_standard_tier(isolated_store):
    """The inactive-tier guard from the PR-D-era validator must
    no longer fire for ``tier="standard"`` now that the OpenMVS
    backend is wired up."""

    async def go():
        cap = await store.create_capture(name="t-standard", source="upload")
        scene = await store.create_scene(cap.id)
        assert scene is not None
        await store.update_scene(
            scene.id,
            status=CaptureStatus.completed,
            mesh_params={"tier": "standard"},
        )

        # No params body — the persisted ``tier="standard"`` row
        # used to 422 (PR #111's guard); now it must queue.
        result = await trigger_mesh(scene.id, MeshRequest(params=None))
        assert result.mesh_status == MeshStatus.queued

    asyncio.run(go())


def test_trigger_accepts_persisted_higher_tier_now(isolated_store):
    """``higher`` was an inactive tier in the PR #113 era; it
    flipped active when the 2DGS backend landed. The trigger
    endpoint must accept it now — the inactive-tier guard only
    fires for genuinely-unknown values (covered by
    ``test_trigger_rejects_persisted_unknown_tier`` in
    test_mesh_tsdf)."""

    async def go():
        cap = await store.create_capture(name="t-higher", source="upload")
        scene = await store.create_scene(cap.id)
        assert scene is not None
        await store.update_scene(
            scene.id,
            status=CaptureStatus.completed,
            mesh_params={"tier": "higher"},
        )

        result = await trigger_mesh(scene.id, MeshRequest(params=None))
        assert result.mesh_status == MeshStatus.queued

    asyncio.run(go())


# ─── COLMAP-text writer ──────────────────────────────────────


def _write_synthetic_transforms(
    path: Path, *, n_frames: int = 3, w: int = 1920, h: int = 1080,
) -> None:
    """Build a minimal 3-frame transforms.json the writer will
    accept. Camera poses are arbitrary but valid SE(3) — we just
    need them through the quaternion path."""
    fx = fy = 1500.0
    cx, cy = w / 2.0, h / 2.0
    frames = []
    for i in range(n_frames):
        # Rotate slightly around Y per frame so each pose is
        # distinct, then translate along -Z so the cameras stand
        # back from the origin (mimics a phone-held capture).
        theta = i * 0.2
        c, s = np.cos(theta), np.sin(theta)
        R = np.array([
            [c, 0.0, s],
            [0.0, 1.0, 0.0],
            [-s, 0.0, c],
        ])
        t = np.array([0.0, 0.0, -2.0])
        m = np.eye(4, dtype=np.float64)
        m[:3, :3] = R
        m[:3, 3] = t
        frames.append({
            "file_path": f"images/{i:06d}.jpg",
            "transform_matrix": m.tolist(),
        })
    data = {
        "camera_model": "PINHOLE",
        "fl_x": fx, "fl_y": fy, "cx": cx, "cy": cy,
        "w": w, "h": h, "frames": frames,
    }
    path.write_text(json.dumps(data))


def test_colmap_writer_emits_expected_files(tmp_path: Path):
    transforms = tmp_path / "transforms.json"
    _write_synthetic_transforms(transforms, n_frames=3)
    # Build a stub source image directory so the symlink path
    # has something to point at.
    images_src = tmp_path / "src_images"
    images_src.mkdir()
    for i in range(3):
        (images_src / f"{i:06d}.jpg").write_bytes(b"")

    out_dir = tmp_path / "colmap"
    info = _colmap_writer.write_colmap_text(
        transforms_path=transforms,
        images_src_dir=images_src,
        out_dir=out_dir,
    )
    assert info == {"n_cameras": 1, "n_images": 3}

    cams = (out_dir / "cameras.txt").read_text().splitlines()
    # Header comments + one data row.
    data_rows = [ln for ln in cams if ln and not ln.startswith("#")]
    assert len(data_rows) == 1
    fields = data_rows[0].split()
    assert fields[0] == "1"           # camera_id
    assert fields[1] == "PINHOLE"
    assert fields[2] == "1920"
    assert fields[3] == "1080"
    assert pytest.approx(float(fields[4])) == 1500.0  # fl_x

    imgs = (out_dir / "images.txt").read_text().splitlines()
    data_rows = [ln for ln in imgs if ln and not ln.startswith("#")]
    # Each image: pose line + empty observations line. 3 frames → 6 entries.
    # (The blank lines split() collapses, but our writer emits them so
    # the file matches COLMAP's documented two-lines-per-image shape.)
    pose_rows = [
        ln for ln in data_rows
        if ln.split()[0].isdigit() and len(ln.split()) > 5
    ]
    assert len(pose_rows) == 3

    # points3D.txt should be header-only.
    pts = (out_dir / "points3D.txt").read_text().splitlines()
    assert all(ln.startswith("#") or ln == "" for ln in pts)

    # Image directory should have one symlink per frame.
    staged = sorted((out_dir / "images").iterdir())
    assert [p.name for p in staged] == [
        "000000.jpg", "000001.jpg", "000002.jpg",
    ]


def test_colmap_writer_quaternions_are_unit_norm(tmp_path: Path):
    """Every emitted quaternion should be unit-norm; if Shepperd
    drift slips past the normaliser, OpenMVS treats the camera as
    invalid silently."""
    transforms = tmp_path / "transforms.json"
    _write_synthetic_transforms(transforms, n_frames=8)
    images_src = tmp_path / "src_images"
    images_src.mkdir()
    for i in range(8):
        (images_src / f"{i:06d}.jpg").write_bytes(b"")

    out_dir = tmp_path / "colmap"
    _colmap_writer.write_colmap_text(
        transforms_path=transforms,
        images_src_dir=images_src,
        out_dir=out_dir,
    )

    img_lines = (out_dir / "images.txt").read_text().splitlines()
    pose_rows = [
        ln for ln in img_lines
        if ln and not ln.startswith("#") and ln.split()[0].isdigit() and len(ln.split()) > 5
    ]
    for row in pose_rows:
        fields = row.split()
        qw, qx, qy, qz = (float(x) for x in fields[1:5])
        norm = np.sqrt(qw * qw + qx * qx + qy * qy + qz * qz)
        assert abs(norm - 1.0) < 1e-6, f"non-unit quaternion: {norm}"


def test_colmap_writer_axis_round_trip(tmp_path: Path):
    """The OpenGL→OpenCV axis fix should be reversible: starting
    from the writer's emitted w2c (OpenCV) and inverting back with
    the same diag(1,-1,-1,1) flip should recover the original c2w."""
    transforms = tmp_path / "transforms.json"
    _write_synthetic_transforms(transforms, n_frames=1)
    images_src = tmp_path / "src_images"
    images_src.mkdir()
    (images_src / "000000.jpg").write_bytes(b"")

    out_dir = tmp_path / "colmap"
    _colmap_writer.write_colmap_text(
        transforms_path=transforms,
        images_src_dir=images_src,
        out_dir=out_dir,
    )

    # Reconstruct w2c from the emitted quaternion + translation.
    img_lines = (out_dir / "images.txt").read_text().splitlines()
    pose_row = next(
        ln for ln in img_lines
        if ln and not ln.startswith("#") and ln.split()[0] == "1"
    )
    fields = pose_row.split()
    qw, qx, qy, qz = (float(x) for x in fields[1:5])
    tx, ty, tz = (float(x) for x in fields[5:8])
    # Quaternion → rotation matrix (wxyz convention).
    R = np.array([
        [1 - 2 * (qy * qy + qz * qz), 2 * (qx * qy - qz * qw), 2 * (qx * qz + qy * qw)],
        [2 * (qx * qy + qz * qw), 1 - 2 * (qx * qx + qz * qz), 2 * (qy * qz - qx * qw)],
        [2 * (qx * qz - qy * qw), 2 * (qy * qz + qx * qw), 1 - 2 * (qx * qx + qy * qy)],
    ])
    w2c = np.eye(4, dtype=np.float64)
    w2c[:3, :3] = R
    w2c[:3, 3] = (tx, ty, tz)
    # Invert + flip back → expected original c2w.
    c2w_cv = np.linalg.inv(w2c)
    c2w_opengl = c2w_cv @ np.diag([1.0, -1.0, -1.0, 1.0])

    expected = json.loads(transforms.read_text())["frames"][0]["transform_matrix"]
    np.testing.assert_allclose(c2w_opengl, np.asarray(expected), atol=1e-9)


# ─── artifact route: filename allowlist ──────────────────────


@pytest.mark.parametrize("good", [
    "scene.obj",
    "scene.mtl",
    "scene.glb",
    "scene_tex0.jpg",
    "scene_tex1.jpg",
    "scene_tex9.jpg",
    "scene_tex10.jpg",
    "scene_tex0.png",
    # OpenMVS atlases large captures into many pages — the
    # allowlist must not cap the index width, otherwise references
    # to higher-index pages 400 and textured rendering breaks.
    "scene_tex100.jpg",
    "scene_tex999.png",
])
def test_mesh_asset_regex_accepts_canonical_names(good):
    assert _MESH_ASSET_RE.match(good)


@pytest.mark.parametrize("bad", [
    "../etc/passwd",
    "scene.log",
    "scene.zip",
    "scene_tex.jpg",       # no number
    "scene_texabc.jpg",    # non-numeric
    "scenex.obj",          # not exactly "scene"
    "scene.obj/..",        # traversal
    "scene.OBJ",           # case mismatch (be strict)
    "",
])
def test_mesh_asset_regex_rejects_anything_else(bad):
    assert _MESH_ASSET_RE.match(bad) is None

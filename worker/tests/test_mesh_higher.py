"""Tests for the higher-tier (2DGS retrain) mesh extraction surface.

The actual 2DGS retrain + dome render + texture bake can't run in
CI — needs CUDA + gsplat + the worker-gs image's torch stack. So
this file focuses on the surfaces that DON'T need those:

* Validator: ``tier="higher"`` is now accepted; ``higher_train_iters``
  range + bool-rejection invariants.
* Trigger endpoint: persisted ``tier="higher"`` no longer 422s
  (the prior inactive-tier guard from PR #111 had to relax when
  the backend landed).
* Dispatcher: ``run_mesh`` with ``tier="higher"`` no longer raises
  ``NotImplementedError`` at the parent (it now requires
  ``sfm/transforms.json`` like the standard tier — same shape
  failure if the SfM step never ran).

End-to-end (real capture → 2DGS retrain → textured mesh) happens
on the dev box via ``make up`` per the PR.
"""
from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from fastapi import HTTPException

from app.api.scenes import (
    MeshRequest,
    _validate_mesh_params,
    trigger_mesh,
)
from app.config import Settings
from app.jobs import store
from app.jobs.schema import CaptureStatus, MeshStatus
from app.pipeline import mesh as mesh_step


@pytest.fixture
def isolated_store(tmp_path: Path):
    settings = Settings(
        data_dir=tmp_path,
        db_filename="test_mesh_higher.sqlite",
    )

    async def setup():
        await store.init_store(settings)

    async def teardown():
        await store.shutdown_store()

    asyncio.run(setup())
    yield
    asyncio.run(teardown())


# ─── validation: higher tier ─────────────────────────────────


def test_validate_mesh_params_accepts_higher_tier():
    out = _validate_mesh_params({"tier": "higher"})
    assert out == {"tier": "higher"}


def test_validate_mesh_params_accepts_higher_train_iters():
    out = _validate_mesh_params({
        "tier": "higher",
        "higher_train_iters": 7_500,
    })
    assert out["tier"] == "higher"
    assert out["higher_train_iters"] == 7_500


@pytest.mark.parametrize("bad", [
    1_999,        # below floor
    30_001,       # above ceiling
    0,
    -100,
    True,
    "10000",
])
def test_validate_mesh_params_rejects_bad_higher_train_iters(bad):
    with pytest.raises(HTTPException) as exc:
        _validate_mesh_params({"higher_train_iters": bad})
    assert exc.value.status_code == 422


def test_validate_mesh_params_accepts_higher_train_iters_at_bounds():
    """The documented range is [2000, 30000] inclusive — the
    range check must accept both ends so a forward-rolled UI
    using ``min`` / ``max`` from the same constants doesn't 422
    on its own canonical values."""
    lo = _validate_mesh_params({"higher_train_iters": 2_000})
    hi = _validate_mesh_params({"higher_train_iters": 30_000})
    assert lo["higher_train_iters"] == 2_000
    assert hi["higher_train_iters"] == 30_000


# ─── trigger endpoint: higher is no longer a 422 path ────────


def test_trigger_accepts_persisted_higher_tier(isolated_store):
    """``higher`` was added to the active set when the 2DGS
    backend landed. The PR #111-era validator + the trigger
    endpoint's persisted-tier guard must both let it through now.
    The inactive-tier rejection from older PRs targeted exactly
    this path; it can't fire anymore."""

    async def go():
        cap = await store.create_capture(name="t-higher", source="upload")
        scene = await store.create_scene(cap.id)
        assert scene is not None
        await store.update_scene(
            scene.id,
            status=CaptureStatus.completed,
            mesh_params={"tier": "higher"},
        )

        # Empty body — the persisted ``tier="higher"`` row used
        # to 422 under the inactive-tier guard. With the backend
        # wired, the trigger must queue.
        result = await trigger_mesh(scene.id, MeshRequest(params=None))
        assert result.mesh_status == MeshStatus.queued

    asyncio.run(go())


# ─── dispatch: higher tier needs transforms.json ─────────────


# ─── SceneView URL builders cover higher tier ────────────────


def test_mesh_tex_urls_includes_higher_tier_bundle(isolated_store, tmp_path):
    """Regression for the codex P2: ``_mesh_tex_urls`` used to
    only return texture URLs when ``mesh_params.tier == "standard"``,
    leaving higher-tier extractions with null sidecars even though
    the bundle exists on disk. Now gated on the tier set
    ``_TEXTURED_BUNDLE_TIERS = {"standard", "higher"}``."""
    from app.api.scenes import _mesh_tex_urls

    async def go():
        cap = await store.create_capture(name="t-higher-tex", source="upload")
        scene = await store.create_scene(cap.id)
        assert scene is not None
        # Stage a higher-tier mesh bundle on disk under the
        # scene's canonical mesh dir.
        from app.config import get_settings
        mesh_dir = get_settings().scenes_dir() / scene.id / "mesh"
        mesh_dir.mkdir(parents=True, exist_ok=True)
        (mesh_dir / "scene.obj").write_text("# stub")
        (mesh_dir / "scene.mtl").write_text("newmtl scene\nmap_Kd scene_tex0.jpg\n")
        (mesh_dir / "scene_tex0.jpg").write_bytes(b"\xff\xd8\xff\xe0stub")
        (mesh_dir / "scene_tex1.jpg").write_bytes(b"\xff\xd8\xff\xe0stub")
        await store.update_scene(
            scene.id,
            mesh_obj_path=str(mesh_dir / "scene.obj"),
            mesh_params={"tier": "higher"},
        )

        refreshed = await store.get_scene(scene.id)
        assert refreshed is not None
        urls = _mesh_tex_urls(refreshed)
        assert urls is not None
        assert len(urls) == 2
        assert all("/artifacts/mesh_assets/scene_tex" in u for u in urls)

    asyncio.run(go())


def test_mesh_tex_urls_returns_none_for_low_tier_even_with_stale_files(
    isolated_store, tmp_path,
):
    """The defensive belt-and-suspenders gate from PR #114 must
    still fire — a low-tier scene whose disk still holds stale
    standard / higher sidecars (somehow) doesn't get them
    advertised to the web client. ``_run_low_tier``'s cleanup
    handles the on-disk side; this gate handles the API surface."""
    from app.api.scenes import _mesh_tex_urls

    async def go():
        cap = await store.create_capture(name="t-low-stale", source="upload")
        scene = await store.create_scene(cap.id)
        assert scene is not None
        from app.config import get_settings
        mesh_dir = get_settings().scenes_dir() / scene.id / "mesh"
        mesh_dir.mkdir(parents=True, exist_ok=True)
        (mesh_dir / "scene.obj").write_text("# stub vertex-colored")
        (mesh_dir / "scene_tex0.jpg").write_bytes(b"\xff\xd8stub")
        await store.update_scene(
            scene.id,
            mesh_obj_path=str(mesh_dir / "scene.obj"),
            mesh_params={"tier": "low"},
        )

        refreshed = await store.get_scene(scene.id)
        assert refreshed is not None
        assert _mesh_tex_urls(refreshed) is None

    asyncio.run(go())


def test_run_mesh_higher_requires_transforms_json(tmp_path: Path):
    """The higher tier consumes the SfM workspace via
    ``_higher_subprocess`` (the 2DGS retrain supervises against
    the SfM frames). If transforms.json is missing, the parent
    raises a clear RuntimeError so the runner writes ``mesh_error``
    cleanly — same shape the standard tier uses."""
    scene_dir = tmp_path / "scene_X"
    scene_dir.mkdir()
    src_ply = scene_dir / "scene.ply"
    src_ply.write_bytes(b"ply\nformat ascii 1.0\nelement vertex 0\nend_header\n")

    async def _noop_progress(p, m):
        return None

    async def go():
        with pytest.raises(RuntimeError, match="transforms.json"):
            await mesh_step.run_mesh(
                scene_dir=scene_dir,
                src_ply=src_ply,
                params={"tier": "higher"},
                progress=_noop_progress,
                job_id="t",
            )

    asyncio.run(go())

"""Scene HTTP + event-WebSocket routes."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.config import get_settings
from app.jobs import events, store, thumb_regen
from app.jobs.schema import EditStatus, JobKind, JobStatus, MeshStatus, Scene
from app.pipeline.filter import validate_recipe
from app.pipeline.mesh import DEFAULT_PARAMS as MESH_DEFAULT_PARAMS

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/scenes", tags=["scenes"])


class JobView(BaseModel):
    id: str
    kind: str
    status: str
    progress: float
    progress_msg: str | None
    error: str | None


class SceneView(BaseModel):
    id: str
    capture_id: str
    status: str
    error: str | None
    ply_url: str | None
    spz_url: str | None
    edited_ply_url: str | None
    edited_spz_url: str | None
    edit_status: str
    edit_error: str | None
    edit_recipe: dict | None
    mesh_obj_url: str | None
    mesh_glb_url: str | None
    # Sibling texture URLs for the standard-tier (OpenMVS) bundle.
    # ``mesh_obj_url`` references the textures by relative URL via
    # the MTL's ``map_Kd`` directive; the SceneView exposes the
    # absolute URLs separately so the web UI can offer per-file
    # download links and so a future native consumer can fetch the
    # bundle eagerly. Null on low-tier meshes (vertex-colored OBJ
    # alone, no MTL / JPGs) and on scenes without a mesh yet.
    mesh_tex_urls: list[str] | None
    mesh_status: str
    mesh_error: str | None
    mesh_params: dict | None
    # PNG thumbnail of the trained splat, rendered post-export.
    # Null when the scene hasn't reached the thumbnail step yet,
    # the render failed, or the scene is a stub. Web ``CaptureCard``
    # falls back to a chip-tinted gradient placeholder when null.
    thumb_url: str | None
    # 24-frame MP4 orbit of the trained splat, rendered after the
    # still PNG above lands. Two-stage thumbnail: ``thumb_url``
    # comes back first (~10 s after export), ``orbit_url`` backfills
    # 1-3 min later. CaptureCard prefers ``orbit_url`` when present
    # and falls back to ``thumb_url`` otherwise.
    orbit_url: str | None
    jobs: list[JobView]
    created_at: str
    completed_at: str | None


def _collapse_thumb_orbit_history(jobs: list) -> list:
    """Hide superseded thumbnail / orbit rows from the API response.

    Each filter apply / discard enqueues a fresh thumbnail+orbit
    pair (see app.jobs.thumb_regen.enqueue_regen) — by design, so the
    new render reflects the latest source PLY. But the prior rows
    stay in the DB after the regen lands, and the pipeline UI on web
    + Android shows every row from ``scene.jobs``, so after a handful
    of filter cycles the pipeline list grows several thumbnail and
    orbit entries even though only the most-recent of each is
    meaningful to the user.

    Collapse the history at serialization time: keep all non-(thumb,
    orbit) rows verbatim, and for each of those two kinds keep only
    the most-recently-created row. Audit rows stay in the DB; this
    is purely a view filter. Filter / mesh history is intentionally
    NOT collapsed — those are user-initiated and the row count
    matches user intent (one click = one row).
    """
    collapsible = {JobKind.thumbnail, JobKind.orbit}
    latest_by_kind: dict[JobKind, object] = {}
    out: list = []
    for j in jobs:
        if j.kind in collapsible:
            existing = latest_by_kind.get(j.kind)
            if existing is None or j.created_at > existing.created_at:
                latest_by_kind[j.kind] = j
        else:
            out.append(j)
    out.extend(latest_by_kind.values())
    # Preserve creation order across the merged list so the UI's
    # pipeline rendering stays consistent (mirrors the natural
    # extract → sfm → train → export → thumbnail → orbit sequence
    # for a fresh capture).
    out.sort(key=lambda j: j.created_at)
    return out


def _scene_mesh_dir(scene: Scene) -> Path:
    """Resolve the on-disk mesh dir for a scene. Used by the URL
    builders below to enumerate texture-atlas pages without reading
    a stored path column (the standard tier's texture URLs are
    derived at serialization time, not persisted)."""
    settings = get_settings()
    return settings.scenes_dir() / scene.id / "mesh"


def _mesh_obj_url(scene: Scene) -> str | None:
    if not scene.mesh_obj_path:
        return None
    # Always serve via the bundle route. Low-tier OBJs work fine
    # via this route too (single-file lookup; the MTL allowlist
    # entry just 404s when the low-tier didn't write one).
    return f"/api/scenes/{scene.id}/artifacts/mesh_assets/scene.obj"


def _mesh_glb_url(scene: Scene) -> str | None:
    if not scene.mesh_glb_path:
        return None
    return f"/api/scenes/{scene.id}/artifacts/mesh_assets/scene.glb"


def _mesh_tex_urls(scene: Scene) -> list[str] | None:
    """Enumerate ``scene_tex<N>.<ext>`` siblings under the scene's
    mesh dir, returning their canonical bundle URLs.

    Null when:
      * The scene has no completed mesh on disk (low-tier scenes
        produce a vertex-colored OBJ with no JPG sidecars).
      * The latest run was a tier other than ``standard``. Older
        low-tier extractions clean stale standard-tier sidecars on
        replace (see ``_run_low_tier``), but the explicit tier gate
        here is the belt-and-suspenders defence: if a future tier
        accidentally leaves orphaned ``scene_tex*`` files behind,
        we don't surface them to the web as if they were live.

    Globs both ``.jpg`` and ``.png`` since OpenMVS's TextureMesh
    can emit either depending on its build config. The artifact-
    route allowlist serves both extensions; this glob just feeds
    the SceneView URL list.

    We glob the dir rather than reading a stored ``mesh_tex_path``
    column because OpenMVS emits a variable number of texture
    pages (1 for small subjects, more for buildings); a persisted
    array would need migration each time we change the bundle
    shape. The glob is cheap (<10 files per mesh on average) and
    runs only on single-scene GETs / WS snapshots, not the home
    list path.
    """
    if not scene.mesh_obj_path:
        return None
    # Only the standard tier produces a texture-page bundle. A
    # low-tier vertex-colored OBJ from a scene that previously ran
    # standard tier would otherwise still pick up the cleaned-up
    # sidecars; this gate prevents the API from advertising
    # textures the new low-tier OBJ doesn't reference.
    tier = (scene.mesh_params or {}).get("tier")
    if tier != "standard":
        return None
    mesh_dir = _scene_mesh_dir(scene)
    if not mesh_dir.exists():
        return None
    tex_files = sorted(
        list(mesh_dir.glob("scene_tex*.jpg"))
        + list(mesh_dir.glob("scene_tex*.png"))
    )
    if not tex_files:
        return None
    return [
        f"/api/scenes/{scene.id}/artifacts/mesh_assets/{f.name}"
        for f in tex_files
    ]


async def _to_view(scene: Scene) -> SceneView:
    jobs = await store.list_jobs_for_scene(scene.id)
    jobs = _collapse_thumb_orbit_history(jobs)
    edit_status = (
        scene.edit_status.value
        if scene.edit_status is not None
        else EditStatus.none.value
    )
    mesh_status = (
        scene.mesh_status.value
        if scene.mesh_status is not None
        else MeshStatus.none.value
    )
    return SceneView(
        id=scene.id,
        capture_id=scene.capture_id,
        status=scene.status.value,
        error=scene.error,
        ply_url=f"/api/scenes/{scene.id}/artifacts/ply" if scene.ply_path else None,
        spz_url=f"/api/scenes/{scene.id}/artifacts/spz" if scene.spz_path else None,
        edited_ply_url=(
            f"/api/scenes/{scene.id}/artifacts/ply?edit=true"
            if scene.edited_ply_path
            else None
        ),
        edited_spz_url=(
            f"/api/scenes/{scene.id}/artifacts/spz?edit=true"
            if scene.edited_spz_path
            else None
        ),
        edit_status=edit_status,
        edit_error=scene.edit_error,
        edit_recipe=scene.edit_recipe,
        # The OBJ URL flips between the legacy ``/artifacts/obj``
        # (single-file FileResponse) and the new
        # ``/artifacts/mesh_assets/scene.obj`` (multi-file bundle
        # route) based on whether the latest mesh is standard-tier.
        # Standard-tier OBJs declare ``mtllib scene.mtl`` which the
        # browser resolves relative to the OBJ's URL — only the
        # bundle path puts MTL + JPGs at sibling URLs. Low-tier
        # vertex-colored OBJs have no MTL so either path works,
        # but we use the bundle path uniformly to simplify the web
        # client's URL handling.
        mesh_obj_url=_mesh_obj_url(scene),
        mesh_glb_url=_mesh_glb_url(scene),
        mesh_tex_urls=_mesh_tex_urls(scene),
        mesh_status=mesh_status,
        mesh_error=scene.mesh_error,
        mesh_params=scene.mesh_params,
        thumb_url=(
            f"/api/scenes/{scene.id}/artifacts/thumb"
            if scene.thumbnail_path
            else None
        ),
        orbit_url=(
            f"/api/scenes/{scene.id}/artifacts/orbit"
            if scene.orbit_path
            else None
        ),
        jobs=[
            JobView(
                id=j.id,
                kind=j.kind.value,
                status=j.status.value,
                progress=j.progress,
                progress_msg=j.progress_msg,
                error=j.error,
            )
            for j in jobs
        ],
        created_at=scene.created_at.isoformat(),
        completed_at=scene.completed_at.isoformat() if scene.completed_at else None,
    )


@router.get("/{scene_id}")
async def get_scene(scene_id: str) -> SceneView:
    scene = await store.get_scene(scene_id)
    if scene is None:
        raise HTTPException(404, "scene not found")
    return await _to_view(scene)


# Bundle file-name allowlist for the standard-tier mesh route below.
# OpenMVS emits ``scene.obj`` + ``scene.mtl`` + one or more
# ``scene_tex<N>.jpg`` texture atlas pages. The trimesh GLB pack adds
# ``scene.glb``. Restrict to that exact set so directory traversal is
# impossible and only canonical bundle pieces leak — the orchestrator's
# intermediates (``mesh.log``, ``.staging-*/``, ``colmap/``) stay
# scene-private.
#
# Texture-page index ``\d+`` not ``\d{1,2}`` — OpenMVS atlases a
# very large scene into 100+ pages and the OBJ's ``mtllib`` /
# MTL's ``map_Kd`` references span the full range. Capping at 2
# digits would 400 those higher-index fetches and break textured
# rendering for big captures.
_MESH_ASSET_RE = re.compile(
    r"^scene(\.obj|\.mtl|\.glb|_tex\d+\.(jpg|png))$"
)


@router.get("/{scene_id}/artifacts/mesh_assets/{filename}")
async def download_mesh_asset(scene_id: str, filename: str) -> Any:
    """Serve a file from the canonical ``scene_dir/mesh/`` bundle.

    The standard-tier (OpenMVS) mesh is a multi-file bundle (OBJ +
    MTL + N texture JPGs). Browsers resolve the MTL's ``map_Kd``
    references and the OBJ's ``mtllib`` directive relative to the
    request URL, so as long as every bundle file is served from the
    same path prefix the loader stitches them back together.

    Allowlist regex below blocks anything other than the canonical
    bundle filenames (``mesh.log``, ``.staging-*`` etc never leak).
    """
    scene = await store.get_scene(scene_id)
    if scene is None:
        raise HTTPException(404, "scene not found")
    if not _MESH_ASSET_RE.match(filename):
        raise HTTPException(400, f"unknown mesh asset {filename!r}")
    settings = get_settings()
    asset = settings.scenes_dir() / scene.id / "mesh" / filename
    if not asset.exists():
        raise HTTPException(404, f"mesh asset {filename!r} not yet produced")
    # Media type is mostly cosmetic — three.js loaders content-sniff
    # by URL suffix. Set a sensible one anyway so curl + browser
    # extension show the right preview.
    if filename.endswith(".obj"):
        media_type = "model/obj"
    elif filename.endswith(".mtl"):
        media_type = "text/plain"
    elif filename.endswith(".glb"):
        media_type = "model/gltf-binary"
    elif filename.endswith(".jpg"):
        media_type = "image/jpeg"
    elif filename.endswith(".png"):
        media_type = "image/png"
    else:
        media_type = "application/octet-stream"
    return FileResponse(asset, media_type=media_type, filename=filename)


@router.get("/{scene_id}/artifacts/{kind}")
async def download_artifact(scene_id: str, kind: str, edit: bool = False) -> Any:
    scene = await store.get_scene(scene_id)
    if scene is None:
        raise HTTPException(404, "scene not found")
    path: str | None
    media_type = "application/octet-stream"
    # Mesh artifacts (obj / glb) live on dedicated columns; ?edit
    # only applies to the splat (ply / spz). The mesh is reconstructed
    # from the trained checkpoint independently of any edits.
    if kind in ("obj", "glb"):
        if edit:
            raise HTTPException(400, "?edit=true is not valid for mesh artifacts")
        if kind == "obj":
            path = scene.mesh_obj_path
            filename = "scene.obj"
            media_type = "model/obj"
        else:
            path = scene.mesh_glb_path
            filename = "scene.glb"
            media_type = "model/gltf-binary"
        if not path or not Path(path).exists():
            raise HTTPException(404, f"mesh {kind!r} not yet produced")
        return FileResponse(path, media_type=media_type, filename=filename)
    # Thumbnail PNG produced by the post-export thumbnail job.
    # Same shape as mesh — dedicated column, no ?edit variant.
    # Returns 404 when the render hasn't run / failed; the web
    # CaptureCard then falls back to a chip-tinted gradient.
    if kind == "thumb":
        if edit:
            raise HTTPException(400, "?edit=true is not valid for the thumbnail")
        path = scene.thumbnail_path
        if not path or not Path(path).exists():
            raise HTTPException(404, "thumbnail not yet produced")
        return FileResponse(path, media_type="image/png", filename="thumb.png")
    # MP4 orbit produced by the post-thumbnail orbit job. Same
    # 404-when-absent shape as the still PNG — the CaptureCard
    # falls back to the still thumb when the orbit isn't ready,
    # and to the chip-tinted gradient if neither is.
    if kind == "orbit":
        if edit:
            raise HTTPException(400, "?edit=true is not valid for the orbit")
        path = scene.orbit_path
        if not path or not Path(path).exists():
            raise HTTPException(404, "orbit not yet produced")
        return FileResponse(path, media_type="video/mp4", filename="orbit.mp4")
    if edit:
        if kind == "ply":
            path = scene.edited_ply_path
            filename = "scene-edited.ply"
        elif kind == "spz":
            path = scene.edited_spz_path
            filename = "scene-edited.spz"
        else:
            raise HTTPException(400, f"unknown artifact kind {kind!r}")
        if not path or not Path(path).exists():
            raise HTTPException(404, f"edited {kind!r} not yet produced")
    else:
        if kind == "ply":
            path = scene.ply_path
            filename = "scene.ply"
        elif kind == "spz":
            path = scene.spz_path
            filename = "scene.spz"
        else:
            raise HTTPException(400, f"unknown artifact kind {kind!r}")
        if not path or not Path(path).exists():
            raise HTTPException(404, f"artifact {kind!r} not yet produced")
    return FileResponse(path, media_type=media_type, filename=filename)


# ─── edit recipe (filter pipeline) ─────────────────


class EditRequest(BaseModel):
    """User-authored cleanup recipe. The shape is validated by
    ``app.pipeline.filter.validate_recipe`` — keeping the server-side
    schema check in one place so the worker and api stay aligned."""
    recipe: dict


@router.put("/{scene_id}/edit")
async def upsert_edit(scene_id: str, body: EditRequest) -> SceneView:
    """Replace the scene's edit recipe and (re)enqueue a filter job.

    Idempotent — re-PUTting cancels any in-flight filter job for this
    scene, swaps in the new recipe, and enqueues a fresh job. We
    DON'T touch the existing edited artifacts on disk yet; the new
    job overwrites them on success and leaves the previous edit
    accessible until then.
    """
    scene = await store.get_scene(scene_id)
    if scene is None:
        raise HTTPException(404, "scene not found")
    if not scene.ply_path:
        raise HTTPException(409, "scene has no source splat to edit yet")

    try:
        recipe = validate_recipe(body.recipe)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc

    # Cancel any in-flight filter job for this scene before queueing
    # a new one. Otherwise two concurrent filter jobs would race on
    # the same edit/ output dir.
    for j in await store.list_jobs_for_scene(scene.id):
        if j.kind != JobKind.filter:
            continue
        if j.status in (JobStatus.queued, JobStatus.claimed, JobStatus.running):
            if await store.cancel_job(j.id):
                await events.publish_job(j.id, "job.canceled")

    # Drop terminal filter rows so the scene's job list converges on
    # a single, canonical filter row rather than accumulating one per
    # apply. The web pipeline UI keys off scene.jobs and would
    # otherwise grow a "filter / filter / filter / …" list as the
    # user iterates on the recipe.
    await store.delete_terminal_jobs_of_kind(scene.id, JobKind.filter)

    await store.update_scene(
        scene.id,
        edit_recipe=recipe,
        edit_status=EditStatus.queued,
        edit_error=None,
    )
    job = await store.enqueue_job(scene.id, JobKind.filter, payload={})
    if job is None:
        # Scene was deleted out from under us between get_scene
        # and the enqueue. Surface 404 rather than silently no-op.
        raise HTTPException(404, "scene was deleted")
    # Just the bare event — the client only uses scene.edit_queued
    # to flip its local edit_status and re-fetch the snapshot, so
    # broadcasting the recipe (which can be MB-scale once
    # keep_indices lands) is pure waste on the WS hot path.
    await events.publish_scene(
        scene.id, "scene.edit_queued", job_id=job.id,
    )

    refreshed = await store.get_scene(scene.id)
    assert refreshed is not None
    return await _to_view(refreshed)


@router.delete("/{scene_id}/edit")
async def clear_edit(scene_id: str) -> SceneView:
    """Cancel any in-flight filter job, remove the edit artifacts,
    and null the recipe. The original splat is untouched."""
    scene = await store.get_scene(scene_id)
    if scene is None:
        raise HTTPException(404, "scene not found")

    for j in await store.list_jobs_for_scene(scene.id):
        if j.kind != JobKind.filter:
            continue
        if j.status in (JobStatus.queued, JobStatus.claimed, JobStatus.running):
            if await store.cancel_job(j.id):
                await events.publish_job(j.id, "job.canceled")

    settings = get_settings()
    edit_dir = settings.scenes_dir() / scene.id / "edit"
    if edit_dir.exists():
        shutil.rmtree(edit_dir, ignore_errors=True)

    await store.update_scene(
        scene.id,
        edited_ply_path=None,
        edited_spz_path=None,
        edit_recipe=None,
        edit_status=EditStatus.none,
        edit_error=None,
    )
    await events.publish_scene(scene.id, "scene.edit_cleared")

    # Regen the thumbnail / orbit pair against the ORIGINAL splat.
    # Without this, the home grid would keep showing the filtered
    # thumb after the user discarded their edit (Scene.thumbnail_path
    # / Scene.orbit_path still point at the filter-regen renders on
    # disk). The regen routes through ns-render against the
    # splatfacto checkpoint at scene.ply_path — same code path the
    # first post-export thumbnail used — so the output is byte-
    # identical to the original thumbnail (just rebuilt). The
    # helper also cancels any in-flight thumbnail / orbit jobs first
    # so a stale filter-regen finishing after this enqueue can't
    # overwrite the freshly-restored original thumbnail.
    #
    # Skip the regen when there's no original ply yet (capture is
    # mid-pipeline, edge case for filter applied to a stub scene).
    if scene.ply_path:
        try:
            await thumb_regen.enqueue_regen(scene.id, use_edited_ply=False)
        except Exception:
            log.exception(
                "edit-clear %s: failed to enqueue thumbnail regen "
                "(home grid will keep showing the filtered thumb "
                "until next post-export render)",
                scene.id,
            )

    refreshed = await store.get_scene(scene.id)
    assert refreshed is not None
    return await _to_view(refreshed)


# ─── mesh extraction (Poisson via ns-export) ────────


class MeshRequest(BaseModel):
    """Optional override for the Poisson extraction params. Anything
    omitted falls back to ``mesh.DEFAULT_PARAMS``; unknown keys are
    rejected so a typo doesn't silently no-op."""
    params: dict | None = None


_ALLOWED_MESH_KEYS = set(MESH_DEFAULT_PARAMS.keys())
# Open3D's PCA-based normal estimator is the only one we support
# end-to-end. The previous ``model_output`` option was a holdover
# from the ns-export-poisson era; even on our open3d-direct path
# it can only fall back to PCA for splatfacto-trained scenes
# (gaussians don't carry meaningful surface normals; the PLY's
# nx/ny/nz are written but always zero). Keeping a single value
# here so the UI choice never silently no-ops.
_ALLOWED_NORMAL_METHODS = {"open3d"}
# Mesh tier selector. Only ``low`` (TSDF fusion) is implemented at
# the API today; ``standard`` (OpenMVS textured) and ``higher``
# (2DGS / SuGaR retrain) are reserved for future PRs. We accept
# them in validation so a forward-rolled web client can submit the
# parameter, but the worker dispatcher (mesh.py:_run_mesh) raises
# ``NotImplementedError`` until those backends land. The UI shows
# the latter two as "coming soon" non-clickable chips for now.
_ALLOWED_MESH_TIERS = {"low", "standard", "higher"}
# Tiers we'll actually accept at the trigger endpoint today.
# Anything else returns 422 with a clear message rather than
# silently downgrading.
_ACTIVE_MESH_TIERS = {"low", "standard", "higher"}
# OpenMVS's TextureMesh accepts the texture-atlas page size as a
# power of 2. Constraining the API set to these four values keeps
# the UI's chip-row tractable and avoids feeding a non-power-of-2
# down to a binary that may round it silently.
_ALLOWED_MVS_TEXTURE_SIZES = {1024, 2048, 4096, 8192}


def _validate_mesh_params(raw: dict | None) -> dict:
    """Strict per-key type / range check.

    Trusting the worker to coerce these would silently misinterpret
    common JSON mistakes (``"false"`` is truthy in Python's ``bool``,
    ``"1e6"`` parses but loses precision through ``int``). Fail fast
    at the api boundary so the user gets a clear 422 instead of a
    confusing job failure twenty seconds later.
    """
    if raw is None:
        return {}
    if not isinstance(raw, dict):
        raise HTTPException(422, "params must be a JSON object")
    bad = set(raw.keys()) - _ALLOWED_MESH_KEYS
    if bad:
        raise HTTPException(
            422,
            f"unknown mesh param(s) {sorted(bad)}; allowed: {sorted(_ALLOWED_MESH_KEYS)}",
        )
    out: dict = {}
    if "num_points" in raw:
        v = raw["num_points"]
        # Reject bools explicitly — bool is a subclass of int in
        # Python, so ``isinstance(True, int)`` is True and the
        # subsequent range check would happily accept it.
        if isinstance(v, bool) or not isinstance(v, int) or v <= 0:
            raise HTTPException(422, "num_points must be a positive integer")
        out["num_points"] = v
    if "remove_outliers" in raw:
        v = raw["remove_outliers"]
        if not isinstance(v, bool):
            raise HTTPException(422, "remove_outliers must be a boolean")
        out["remove_outliers"] = v
    if "normal_method" in raw:
        v = raw["normal_method"]
        if not isinstance(v, str) or v not in _ALLOWED_NORMAL_METHODS:
            raise HTTPException(
                422,
                f"normal_method must be one of {sorted(_ALLOWED_NORMAL_METHODS)}",
            )
        out["normal_method"] = v
    if "use_bounding_box" in raw:
        v = raw["use_bounding_box"]
        if not isinstance(v, bool):
            raise HTTPException(422, "use_bounding_box must be a boolean")
        out["use_bounding_box"] = v
    if "depth" in raw:
        v = raw["depth"]
        # Open3D's screened-Poisson recommends depth in [5, 12]; the
        # cost goes up roughly 8x per step. Allow the documented
        # range and refuse anything weirder so the worker doesn't
        # OOM on a "depth": 20 typo.
        if isinstance(v, bool) or not isinstance(v, int) or v < 5 or v > 12:
            raise HTTPException(
                422, "depth must be an integer in [5, 12]",
            )
        out["depth"] = v
    if "density_quantile" in raw:
        v = raw["density_quantile"]
        # Bool→float coercion would let "false" → 0.0 through
        # silently; reject bools explicitly. Quantile is a
        # probability in [0, 1).
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise HTTPException(422, "density_quantile must be a number")
        if v < 0 or v >= 1:
            raise HTTPException(422, "density_quantile must be in [0, 1)")
        out["density_quantile"] = float(v)
    # ─── TSDF-tier knobs ────────────────────────────────────────
    if "tier" in raw:
        v = raw["tier"]
        if not isinstance(v, str) or v not in _ALLOWED_MESH_TIERS:
            raise HTTPException(
                422,
                f"tier must be one of {sorted(_ALLOWED_MESH_TIERS)}",
            )
        if v not in _ACTIVE_MESH_TIERS:
            # Reject standard / higher at the trigger endpoint
            # explicitly. The dispatcher raises the same way, but
            # rejecting here keeps the queued/running churn out of
            # the pipeline list — and gives the UI a clean 422 to
            # surface.
            raise HTTPException(
                422,
                f"mesh tier '{v}' is not yet implemented; "
                f"active tiers: {sorted(_ACTIVE_MESH_TIERS)}",
            )
        out["tier"] = v
    if "n_views" in raw:
        v = raw["n_views"]
        if isinstance(v, bool) or not isinstance(v, int) or v < 24 or v > 360:
            raise HTTPException(
                422, "n_views must be an integer in [24, 360]",
            )
        out["n_views"] = v
    if "view_elevations" in raw:
        v = raw["view_elevations"]
        if not isinstance(v, list) or not (1 <= len(v) <= 4):
            raise HTTPException(
                422,
                "view_elevations must be a list of 1–4 floats in [-1, 1]",
            )
        norm: list[float] = []
        for x in v:
            if isinstance(x, bool) or not isinstance(x, (int, float)):
                raise HTTPException(
                    422, "view_elevations entries must be numbers",
                )
            if x < -1 or x > 1:
                raise HTTPException(
                    422, "view_elevations entries must be in [-1, 1]",
                )
            norm.append(float(x))
        out["view_elevations"] = norm
    if "voxel_size" in raw:
        v = raw["voxel_size"]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise HTTPException(422, "voxel_size must be a number")
        if v <= 0 or v > 0.1:
            raise HTTPException(422, "voxel_size must be in (0, 0.1]")
        out["voxel_size"] = float(v)
    if "sdf_trunc_mult" in raw:
        v = raw["sdf_trunc_mult"]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise HTTPException(422, "sdf_trunc_mult must be a number")
        if v < 1 or v > 8:
            raise HTTPException(422, "sdf_trunc_mult must be in [1, 8]")
        out["sdf_trunc_mult"] = float(v)
    if "depth_trunc" in raw:
        v = raw["depth_trunc"]
        # ``depth`` (octree) and ``depth_trunc`` (scene-relative
        # ray-depth cap) are different params with overlapping
        # names; we check ``isinstance(v, float)`` below the int
        # branch so an int ``depth_trunc`` accepts too.
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise HTTPException(422, "depth_trunc must be a number")
        if v < 1 or v > 50:
            raise HTTPException(422, "depth_trunc must be in [1, 50]")
        out["depth_trunc"] = float(v)
    # ─── standard-tier (OpenMVS) knobs ──────────────────────────
    if "mvs_dense_views" in raw:
        v = raw["mvs_dense_views"]
        if isinstance(v, bool) or not isinstance(v, int) or v < 2 or v > 7:
            raise HTTPException(
                422, "mvs_dense_views must be an integer in [2, 7]",
            )
        out["mvs_dense_views"] = v
    if "mvs_texture_size" in raw:
        v = raw["mvs_texture_size"]
        # Restrict to powers-of-2 OpenMVS actually accepts; allow
        # int input but not bool.
        if isinstance(v, bool) or not isinstance(v, int):
            raise HTTPException(422, "mvs_texture_size must be an integer")
        if v not in _ALLOWED_MVS_TEXTURE_SIZES:
            raise HTTPException(
                422,
                f"mvs_texture_size must be one of "
                f"{sorted(_ALLOWED_MVS_TEXTURE_SIZES)}",
            )
        out["mvs_texture_size"] = v
    if "mvs_refine_iters" in raw:
        v = raw["mvs_refine_iters"]
        # 0 is the documented opt-out (skip RefineMesh entirely).
        if isinstance(v, bool) or not isinstance(v, int) or v < 0 or v > 4:
            raise HTTPException(
                422, "mvs_refine_iters must be an integer in [0, 4]",
            )
        out["mvs_refine_iters"] = v
    # ─── low-tier (TSDF) quality knobs ─────────────────────────
    if "use_edited_splat" in raw:
        v = raw["use_edited_splat"]
        if not isinstance(v, bool):
            raise HTTPException(422, "use_edited_splat must be a boolean")
        out["use_edited_splat"] = v
    if "alpha_min" in raw:
        v = raw["alpha_min"]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise HTTPException(422, "alpha_min must be a number")
        if v < 0 or v > 1:
            raise HTTPException(422, "alpha_min must be in [0, 1]")
        out["alpha_min"] = float(v)
    if "bbox_percentile_low" in raw or "bbox_percentile_high" in raw:
        lo_v = raw.get("bbox_percentile_low", 10.0)
        hi_v = raw.get("bbox_percentile_high", 90.0)
        for label, val in (
            ("bbox_percentile_low", lo_v),
            ("bbox_percentile_high", hi_v),
        ):
            if isinstance(val, bool) or not isinstance(val, (int, float)):
                raise HTTPException(422, f"{label} must be a number")
            if val < 0 or val > 100:
                raise HTTPException(422, f"{label} must be in [0, 100]")
        if float(lo_v) >= float(hi_v):
            raise HTTPException(
                422,
                "bbox_percentile_low must be strictly less than bbox_percentile_high",
            )
        if "bbox_percentile_low" in raw:
            out["bbox_percentile_low"] = float(lo_v)
        if "bbox_percentile_high" in raw:
            out["bbox_percentile_high"] = float(hi_v)
    if "floater_opacity_min" in raw:
        v = raw["floater_opacity_min"]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise HTTPException(422, "floater_opacity_min must be a number")
        if v < 0 or v > 1:
            raise HTTPException(422, "floater_opacity_min must be in [0, 1]")
        out["floater_opacity_min"] = float(v)
    if "floater_scale_max_pct" in raw:
        v = raw["floater_scale_max_pct"]
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            raise HTTPException(422, "floater_scale_max_pct must be a number")
        if v < 0 or v > 100:
            raise HTTPException(
                422, "floater_scale_max_pct must be in [0, 100]",
            )
        out["floater_scale_max_pct"] = float(v)
    # ─── higher-tier (2DGS retrain) knobs ───────────────────────
    if "higher_train_iters" in raw:
        v = raw["higher_train_iters"]
        # Range is wide on purpose — power users may push past
        # the default 10k for very-large captures, or pull down to
        # 2k for a fast smoke test. Anything above ~30k just
        # consumes wall time without measurable surface
        # improvement (2DGS paper's published sweep plateaus at
        # ~15k for similar-sized scenes).
        if isinstance(v, bool) or not isinstance(v, int) or v < 2_000 or v > 30_000:
            raise HTTPException(
                422, "higher_train_iters must be an integer in [2000, 30000]",
            )
        out["higher_train_iters"] = v
    return out


@router.post("/{scene_id}/mesh")
async def trigger_mesh(scene_id: str, body: MeshRequest | None = None) -> SceneView:
    """Kick off Poisson mesh extraction for this scene.

    Idempotent in spirit: re-posting cancels any in-flight mesh job,
    swaps the params, and enqueues a fresh extraction. Existing mesh
    artefacts on disk are left in place until the new run overwrites
    them; that way a running re-extract doesn't temporarily 404 the
    download link the user just clicked.
    """
    scene = await store.get_scene(scene_id)
    if scene is None:
        raise HTTPException(404, "scene not found")
    if scene.status.value != "completed":
        raise HTTPException(
            409,
            "scene pipeline is not complete; mesh needs the trained checkpoint",
        )

    params = _validate_mesh_params(body.params if body else None)

    # If the request omits ``tier`` (or omits ``params`` entirely),
    # the runner will fall back to the persisted ``scene.mesh_params``.
    # That row may carry an inactive tier (forward-rolled by a future
    # client, hand-edited via direct DB access, or persisted from a
    # window where the tier surface changed). Catch that here so the
    # job never queues — otherwise it'd run, fail in the worker with
    # ``NotImplementedError``, and pollute the pipeline list with
    # exactly the queued/running churn the validator is meant to
    # prevent. Same 422 + message shape as the validator's tier
    # check so the UI surface is uniform.
    if "tier" not in params:
        persisted_tier = (scene.mesh_params or {}).get("tier")
        if persisted_tier and persisted_tier not in _ACTIVE_MESH_TIERS:
            raise HTTPException(
                422,
                f"persisted mesh tier '{persisted_tier}' is not yet "
                f"implemented; submit ``params: {{\"tier\": \"low\"}}`` "
                f"to override. active tiers: {sorted(_ACTIVE_MESH_TIERS)}",
            )

    for j in await store.list_jobs_for_scene(scene.id):
        if j.kind != JobKind.mesh:
            continue
        if j.status in (JobStatus.queued, JobStatus.claimed, JobStatus.running):
            if await store.cancel_job(j.id):
                await events.publish_job(j.id, "job.canceled")

    # Same canonical-row dance as filter: drop completed/failed (and
    # acked-canceled) prior mesh rows so the pipeline list stays
    # tidy.
    await store.delete_terminal_jobs_of_kind(scene.id, JobKind.mesh)

    await store.update_scene(
        scene.id,
        mesh_params=params or scene.mesh_params,
        mesh_status=MeshStatus.queued,
        mesh_error=None,
    )
    job = await store.enqueue_job(scene.id, JobKind.mesh, payload={})
    if job is None:
        raise HTTPException(404, "scene was deleted")
    await events.publish_scene(scene.id, "scene.mesh_queued", job_id=job.id)

    refreshed = await store.get_scene(scene.id)
    assert refreshed is not None
    return await _to_view(refreshed)


@router.delete("/{scene_id}/mesh")
async def clear_mesh(scene_id: str) -> SceneView:
    """Cancel any in-flight mesh job and remove the mesh artefacts."""
    scene = await store.get_scene(scene_id)
    if scene is None:
        raise HTTPException(404, "scene not found")

    for j in await store.list_jobs_for_scene(scene.id):
        if j.kind != JobKind.mesh:
            continue
        if j.status in (JobStatus.queued, JobStatus.claimed, JobStatus.running):
            if await store.cancel_job(j.id):
                await events.publish_job(j.id, "job.canceled")

    settings = get_settings()
    mesh_dir = settings.scenes_dir() / scene.id / "mesh"
    if mesh_dir.exists():
        shutil.rmtree(mesh_dir, ignore_errors=True)

    await store.update_scene(
        scene.id,
        mesh_obj_path=None,
        mesh_glb_path=None,
        mesh_status=MeshStatus.none,
        mesh_error=None,
    )
    await events.publish_scene(scene.id, "scene.mesh_cleared")

    refreshed = await store.get_scene(scene.id)
    assert refreshed is not None
    return await _to_view(refreshed)


@router.websocket("/{scene_id}/events")
async def scene_events_endpoint(ws: WebSocket, scene_id: str) -> None:
    scene = await store.get_scene(scene_id)
    if scene is None:
        await ws.close(code=4404, reason="scene not found")
        return
    await ws.accept()

    topic = f"scene.{scene.id}"
    queue = await events.subscribe(topic)
    job_queues = []
    try:
        view = await _to_view(scene)
        await ws.send_text(
            json.dumps({"topic": topic, "kind": "snapshot", "data": view.model_dump()})
        )
        for j in await store.list_jobs_for_scene(scene.id):
            job_topic = f"job.{j.id}"
            jq = await events.subscribe(job_topic)
            job_queues.append((job_topic, jq))

        async def fwd(q):
            while True:
                evt = await q.get()
                await ws.send_text(evt.to_json())

        tasks = [asyncio.create_task(fwd(queue))] + [
            asyncio.create_task(fwd(jq)) for _, jq in job_queues
        ]
        try:
            done, pending = await asyncio.wait(
                tasks, return_when=asyncio.FIRST_EXCEPTION
            )
            for t in pending:
                t.cancel()
        except WebSocketDisconnect:
            pass
    finally:
        await events.unsubscribe(topic, queue)
        for jt, jq in job_queues:
            await events.unsubscribe(jt, jq)

"""Scene HTTP + event-WebSocket routes."""
from __future__ import annotations

import asyncio
import json
import logging
import shutil
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from pydantic import BaseModel

from app.config import get_settings
from app.jobs import events, store
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
    mesh_status: str
    mesh_error: str | None
    mesh_params: dict | None
    # PNG thumbnail of the trained splat, rendered post-export.
    # Null when the scene hasn't reached the thumbnail step yet,
    # the render failed, or the scene is a stub. Web ``CaptureCard``
    # falls back to a chip-tinted gradient placeholder when null.
    thumb_url: str | None
    jobs: list[JobView]
    created_at: str
    completed_at: str | None


async def _to_view(scene: Scene) -> SceneView:
    jobs = await store.list_jobs_for_scene(scene.id)
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
        mesh_obj_url=(
            f"/api/scenes/{scene.id}/artifacts/obj"
            if scene.mesh_obj_path
            else None
        ),
        mesh_glb_url=(
            f"/api/scenes/{scene.id}/artifacts/glb"
            if scene.mesh_glb_path
            else None
        ),
        mesh_status=mesh_status,
        mesh_error=scene.mesh_error,
        mesh_params=scene.mesh_params,
        thumb_url=(
            f"/api/scenes/{scene.id}/artifacts/thumb"
            if scene.thumbnail_path
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

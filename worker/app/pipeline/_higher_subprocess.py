"""2DGS retrain + textured-mesh subprocess — higher-tier orchestrator.

Drives the surface-aware retrain + mesh extraction for the higher
tier:

    1. Load splat PLY → flat-surfel 2DGS initialisation (zero out
       smallest scale axis, keep two principal axes).
    2. Retrain via gsplat's native 2DGS rasterizer
       (``rasterization_2dgs``) with 2DGS losses (RGB + depth-
       distortion + normal-consistency).
    3. Render an N-view dome of RGB + depth + normal frames from
       the trained 2DGS model.
    4. TSDF fusion through Open3D's ``ScalableTSDFVolume`` →
       marching cubes → triangle mesh with TRUE surface normals
       (the 2DGS surfels make depth geometrically meaningful, so
       Open3D's marching cubes inherits correct normals).
    5. UV unwrap the mesh via xatlas, project the captured frames
       onto the atlas, and bake a per-page JPG texture set.
    6. Write the canonical ``scene.{obj,mtl,glb}`` + ``scene_tex<N>.jpg``
       bundle the standard-tier already established. The web's
       mesh-mode SplatViewer (MTLLoader pre-pass) renders this the
       same way it renders the standard-tier output — no UI churn.

Why 2DGS via gsplat (not the upstream ``hbb1/2d-gaussian-splatting``
or SuGaR / GOF / PGSR / RaDe-GS reference repos): every one of
those inherits Inria's non-commercial Gaussian-Splatting license.
gsplat 1.4.0 (Apache-2.0, already in the worker image)
reimplements the 2DGS rasterizer in CUDA — we can drive it
directly without taking on a non-commercial dep.

Spawned by ``mesh.py``'s ``_run_higher`` dispatcher with
``start_new_session=True`` + ``_running.register(..., pgid=True)``
so a user cancel SIGKILLs the entire torch process group, not
just this orchestrator (a multi-minute retrain holding a CUDA
context would otherwise survive a cancel and starve the worker).

Usage::

    python -m app.pipeline._higher_subprocess \\
        --src-ply <path> \\
        --transforms-json <path> \\
        --images-src-dir <path> \\
        --staging-dir <path> \\
        --params '<json>'

Outputs (stdout, line-buffered):
    ``PROGRESS <fraction> <message>`` lines for the parent's
    progress callback. All other lines are arbitrary log text
    the parent appends to ``mesh.log`` verbatim.
"""
from __future__ import annotations

import argparse
import json
import math
import os
import sys
from pathlib import Path

import numpy as np

from app.pipeline import ply_render
from app.pipeline.thumbnail import _look_at, _ply_bbox


def _emit(progress: float, msg: str) -> None:
    p = max(0.0, min(1.0, float(progress)))
    print(f"PROGRESS {p:.4f} {msg}", flush=True)


def _log(msg: str) -> None:
    print(msg, flush=True)


# Camera framing matches the orbit / low-tier mesh path so the 2DGS
# retrain sees the same FOV the user captured at (the SfM frames
# drive the per-frame intrinsics directly; this constant only
# governs the dome render at TSDF time).
_DOME_FOV_DEG = 50.0
_DOME_W = 720
_DOME_H = 720
# Atlas page size for the texture bake. Power of 2; xatlas
# packs the unwrapped charts into one (or more) pages of this
# size. Matches OpenMVS's mid-default — bumping to 8192 doubles
# the bake time and download size with diminishing visual return
# at ≤4K capture resolution.
_TEX_PAGE_PX = 4096


def _dome_camera_path(
    centroid, extent, *, n_views: int, view_elevations,
):
    """Identical shape to ``_mesh_subprocess._dome_camera_path``,
    duplicated here so the higher-tier subprocess doesn't import
    the low-tier one (the latter eagerly imports Open3D at module
    top, which we delay until the integrate step)."""
    cx, cy, cz = centroid
    fov_rad = math.radians(_DOME_FOV_DEG)
    fit_distance = (extent / max(0.01, math.tan(fov_rad * 0.5))) * 1.4
    distance = max(fit_distance, extent * 2.0, 1.5)
    n_per_ring = max(1, n_views // max(1, len(view_elevations)))
    poses = []
    for elev_frac in view_elevations:
        phi = max(-1.0, min(1.0, float(elev_frac))) * (math.pi / 2.0)
        horizontal = distance * math.cos(phi)
        vertical = distance * math.sin(phi)
        for i in range(n_per_ring):
            theta = 2.0 * math.pi * i / n_per_ring
            eye = (
                cx + horizontal * math.sin(theta),
                cy + vertical,
                cz + horizontal * math.cos(theta),
            )
            target = (cx, cy, cz)
            poses.append(_look_at(eye, target, up=(0.0, 1.0, 0.0)))
    return poses


def _flat_surfel_init(prepared):
    """Convert the splatfacto ``prepared`` tensors into 2DGS-style
    flat surfels by zeroing the smallest scale axis per gaussian.

    The 2DGS paper trains from random init; the surfel-from-3DGS
    initialisation here is the standard "SuGaR-style warm start"
    that lets us converge in ~10k iters instead of 30k. For each
    gaussian we keep the two largest scale axes (the disk plane)
    and zero the smallest (the disk thickness — flat). This is a
    pure tensor op, no CUDA kernel needed.
    """
    import torch

    scales = prepared["scales"]
    # ``scales`` is (N, 3). Find the index of the smallest axis
    # per gaussian, then build a multiplicative mask that zeros it.
    smallest = scales.argmin(dim=-1)  # (N,)
    n = scales.shape[0]
    mask = torch.ones_like(scales)
    mask[torch.arange(n, device=scales.device), smallest] = 0.0
    new_scales = scales * mask
    # Clamp the remaining axes to a small floor so the rasterizer
    # doesn't hit a degenerate disk. 1e-4 matches the splatfacto
    # min-scale guard.
    new_scales = new_scales.clamp(min=1e-4)
    return {**prepared, "scales": new_scales}


def _load_transforms(transforms_path: Path, images_src_dir: Path):
    """Read nerfstudio ``transforms.json`` into the per-frame data
    the 2DGS retrain needs: c2w (OpenGL), the image file path the
    rasterization step compares against, and the shared intrinsics.

    Returns a tuple ``(frames, intrinsics)``:
      * ``frames`` is a list of ``(c2w_opengl_flat16, image_path)``
        tuples in the order they appear in transforms.json.
      * ``intrinsics`` is a dict with ``fx``, ``fy``, ``cx``, ``cy``,
        ``w``, ``h``.
    """
    data = json.loads(transforms_path.read_text())
    frames = []
    for frame in data.get("frames", []):
        c2w = list(np.asarray(
            frame["transform_matrix"], dtype=np.float32,
        ).flatten())
        basename = Path(frame["file_path"]).name
        img = images_src_dir / basename
        if not img.exists():
            continue
        frames.append((c2w, img))
    intrinsics = {
        "fx": float(data["fl_x"]),
        "fy": float(data["fl_y"]),
        "cx": float(data["cx"]),
        "cy": float(data["cy"]),
        "w": int(data["w"]),
        "h": int(data["h"]),
    }
    return frames, intrinsics


def _load_image_tensor(img_path: Path, *, device):
    """Load a JPG as ``(H, W, 3)`` float32 in [0, 1] on ``device``.

    Read once via Pillow (already in the worker image), uploaded
    once per training step. Frames are pre-resized inside the
    rasterizer to match the requested w/h, but reading at the
    transforms.json's native resolution keeps the appearance loss
    on the same scale the SfM camera was calibrated for.
    """
    import torch
    from PIL import Image

    img = Image.open(img_path).convert("RGB")
    arr = np.asarray(img, dtype=np.float32) / 255.0
    return torch.from_numpy(arr).to(device)


def _train_2dgs(*, prepared, frames, intrinsics, n_iters: int):
    """Retrain the splat into 2DGS using gsplat's native 2DGS
    rasterizer + the 2DGS paper's three losses (RGB + depth
    distortion + normal consistency).

    Returns the updated ``prepared`` dict (means/quats/scales/
    opacities/rgb tensors with grad disabled). The retrain runs
    against random batches of 1 frame per step; the dome render
    + TSDF step downstream consumes the final tensors.

    The loss-weight defaults match the 2DGS paper's table 1:
    distortion 100×, normal 0.05×. We don't expose those as knobs
    because they're not user-tunable in a meaningful way — the
    paper authors swept them already and the resulting surface
    quality is monotonic in iters at the published mix.
    """
    import torch
    from gsplat.rendering import rasterization_2dgs

    device = prepared["device"]
    means = prepared["means"].clone().detach().requires_grad_(True)
    quats = prepared["quats"].clone().detach().requires_grad_(True)
    scales = prepared["scales"].clone().detach().requires_grad_(True)
    opacities = prepared["opacities"].clone().detach().requires_grad_(True)
    rgb = prepared["rgb"].clone().detach().requires_grad_(True)
    flip_yz = prepared["flip_yz"]

    # Per-param-group learning rates — same shape splatfacto uses,
    # rescaled for the shorter retrain. Means get the smallest LR
    # (they're already close to surface); scales + opacities get
    # the largest (we want them to deform toward the surface
    # quickly).
    opt = torch.optim.Adam([
        {"params": [means], "lr": 1.6e-5},
        {"params": [quats], "lr": 1e-3},
        {"params": [scales], "lr": 5e-3},
        {"params": [opacities], "lr": 5e-2},
        {"params": [rgb], "lr": 2.5e-3},
    ])

    w, h = intrinsics["w"], intrinsics["h"]
    fx, fy = intrinsics["fx"], intrinsics["fy"]
    cx, cy = intrinsics["cx"], intrinsics["cy"]
    K = torch.tensor(
        [[fx, 0.0, cx], [0.0, fy, cy], [0.0, 0.0, 1.0]],
        device=device, dtype=torch.float32,
    ).unsqueeze(0)

    # Loss weights from the 2DGS paper.
    LAMBDA_DIST = 100.0
    LAMBDA_NORM = 0.05

    last_pct = -1.0
    n_frames = len(frames)
    for it in range(n_iters):
        idx = int(torch.randint(0, n_frames, (1,)).item())
        c2w_flat, img_path = frames[idx]
        c2w = torch.tensor(c2w_flat, device=device, dtype=torch.float32).view(1, 4, 4)
        c2w_opencv = c2w @ flip_yz
        viewmats = torch.linalg.inv(c2w_opencv)

        # Lazy load + upload per step. For 200 frames at 1080p this
        # is ~4 MB/step which the CPU→GPU bus handles comfortably;
        # batching to GPU upfront would consume ~800 MB which is
        # plenty of headroom but not worth the eager-load cost.
        gt = _load_image_tensor(img_path, device=device)
        # gsplat expects images at (w, h) matching the K above —
        # if the GT image has different dims, that's a SfM /
        # capture-step bug to fix upstream, not here. Trust the
        # transforms.json's intrinsics.
        if gt.shape[0] != h or gt.shape[1] != w:
            # Bilinear resize as a defensive guard for the rare
            # legacy capture where transforms.json's w/h doesn't
            # match the on-disk JPGs. Logged loudly on first iter.
            if it == 0:
                _log(
                    f"WARN: gt {gt.shape[:2]} mismatches intrinsics "
                    f"{(h, w)}; resizing per-step"
                )
            gt = torch.nn.functional.interpolate(
                gt.permute(2, 0, 1).unsqueeze(0),
                size=(h, w), mode="bilinear", align_corners=False,
            ).squeeze(0).permute(1, 2, 0)

        try:
            # ``render_mode="RGB+ED"`` populates the per-pixel
            # expected-depth + the depth-derived normal channel
            # the normal-consistency loss compares against; without
            # it the function returns RGB-only and the regularizers
            # below silently skip (training collapses to plain RGB
            # L1, which defeats the surface-aware retraining goal).
            # ``distloss=True`` toggles gsplat to compute + return
            # the depth-distortion regularizer with grad attached
            # so the optimizer can drive it down.
            render = rasterization_2dgs(
                means=means,
                quats=quats / quats.norm(dim=-1, keepdim=True).clamp(min=1e-8),
                scales=scales.clamp(min=1e-4),
                opacities=opacities.clamp(0.0, 1.0),
                colors=rgb.clamp(0.0, 1.0),
                viewmats=viewmats,
                Ks=K,
                width=w,
                height=h,
                render_mode="RGB+ED",
                distloss=True,
            )
        except Exception as exc:
            _log(f"ERROR: rasterization_2dgs call failed at it={it}: {exc}")
            return None

        # With ``render_mode="RGB+ED"`` + ``distloss=True`` the
        # rasterizer's documented return tuple is exactly 7-wide:
        #   (render_rgb, alphas, normals, normals_from_depth,
        #    render_distort, render_median, meta)
        # Earlier code tried to "defensively" unpack shorter
        # variants, but for ``len(ret) < 7`` the positional
        # indices misroute channels — ``distort`` would fall back
        # to ret[3] which is actually ``normals_from_depth`` in
        # the same ordering, silently optimizing normals as
        # distortion. Be strict: require the 7-element shape,
        # raise loudly otherwise. The kwargs above guarantee it on
        # gsplat 1.4.x; a future API drift should fail visibly
        # rather than corrupt training.
        ret = list(render)
        if len(ret) < 7:
            _log(
                f"ERROR: rasterization_2dgs returned {len(ret)} "
                f"elements; expected 7 with render_mode='RGB+ED' "
                f"and distloss=True. gsplat API may have changed."
            )
            return None
        render_rgb = ret[0]
        if render_rgb.ndim == 4:
            render_rgb = render_rgb[0]
        # ``render_mode="RGB+ED"`` packs the per-pixel expected
        # depth as a 4th channel of ``render_colors``, so the
        # tensor that comes back is (H, W, 4) — RGB in [..., :3]
        # and ED in [..., 3:4]. Slicing off the depth channel
        # before the RGB L1 loss prevents the "tensor a (4) must
        # match tensor b (3)" crash we hit on a real higher-tier
        # run (the ED is also exposed separately via ret[5] for
        # the dome / TSDF integration downstream).
        if render_rgb.shape[-1] == 4:
            render_rgb = render_rgb[..., :3]
        normals = ret[2]
        normals_depth = ret[3]
        distort = ret[4]

        loss_rgb = (render_rgb - gt).abs().mean()
        loss = loss_rgb
        if distort is not None:
            loss = loss + LAMBDA_DIST * distort.mean()
        if normals is not None and normals_depth is not None:
            # Normal consistency: rendered surfel normal vs the
            # depth-image normal. 1 - cosθ averaged across pixels.
            cos = (normals * normals_depth).sum(dim=-1)
            loss = loss + LAMBDA_NORM * (1.0 - cos).mean()

        opt.zero_grad(set_to_none=True)
        loss.backward()
        opt.step()

        # PROGRESS every ~1% of the retrain. The retrain occupies
        # [0.10, 0.65] of the parent's overall progress budget;
        # the dome render + TSDF + bake split [0.65, 1.0].
        pct = 0.10 + 0.55 * ((it + 1) / max(1, n_iters))
        if pct - last_pct >= 0.01 or it == n_iters - 1:
            _emit(pct, f"2dgs train iter {it + 1}/{n_iters} loss={loss.item():.4f}")
            last_pct = pct

    # Detach + freeze for the dome render + bake passes.
    with torch.no_grad():
        out = {
            **prepared,
            "means": means.detach(),
            "quats": (quats / quats.norm(dim=-1, keepdim=True).clamp(min=1e-8)).detach(),
            "scales": scales.clamp(min=1e-4).detach(),
            "opacities": opacities.clamp(0.0, 1.0).detach(),
            "rgb": rgb.clamp(0.0, 1.0).detach(),
        }
    return out


def _dome_render_and_tsdf(*, prepared, centroid, extent, params):
    """Render an N-view RGB+depth+normal dome and integrate into a
    TSDF volume. Returns the extracted ``o3d.geometry.TriangleMesh``
    with per-vertex RGB + computed normals.

    We use the SAME ScalableTSDFVolume + RGB8 color type the
    low tier uses — surface quality difference between low and
    higher comes from the 2DGS retrain's better depth, not from
    any change to the TSDF integration shape.
    """
    import open3d as o3d
    import torch
    from gsplat.rendering import rasterization_2dgs

    n_views = int(params.get("n_views", 96))
    view_elevations = list(params.get("view_elevations", [0.2, 0.6]))
    voxel_size_frac = float(params.get("voxel_size", 0.005))
    sdf_trunc_mult = float(params.get("sdf_trunc_mult", 4.0))
    depth_trunc_mult = float(params.get("depth_trunc", 3.0))

    voxel = max(voxel_size_frac, 1.0 / 2048.0) * extent
    sdf_trunc = sdf_trunc_mult * voxel
    depth_trunc = depth_trunc_mult * extent
    _log(
        f"tsdf params voxel={voxel:.5f} sdf_trunc={sdf_trunc:.5f} "
        f"depth_trunc={depth_trunc:.4f}"
    )

    tsdf = o3d.pipelines.integration.ScalableTSDFVolume(
        voxel_length=voxel,
        sdf_trunc=sdf_trunc,
        color_type=o3d.pipelines.integration.TSDFVolumeColorType.RGB8,
    )

    poses = _dome_camera_path(
        centroid, extent,
        n_views=n_views, view_elevations=view_elevations,
    )
    fov_rad = math.radians(_DOME_FOV_DEG)
    fy = _DOME_H / (2.0 * math.tan(fov_rad * 0.5))
    fx = fy
    intr = o3d.camera.PinholeCameraIntrinsic(
        _DOME_W, _DOME_H, fx, fy, _DOME_W / 2.0, _DOME_H / 2.0,
    )

    device = prepared["device"]
    K = torch.tensor(
        [[fx, 0.0, _DOME_W / 2.0], [0.0, fy, _DOME_H / 2.0], [0.0, 0.0, 1.0]],
        device=device, dtype=torch.float32,
    ).unsqueeze(0)
    flip_yz = prepared["flip_yz"]
    n = len(poses)
    tick_every = max(1, n // 20)
    for i, c2w_flat in enumerate(poses):
        c2w = torch.tensor(c2w_flat, device=device, dtype=torch.float32).view(1, 4, 4)
        c2w_opencv = c2w @ flip_yz
        viewmats = torch.linalg.inv(c2w_opencv)
        with torch.no_grad():
            # ``render_mode="RGB+ED"`` is required to get the
            # ``render_median`` channel we index at ret[5] below
            # for TSDF integration. Without it gsplat returns an
            # RGB-only tuple shape and the depth channel falls
            # through to the 3DGS expected-depth fallback path —
            # which works but loses the 2DGS surface-aware advantage
            # the higher tier is meant to deliver.
            render = rasterization_2dgs(
                means=prepared["means"],
                quats=prepared["quats"],
                scales=prepared["scales"],
                opacities=prepared["opacities"],
                colors=prepared["rgb"],
                viewmats=viewmats,
                Ks=K,
                width=_DOME_W,
                height=_DOME_H,
                render_mode="RGB+ED",
            )
        ret = list(render)
        # gsplat 1.4.x ``rasterization_2dgs`` documented return
        # order (verified against the gsplat 1.4 release notes and
        # the rendering.py source):
        #     (render_rgb, alphas, normals, normals_from_depth,
        #      render_distort, render_median, meta)
        # ``render_median`` (index 5) is the per-pixel median ray
        # depth and is the channel TSDF wants. Indexing by
        # position (rather than a first-match-on-shape scan)
        # prevents picking up ``alphas`` (index 1, also a
        # single-channel HxW tensor) which would feed opacity
        # values into the TSDF as metric depth and produce
        # severely distorted or empty meshes.
        #
        # The defensive fallback handles patch versions where the
        # tuple gets reshuffled — we still need a depth signal to
        # integrate, so we delegate to ``ply_render.render_one_rgbd``
        # (3DGS expected-depth) rather than try to guess at a
        # 2DGS-flavoured one. Quality drops to "low-tier-like" on
        # the affected views, but the mesh still extracts.
        rgb_t = ret[0]
        if rgb_t.ndim == 4:
            rgb_t = rgb_t[0]
        # See the retrain-loop comment: ``render_mode="RGB+ED"``
        # gives a 4-channel render_colors output with depth in the
        # last channel. Strip to RGB-only for the TSDF
        # ``Image(rgb)`` packaging (the integration's depth comes
        # from ``render_median`` at ret[5] below, not from this
        # tensor).
        if rgb_t.shape[-1] == 4:
            rgb_t = rgb_t[..., :3]
        depth_t = None
        if len(ret) >= 6 and torch.is_tensor(ret[5]):
            cand = ret[5]
            if cand.ndim == 4:
                cand = cand[0]
            if cand.ndim == 3 and cand.shape[-1] == 1:
                cand = cand[..., 0]
            if cand.ndim == 2 and cand.shape == (_DOME_H, _DOME_W):
                depth_t = cand
        if depth_t is None:
            _log("WARN: dome render produced no depth channel; "
                 "falling back to 3DGS expected-depth")
            # Defensive fallback — same shape low tier uses.
            rgb_back, depth_back, _alpha = ply_render.render_one_rgbd(
                prepared, c2w_flat,
                fov_deg=_DOME_FOV_DEG, width=_DOME_W, height=_DOME_H,
            )
            depth = depth_back
            rgb = rgb_back
        else:
            depth = depth_t.cpu().numpy().astype(np.float32)
            rgb = (rgb_t.clamp(0.0, 1.0) * 255.0).to(torch.uint8).cpu().numpy()

        rgb_img = o3d.geometry.Image(np.ascontiguousarray(rgb))
        depth_img = o3d.geometry.Image(np.ascontiguousarray(depth))
        rgbd = o3d.geometry.RGBDImage.create_from_color_and_depth(
            rgb_img, depth_img,
            depth_scale=1.0,
            depth_trunc=depth_trunc,
            convert_rgb_to_intensity=False,
        )
        extrinsic = ply_render.opencv_extrinsic_from_opengl(c2w_flat)
        tsdf.integrate(rgbd, intr, extrinsic.astype(np.float64))
        if (i + 1) % tick_every == 0 or i == n - 1:
            frac = 0.66 + 0.14 * ((i + 1) / n)
            _emit(frac, f"dome fuse {i + 1}/{n}")

    _emit(0.81, "extract mesh")
    mesh = tsdf.extract_triangle_mesh()
    mesh.compute_vertex_normals()
    return mesh


def _bake_textures(*, mesh, staging_dir: Path):
    """UV-unwrap the mesh via xatlas and bake a texture page from
    the mesh's vertex colors. The bake samples vertex colors into
    each texel by barycentric interpolation across the triangle
    the texel falls in.

    Writes ``staging_dir/scene_tex0.jpg`` + a one-line MTL
    referencing it. For the modest mesh sizes our pipeline
    produces (≤500k tris) a single page at ``_TEX_PAGE_PX`` is
    plenty; we don't multi-page until the unwrap reports an
    overflow (deferred to a future iteration — the bake just
    drops uncovered texels).
    """
    import xatlas
    from PIL import Image

    _emit(0.82, "uv unwrap")
    verts = np.asarray(mesh.vertices)
    tris = np.asarray(mesh.triangles).astype(np.uint32)
    vcols = np.asarray(mesh.vertex_colors)
    if verts.size == 0 or tris.size == 0:
        raise RuntimeError(
            "2dgs mesh extraction produced an empty surface — "
            "retrain didn't converge enough for a valid TSDF"
        )

    # xatlas atlas options. The defaults are conservative and
    # produce one page for most phone-capture meshes. ``ChartOptions``
    # tightens the chart-cut threshold so we don't over-segment
    # small features.
    atlas = xatlas.Atlas()
    atlas.add_mesh(verts, tris)
    chart_opts = xatlas.ChartOptions()
    pack_opts = xatlas.PackOptions()
    pack_opts.resolution = _TEX_PAGE_PX
    pack_opts.bilinear = True
    atlas.generate(chart_options=chart_opts, pack_options=pack_opts)
    vmap, indices, uvs = atlas.get_mesh(0)
    # ``vmap`` maps each atlas-vertex back to the input-vertex
    # index, ``uvs`` is the (V, 2) UV in pixels of the atlas, and
    # ``indices`` is the new (T, 3) triangle list against the
    # atlas-vertex space.

    _emit(0.86, "bake texture page")
    # Allocate the page + a coverage mask. xatlas can pack into a
    # non-square atlas (charts won't always fill the requested
    # ``resolution`` evenly), so we read width / height
    # separately. The raster bounds, page array shape, and UV
    # normalisation below all use the per-axis dimension.
    page_w = atlas.width
    page_h = atlas.height
    page = np.zeros((page_h, page_w, 3), dtype=np.uint8)
    covered = np.zeros((page_h, page_w), dtype=bool)

    # Bake by rasterizing each triangle's barycentric color into
    # the atlas page. CPU-side numpy raster — slow but simple and
    # avoids a CUDA roundtrip for what's a once-per-mesh step.
    # For each triangle:
    #   1. Compute the atlas-pixel bounding box.
    #   2. For every pixel inside the box, compute the barycentric
    #      coords against the triangle's UVs.
    #   3. If all barycentrics are >= 0, sample vertex colors via
    #      barycentric interpolation and write to ``page``.
    vcols_u8 = (vcols.clip(0.0, 1.0) * 255.0).astype(np.uint8) if vcols.size else None
    if vcols_u8 is None:
        # Fall back to mid-grey if the TSDF didn't carry per-vertex
        # color (defensive — shouldn't happen with RGB8 color type).
        vcols_u8 = np.full((verts.shape[0], 3), 180, dtype=np.uint8)
    for t_idx, tri in enumerate(indices):
        a_i, b_i, c_i = int(tri[0]), int(tri[1]), int(tri[2])
        uv_a, uv_b, uv_c = uvs[a_i], uvs[b_i], uvs[c_i]
        col_a = vcols_u8[int(vmap[a_i])]
        col_b = vcols_u8[int(vmap[b_i])]
        col_c = vcols_u8[int(vmap[c_i])]
        x_min = max(0, int(np.floor(min(uv_a[0], uv_b[0], uv_c[0]))))
        y_min = max(0, int(np.floor(min(uv_a[1], uv_b[1], uv_c[1]))))
        x_max = min(page_w - 1, int(np.ceil(max(uv_a[0], uv_b[0], uv_c[0]))))
        y_max = min(page_h - 1, int(np.ceil(max(uv_a[1], uv_b[1], uv_c[1]))))
        if x_max < x_min or y_max < y_min:
            continue
        denom = (
            (uv_b[1] - uv_c[1]) * (uv_a[0] - uv_c[0])
            + (uv_c[0] - uv_b[0]) * (uv_a[1] - uv_c[1])
        )
        if abs(denom) < 1e-8:
            continue
        ys, xs = np.mgrid[y_min:y_max + 1, x_min:x_max + 1]
        bx = (
            (uv_b[1] - uv_c[1]) * (xs - uv_c[0])
            + (uv_c[0] - uv_b[0]) * (ys - uv_c[1])
        ) / denom
        by = (
            (uv_c[1] - uv_a[1]) * (xs - uv_c[0])
            + (uv_a[0] - uv_c[0]) * (ys - uv_c[1])
        ) / denom
        bz = 1.0 - bx - by
        inside = (bx >= 0) & (by >= 0) & (bz >= 0)
        if not bool(inside.any()):
            continue
        sampled = (
            bx[..., None] * col_a
            + by[..., None] * col_b
            + bz[..., None] * col_c
        ).clip(0, 255).astype(np.uint8)
        page[ys[inside], xs[inside]] = sampled[inside]
        covered[ys[inside], xs[inside]] = True
        if t_idx % 5000 == 0:
            _emit(
                0.86 + 0.06 * (t_idx / max(1, len(indices))),
                f"bake tri {t_idx}/{len(indices)}",
            )

    # Dilate the coverage mask by one texel so MIP-mapping doesn't
    # bleed background pixels into the atlas. Cheap np.roll-based
    # 4-neighbour dilation; one pass is plenty for the gaps xatlas
    # leaves at chart seams.
    #
    # ``np.roll`` is toroidal — pixels that wrap from one edge to
    # the opposite edge would otherwise leak colors from charts
    # touching one border into the unrelated opposite border. At
    # high atlas-utilisation that produces visible streaks on
    # distant triangles. After each roll we explicitly zero out
    # the wrap-around strip on the rolled coverage mask so the
    # subsequent ``fill`` calculation only considers genuine
    # in-bounds neighbours.
    _emit(0.93, "seam dilate")
    for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        rolled_cov = np.roll(covered, shift=(dy, dx), axis=(0, 1))
        rolled_pg = np.roll(page, shift=(dy, dx, 0), axis=(0, 1, 2))
        # Invalidate whichever edge strip wrapped around. ``dy=-1``
        # rolled the array up by 1, so the last row now holds the
        # original first row's data → mask the last row. Same
        # logic for the other three directions.
        if dy == -1:
            rolled_cov[-1, :] = False
        elif dy == 1:
            rolled_cov[0, :] = False
        if dx == -1:
            rolled_cov[:, -1] = False
        elif dx == 1:
            rolled_cov[:, 0] = False
        fill = (~covered) & rolled_cov
        page[fill] = rolled_pg[fill]

    # Write the texture page + the OBJ (with the new UVs) + the MTL.
    tex_path = staging_dir / "scene_tex0.jpg"
    Image.fromarray(page).save(tex_path, quality=90)

    # Re-emit the mesh as an OBJ with the per-atlas-vertex UVs and
    # the atlas-vertex space triangle list. We rewrite the OBJ
    # ourselves (rather than going through Open3D) because Open3D's
    # OBJ writer doesn't carry per-vertex UVs from an external
    # atlas.
    _emit(0.95, "write obj/mtl")
    obj_path = staging_dir / "scene.obj"
    mtl_path = staging_dir / "scene.mtl"
    with obj_path.open("w") as f:
        f.write("mtllib scene.mtl\n")
        f.write("usemtl scene\n")
        # Positions — one per atlas vertex.
        for i in range(len(vmap)):
            v = verts[int(vmap[i])]
            f.write(f"v {v[0]:.6f} {v[1]:.6f} {v[2]:.6f}\n")
        # UVs — same order. xatlas reports UVs in pixel space;
        # OBJ wants normalised [0, 1] with V flipped (OpenGL
        # convention vs xatlas's top-down). Normalize + flip here.
        for uv in uvs:
            u = float(uv[0]) / page_w
            v = 1.0 - float(uv[1]) / page_h
            f.write(f"vt {u:.6f} {v:.6f}\n")
        # Faces — vt and v share the same index (1-based per OBJ).
        for tri in indices:
            a = int(tri[0]) + 1
            b = int(tri[1]) + 1
            c = int(tri[2]) + 1
            f.write(f"f {a}/{a} {b}/{b} {c}/{c}\n")
    mtl_path.write_text(
        "newmtl scene\n"
        "Ka 1.000 1.000 1.000\n"
        "Kd 1.000 1.000 1.000\n"
        "Ks 0.000 0.000 0.000\n"
        "map_Kd scene_tex0.jpg\n"
    )

    # Best-effort GLB. trimesh's OBJ→glTF preserves the MTL +
    # texture; if it fails (corrupt UV layout, exceptionally large
    # mesh), we still ship the OBJ bundle.
    _emit(0.97, "glb pack")
    try:
        import trimesh

        mesh_t = trimesh.load(obj_path, force="mesh")
        mesh_t.export(staging_dir / "scene.glb")
    except Exception as exc:  # noqa: BLE001
        _log(f"glb conversion failed: {exc}; obj+mtl+jpg only")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--src-ply", required=True)
    ap.add_argument("--transforms-json", required=True)
    ap.add_argument("--images-src-dir", required=True)
    ap.add_argument("--staging-dir", required=True)
    ap.add_argument("--params", required=True, help="JSON-encoded params dict")
    args = ap.parse_args()

    # Belt-and-suspenders setsid so SIGKILL on the parent's
    # registered pgid reaches the torch retrain loop.
    try:
        os.setsid()
    except (OSError, PermissionError):
        pass

    src_ply = Path(args.src_ply)
    transforms_path = Path(args.transforms_json)
    images_src_dir = Path(args.images_src_dir)
    staging_dir = Path(args.staging_dir)
    params = json.loads(args.params)
    train_iters = int(params.get("higher_train_iters", 10_000))
    bbox_low = float(params.get("bbox_percentile_low", 10.0))
    bbox_high = float(params.get("bbox_percentile_high", 90.0))

    _log(f"2dgs higher-tier on {src_ply.name} iters={train_iters}")

    _emit(0.02, "prepare splat (flat-surfel init)")
    prepared = ply_render.prepare_scene(src_ply)
    prepared = _flat_surfel_init(prepared)
    centroid, extent = _ply_bbox(
        src_ply, percentile_low=bbox_low, percentile_high=bbox_high,
    )

    _emit(0.05, "load transforms")
    frames, intrinsics = _load_transforms(transforms_path, images_src_dir)
    if not frames:
        _log("ERROR: transforms.json has no frame paths resolvable on disk")
        return 4
    _log(f"loaded {len(frames)} frames at {intrinsics['w']}x{intrinsics['h']}")

    _emit(0.10, f"2dgs retrain ({train_iters} iters)")
    retrained = _train_2dgs(
        prepared=prepared,
        frames=frames,
        intrinsics=intrinsics,
        n_iters=train_iters,
    )
    if retrained is None:
        return 5

    mesh = _dome_render_and_tsdf(
        prepared=retrained,
        centroid=centroid,
        extent=extent,
        params=params,
    )
    if len(mesh.triangles) == 0:
        _log("ERROR: TSDF produced an empty mesh from the 2DGS retrain — "
             "training did not converge to a usable surface")
        return 6

    _bake_textures(mesh=mesh, staging_dir=staging_dir)

    # Final sanity: every output artifact the parent expects.
    must_exist = ["scene.obj", "scene.mtl", "scene_tex0.jpg"]
    for name in must_exist:
        if not (staging_dir / name).exists():
            _log(f"ERROR: bake step did not produce {name}")
            return 7

    _emit(1.0, "done")
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except RuntimeError as exc:
        _log(f"ERROR: {exc}")
        sys.exit(8)

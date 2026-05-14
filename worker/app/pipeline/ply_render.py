"""PLY-based gaussian splat rasterizer.

Reads a splatfacto-format ``.ply`` and renders it to RGB via
``gsplat.rasterization``. Used for filter-triggered thumbnail
regen — the existing ``ns-render`` path reads the splatfacto
*checkpoint* (which the filter pipeline doesn't touch), so naively
re-running ``run_thumbnail`` / ``run_orbit`` after a filter
produces a byte-identical image of the unedited splat. Reading
from the (edited) PLY directly is the only way to get a render
that reflects the user's filter recipe.

Renders at SH degree 0 (constant color per gaussian) — view-
dependent shading is dropped. Sufficient for the home-grid
thumbnail tile + 1.5 s orbit clip; the loss is invisible at
180 px tall. The full splatfacto checkpoint still drives the
detail-page viewer where view-dependent effects matter.

Heavy deps (torch, gsplat, plyfile, numpy) are imported lazily so
test hosts without CUDA can ``import ply_render`` without crashing
— the import test in CI only loads the module to make sure
references resolve.
"""
from __future__ import annotations

import io
import logging
import math
from pathlib import Path
from typing import Sequence

log = logging.getLogger(__name__)

# Output RGB convention: PNG-friendly uint8 in HWC order.
# Caller is responsible for any post-processing (PNG encode, etc.)
# — keeping the renderer return type ``ndarray`` rather than
# ``bytes`` lets the orbit step skip a re-decode when piping frames
# through ffmpeg.


def is_available() -> tuple[bool, str | None]:
    """Probe whether this module can render without crashing.
    Returns ``(True, None)`` when gsplat + torch + CUDA + plyfile
    are importable AND CUDA actually has a device. Returns
    ``(False, reason)`` otherwise — the caller (thumbnail.py /
    orbit.py) emits a ``permanent_skip`` with the reason so the
    backfill knows not to retry every cycle.
    """
    try:
        import torch  # noqa: F401
    except Exception as exc:  # pragma: no cover - import-time env check
        return False, f"torch unavailable: {exc}"
    try:
        import gsplat  # noqa: F401
    except Exception as exc:  # pragma: no cover
        return False, f"gsplat unavailable: {exc}"
    try:
        import plyfile  # noqa: F401
    except Exception as exc:  # pragma: no cover
        return False, f"plyfile unavailable: {exc}"
    import torch
    if not torch.cuda.is_available():
        return False, "CUDA not available"
    return True, None


def prepare_scene(ply_path: Path):
    """Load a splatfacto PLY into GPU tensors once. Returns an
    opaque ``PreparedScene`` that subsequent ``render_one`` calls
    can reuse without re-parsing or re-uploading.

    Splitting prepare + render lets async callers (orbit step)
    amortize the load cost across N frames AND interleave
    ``await asyncio.sleep(0)`` between frames so the worker's
    heartbeat task can run + observe ``status=canceled`` promptly.
    Without this split the orbit's batched render holds the event
    loop for the full ~30 s CUDA call and a user cancel mid-render
    is delayed by minutes.

    Late imports — keep the module importable without CUDA so the
    caller can probe ``is_available`` cleanly. Failures here are
    programmer errors at this point.
    """
    import numpy as np
    import torch
    from plyfile import PlyData

    device = torch.device("cuda")
    means, quats, scales, opacities, colors_dc = _load_splatfacto_ply(
        ply_path, device=device, np=np, torch=torch, PlyData=PlyData,
    )

    # SH degree 0 → RGB via the standard SH C0 coefficient. Pass
    # already-converted RGB into gsplat (sh_degree=None) — the
    # cheapest input path and bypasses per-gsplat-version SH
    # internals.
    SH_C0 = 0.28209479177387814  # 1 / (2 * sqrt(pi))
    rgb = (0.5 + SH_C0 * colors_dc).clamp(0.0, 1.0)

    # OpenGL → OpenCV camera-axis flip (Y and Z negated). Cached
    # so each render_one call doesn't rebuild it.
    flip_yz = torch.tensor(
        [
            [1.0, 0.0, 0.0, 0.0],
            [0.0, -1.0, 0.0, 0.0],
            [0.0, 0.0, -1.0, 0.0],
            [0.0, 0.0, 0.0, 1.0],
        ],
        device=device,
    )

    return {
        "device": device,
        "means": means,
        "quats": quats,
        "scales": scales,
        "opacities": opacities,
        "rgb": rgb,
        "flip_yz": flip_yz,
    }


def render_one(
    prepared,
    c2w_opengl: Sequence[float],
    *,
    fov_deg: float,
    width: int,
    height: int,
):
    """Render a single frame against a pre-loaded scene. Returns a
    ``(H, W, 3)`` uint8 numpy array. Synchronous CUDA work; call
    via ``asyncio.to_thread`` from async paths so the event loop
    stays free for heartbeats."""
    import torch
    from gsplat.rendering import rasterization

    device = prepared["device"]
    flip_yz = prepared["flip_yz"]

    c2w = torch.tensor(
        list(c2w_opengl), device=device, dtype=torch.float32,
    ).view(1, 4, 4)
    c2w_opencv = c2w @ flip_yz
    viewmats = torch.linalg.inv(c2w_opencv)

    fov_rad = math.radians(fov_deg)
    fy = height / (2.0 * math.tan(fov_rad / 2.0))
    fx = fy
    K = torch.tensor(
        [[fx, 0.0, width / 2.0], [0.0, fy, height / 2.0], [0.0, 0.0, 1.0]],
        device=device,
        dtype=torch.float32,
    ).unsqueeze(0)

    render_colors, _alphas, _meta = rasterization(
        means=prepared["means"],
        quats=prepared["quats"],
        scales=prepared["scales"],
        opacities=prepared["opacities"],
        colors=prepared["rgb"],
        viewmats=viewmats,
        Ks=K,
        width=width,
        height=height,
        sh_degree=None,
    )
    out = (render_colors[0].clamp(0.0, 1.0) * 255.0).to(torch.uint8).cpu().numpy()
    return out


def render_one_rgbd(
    prepared,
    c2w_opengl: Sequence[float],
    *,
    fov_deg: float,
    width: int,
    height: int,
):
    """Render a single frame producing aligned RGB + depth.

    Returns ``(rgb_uint8, depth_float32)``:
      * ``rgb_uint8`` — ``(H, W, 3)`` uint8 in [0, 255], same shape +
        convention as ``render_one``.
      * ``depth_float32`` — ``(H, W)`` float32 in scene units. Each
        pixel is the alpha-weighted expected ray depth (gsplat's
        ``ED`` mode) — well-defined at silhouette edges, no NaNs,
        zero where no gaussians integrate. Suitable as a depth
        channel for ``o3d.geometry.RGBDImage.create_from_color_and_depth``.

    Uses ``rasterization(..., render_mode="RGB+ED")`` which returns a
    ``(B, H, W, 4)`` tensor in a single forward pass — the 4th
    channel is the expected depth. Falls back to two separate passes
    (``RGB`` + ``ED``) if the combined mode rejects the kwarg on the
    installed gsplat version. The two-pass path roughly doubles the
    cost but keeps the contract identical.

    Why "ED" and not "D": gsplat's bare ``D`` mode is alpha-weighted
    accumulated depth (Σ αᵢ zᵢ) which underestimates at silhouette
    edges where the total transmittance is below 1.0. ``ED`` divides
    by accumulated alpha, giving the *expected* depth which is the
    moment the TSDF integration actually wants (the ray's most
    likely surface intersection, not the splat's contribution).
    """
    import numpy as np
    import torch
    from gsplat.rendering import rasterization

    device = prepared["device"]
    flip_yz = prepared["flip_yz"]

    c2w = torch.tensor(
        list(c2w_opengl), device=device, dtype=torch.float32,
    ).view(1, 4, 4)
    c2w_opencv = c2w @ flip_yz
    viewmats = torch.linalg.inv(c2w_opencv)

    fov_rad = math.radians(fov_deg)
    fy = height / (2.0 * math.tan(fov_rad / 2.0))
    fx = fy
    K = torch.tensor(
        [[fx, 0.0, width / 2.0], [0.0, fy, height / 2.0], [0.0, 0.0, 1.0]],
        device=device,
        dtype=torch.float32,
    ).unsqueeze(0)

    kwargs = dict(
        means=prepared["means"],
        quats=prepared["quats"],
        scales=prepared["scales"],
        opacities=prepared["opacities"],
        colors=prepared["rgb"],
        viewmats=viewmats,
        Ks=K,
        width=width,
        height=height,
        sh_degree=None,
    )
    try:
        render_colors, _alphas, _meta = rasterization(
            **kwargs, render_mode="RGB+ED",
        )
        # Combined mode: (1, H, W, 4) — RGB in [:, :, :, :3], ED in
        # [:, :, :, 3].
        rgb_t = render_colors[0, :, :, :3].clamp(0.0, 1.0)
        depth_t = render_colors[0, :, :, 3]
    except (TypeError, ValueError):  # pragma: no cover — older gsplat
        # Older gsplat versions might not accept ``render_mode``;
        # fall back to two passes. Same kwargs, no render_mode for
        # RGB; explicit render_mode="ED" for depth.
        rgb_only, _a, _m = rasterization(**kwargs)
        depth_only, _a2, _m2 = rasterization(**kwargs, render_mode="ED")
        rgb_t = rgb_only[0].clamp(0.0, 1.0)
        depth_t = depth_only[0, :, :, 0] if depth_only.ndim == 4 else depth_only[0]

    rgb = (rgb_t * 255.0).to(torch.uint8).cpu().numpy()
    depth = depth_t.to(torch.float32).cpu().numpy().astype(np.float32, copy=False)
    return rgb, depth


def opencv_extrinsic_from_opengl(c2w_opengl: Sequence[float]):
    """Convert an OpenGL camera-to-world matrix (16-float row-major)
    to a world-to-camera extrinsic in OpenCV convention as a 4×4
    numpy float32 array.

    Open3D's ``ScalableTSDFVolume.integrate(rgbd, intrinsic,
    extrinsic)`` expects this shape: an OpenCV-axis world→camera
    matrix where +X = right, +Y = down, +Z = forward (into the
    scene). The renderer's internal pipeline goes
    OpenGL c2w → OpenCV c2w (via the YZ flip) → invert to get the
    viewmat; this helper exposes the same flip + invert for callers
    that need the extrinsic separately (e.g. the TSDF subprocess).
    """
    import numpy as np

    c2w = np.asarray(list(c2w_opengl), dtype=np.float32).reshape(4, 4)
    flip = np.diag(np.array([1.0, -1.0, -1.0, 1.0], dtype=np.float32))
    c2w_cv = c2w @ flip
    return np.linalg.inv(c2w_cv).astype(np.float32, copy=False)


def render_frames(
    *,
    ply_path: Path,
    c2ws_opengl: Sequence[Sequence[float]],
    fov_deg: float,
    width: int,
    height: int,
):
    """Synchronous batched render — convenience for non-async
    callers (tests, scripts). Loops over [prepare_scene +
    render_one] internally. Async callers should use prepare_scene
    + render_one directly with ``asyncio.to_thread`` so per-frame
    work doesn't block the event loop.

    Returns a numpy array of shape ``(N, H, W, 3)`` with dtype
    ``uint8`` — RGB in [0, 255].
    """
    import numpy as np

    prepared = prepare_scene(ply_path)
    frames = [
        render_one(prepared, c2w, fov_deg=fov_deg, width=width, height=height)
        for c2w in c2ws_opengl
    ]
    return np.stack(frames, axis=0)


def render_png(
    *,
    ply_path: Path,
    c2w_opengl: Sequence[float],
    fov_deg: float,
    width: int,
    height: int,
) -> bytes:
    """Render a single frame and PNG-encode it. Convenience for
    the still-thumbnail path; the orbit step uses prepare_scene +
    per-frame render_one for cancellation-friendly streaming.
    """
    from PIL import Image  # late import, Pillow ships with torch

    prepared = prepare_scene(ply_path)
    arr = render_one(
        prepared, c2w_opengl,
        fov_deg=fov_deg, width=width, height=height,
    )
    buf = io.BytesIO()
    Image.fromarray(arr).save(buf, format="PNG", optimize=True)
    return buf.getvalue()


def _load_splatfacto_ply(
    ply_path: Path,
    *,
    device,
    np,
    torch,
    PlyData,
):
    """Parse a splatfacto-format PLY into (means, quats, scales,
    opacities, colors_dc) GPU tensors with appropriate transforms:
    * opacities = sigmoid(raw_opacity)
    * scales = exp(log_scales)
    * quats normalized (gsplat expects unit quaternions in wxyz)
    * colors_dc returned in raw SH degree-0 form; caller converts
      to RGB via the SH C0 constant.

    Splatfacto's PLY contains:
        x, y, z                — positions (float)
        opacity                — scalar, pre-sigmoid
        scale_0..2             — log-scale (float)
        rot_0..3               — quaternion (wxyz)
        f_dc_0..2              — SH degree-0 coefficients (RGB)
        f_rest_*               — higher SH degrees (ignored here)
    """
    data = PlyData.read(str(ply_path))
    v = data["vertex"]
    n = int(v.count)
    if n == 0:
        raise RuntimeError("source PLY has zero gaussians; cannot render")

    means_np = np.column_stack(
        [
            np.asarray(v["x"], dtype=np.float32),
            np.asarray(v["y"], dtype=np.float32),
            np.asarray(v["z"], dtype=np.float32),
        ]
    )
    means = torch.from_numpy(means_np).to(device)

    raw_opacity = torch.from_numpy(
        np.asarray(v["opacity"], dtype=np.float32)
    ).to(device)
    opacities = torch.sigmoid(raw_opacity)

    log_scales = torch.from_numpy(
        np.column_stack(
            [
                np.asarray(v["scale_0"], dtype=np.float32),
                np.asarray(v["scale_1"], dtype=np.float32),
                np.asarray(v["scale_2"], dtype=np.float32),
            ]
        )
    ).to(device)
    scales = torch.exp(log_scales)

    quats_np = np.column_stack(
        [
            np.asarray(v["rot_0"], dtype=np.float32),  # w
            np.asarray(v["rot_1"], dtype=np.float32),  # x
            np.asarray(v["rot_2"], dtype=np.float32),  # y
            np.asarray(v["rot_3"], dtype=np.float32),  # z
        ]
    )
    quats = torch.from_numpy(quats_np).to(device)
    # Normalize — splatfacto stores raw quats that drift away from
    # unit during training; gsplat's rasterizer assumes unit-length.
    quats = quats / quats.norm(dim=-1, keepdim=True).clamp(min=1e-8)

    colors_dc_np = np.column_stack(
        [
            np.asarray(v["f_dc_0"], dtype=np.float32),
            np.asarray(v["f_dc_1"], dtype=np.float32),
            np.asarray(v["f_dc_2"], dtype=np.float32),
        ]
    )
    colors_dc = torch.from_numpy(colors_dc_np).to(device)

    log.info("ply_render: loaded %d gaussians from %s", n, ply_path.name)
    return means, quats, scales, opacities, colors_dc

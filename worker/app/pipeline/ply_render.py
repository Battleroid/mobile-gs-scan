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


def render_frames(
    *,
    ply_path: Path,
    c2ws_opengl: Sequence[Sequence[float]],
    fov_deg: float,
    width: int,
    height: int,
):
    """Render a batch of frames from one PLY at the given cameras.

    Returns a numpy array of shape ``(N, H, W, 3)`` with dtype
    ``uint8`` — RGB in [0, 255]. Caller batches because the GPU
    setup (PLY load + tensor upload) is the dominant cost; one
    camera is ~the same wall time as 24 cameras once the buffers
    are resident.

    Args:
        ply_path: source splatfacto PLY.
        c2ws_opengl: list of N flattened 4×4 camera-to-world
            matrices in nerfstudio / OpenGL convention (column 0
            right, column 1 up, column 2 backward, column 3
            position). Same flatten produced by
            ``thumbnail._look_at``.
        fov_deg: vertical field-of-view in degrees.
        width, height: output dimensions.
    """
    # Late imports — keep the module importable without CUDA so the
    # caller can probe ``is_available`` cleanly. Failures here are
    # programmer errors at this point (caller should have probed
    # first), so we let the natural ImportError propagate.
    import numpy as np
    import torch
    from gsplat.rendering import rasterization
    from plyfile import PlyData

    device = torch.device("cuda")
    means, quats, scales, opacities, colors_dc = _load_splatfacto_ply(
        ply_path, device=device, np=np, torch=torch, PlyData=PlyData,
    )

    # gsplat expects colors as either:
    #   * raw SH coefficients with ``sh_degree>=0``: shape [N, K, 3]
    #     where K = (degree+1)**2, OR
    #   * already-converted RGB with ``sh_degree=None``: [N, 3]
    # SH degree 0 is just a constant color per gaussian, so we
    # convert ourselves with the standard SH C0 coefficient and
    # pass raw RGB. Bypasses any per-gsplat-version SH internals
    # and the [N, 3] path is the cheapest.
    SH_C0 = 0.28209479177387814  # 1 / (2 * sqrt(pi))
    rgb = (0.5 + SH_C0 * colors_dc).clamp(0.0, 1.0)

    # Build viewmats (world→camera in OpenCV convention) and
    # intrinsics. nerfstudio camera_to_world is OpenGL convention;
    # gsplat wants OpenCV. The Y/Z flip below converts:
    #   OpenGL: +X right, +Y up,    +Z back
    #   OpenCV: +X right, +Y down,  +Z forward
    flip_yz = torch.tensor(
        [
            [1.0, 0.0, 0.0, 0.0],
            [0.0, -1.0, 0.0, 0.0],
            [0.0, 0.0, -1.0, 0.0],
            [0.0, 0.0, 0.0, 1.0],
        ],
        device=device,
    )
    c2w_opengl = torch.tensor(
        [list(c) for c in c2ws_opengl], device=device, dtype=torch.float32,
    ).view(-1, 4, 4)
    c2w_opencv = c2w_opengl @ flip_yz
    viewmats = torch.linalg.inv(c2w_opencv)

    # Pinhole intrinsics from vertical FoV. Square pixels: fx = fy.
    fov_rad = math.radians(fov_deg)
    fy = height / (2.0 * math.tan(fov_rad / 2.0))
    fx = fy
    cx = width / 2.0
    cy = height / 2.0
    K = torch.tensor(
        [[fx, 0.0, cx], [0.0, fy, cy], [0.0, 0.0, 1.0]],
        device=device,
        dtype=torch.float32,
    )
    Ks = K.unsqueeze(0).expand(viewmats.shape[0], 3, 3).contiguous()

    # Rasterize. gsplat returns colors at shape [C, H, W, 3]
    # alongside alpha + a meta dict; we only consume colors. The
    # default ``rasterize_mode="classic"`` matches what splatfacto
    # uses at training time.
    render_colors, _alphas, _meta = rasterization(
        means=means,
        quats=quats,
        scales=scales,
        opacities=opacities,
        colors=rgb,
        viewmats=viewmats,
        Ks=Ks,
        width=width,
        height=height,
        sh_degree=None,
        # near_plane / far_plane defaults are fine for the
        # bbox-fit cameras the still + orbit produce. Going past
        # the defaults can cull foreground gaussians and produce
        # an empty render.
    )

    # [C, H, W, 3] → uint8 RGB on CPU for PNG encoding.
    out = (render_colors.clamp(0.0, 1.0) * 255.0).to(torch.uint8).cpu().numpy()
    return out


def render_png(
    *,
    ply_path: Path,
    c2w_opengl: Sequence[float],
    fov_deg: float,
    width: int,
    height: int,
) -> bytes:
    """Render a single frame and PNG-encode it. Convenience for
    the still-thumbnail path; the orbit step uses ``render_frames``
    directly and pipes the RGB sequence to ffmpeg.
    """
    from PIL import Image  # late import, Pillow ships with torch

    frames = render_frames(
        ply_path=ply_path,
        c2ws_opengl=[list(c2w_opengl)],
        fov_deg=fov_deg,
        width=width,
        height=height,
    )
    buf = io.BytesIO()
    Image.fromarray(frames[0]).save(buf, format="PNG", optimize=True)
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

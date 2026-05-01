# Roadmap

PR #1 = capture-to-viewer happy path on room-scale captures from the
Android app or a drag-and-drop image set. Everything below is
explicitly out of scope for the first PR.

## PR #2 — capture polish

- Scaniverse-style AR coverage cones / heatmap on Android.
- Live downsampled point-cloud preview pushed back to the phone
  during capture.
- Mobile-web PWA: device-motion + IMU pose prior.
- Auto-stop heuristic when bounding sphere coverage uniform.

## PR #3 — mesh + alternate exports

- 2DGS + TSDF + Open3D Poisson → .obj / .glb / .fbx.
- SuGaR fallback for difficult scenes.
- Texture baking from the splat radiance field.

## PR #4 — on-device splat preview

- Brush wgpu viewer in the Android app + PWA. Partial-checkpoint
  push from the worker once training has progress to show.

## PR #5 — non-mobile ingestion polish

- Drone (EXIF + SRT GPS priors).
- DSLR sets (timestamps, GPS, blur filter, splatfacto-w appearance).
- Resume-able multipart uploads for large sets.

## PR #6 — iOS native + LiDAR

- ARKit native iOS app + optional LiDAR depth supervision.

## PR #7 — public-tunnel deployment

- Tailscale Funnel + Cloudflare Tunnel. Per-tunnel shared secret.

## Forever-deferred

- Multi-tenant accounts.
- Hosted cloud version.
- In-studio editing tools (export to SuperSplat/Blender instead).

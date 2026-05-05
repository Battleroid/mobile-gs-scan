# Roadmap

Pebble's MVP capture-to-viewer-to-edit-to-mesh path is shippable
end-to-end. Items below are still out of scope.

## Capture polish

- AR coverage cones / heatmap on Android during recording.
- Live downsampled point-cloud preview pushed back to the phone
  during capture.
- Auto-stop heuristic when bounding-sphere coverage is uniform.

## On-device splat preview

- Brush / wgpu viewer in the Android app. Partial-checkpoint push
  from the worker once training has progress to show.

## Non-mobile ingestion polish

- Drone (EXIF + SRT GPS priors used as seed for SfM).
- DSLR sets (timestamps, GPS, blur filter, splatfacto-w
  appearance modeling).
- Resume-able multipart uploads for large sets.

## iOS native + LiDAR

- ARKit native iOS app + optional LiDAR depth supervision.

## Auth / multi-device

- Basic per-device pairing or a small account model so the studio
  can stop assuming a private LAN with trusted clients. The
  spring-cleaning pass dropped the previous QR-pair flow; this
  re-introduces a simpler, more durable replacement (likely a
  device-token bearer header on the Android client + a config
  page on the web UI).

## Public-tunnel deployment

- Tailscale Funnel + Cloudflare Tunnel.
- Per-tunnel shared secret.

## Alternate mesh exports

- SuGaR fallback for difficult scenes (when Poisson smears).
- Texture baking from the splat radiance field.

## Forever-deferred

- Multi-tenant accounts.
- Hosted cloud version.
- In-studio editing tools beyond the recipe ops (export to
  SuperSplat / Blender instead).

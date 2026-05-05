# mobile-gs-scan

Self-hosted 3D Gaussian Splatting capture studio. Drop a folder of
images or a video at the web UI and a single GPU worker on your LAN
runs it through SfM → splatfacto → export, and serves the result back
in a browser-side viewer with editing + Poisson mesh extraction.

In the same family as Scaniverse / Kiri Engine, but the splat trainer
runs on your own NVIDIA GPU (Ampere or newer recommended; 12 GB VRAM
minimum, 24 GB+ for room-scale captures), in Docker — not someone
else's cloud.

> **Status: MVP merged.** Capture-to-viewer is shippable end-to-end.
> The full roadmap is in [`docs/roadmap.md`](docs/roadmap.md).

## What's in v1

- **Web UI** (Next.js + React Three Fiber):
  - *Drag-and-drop* a folder of images **or a single video** at
    `/captures/new`. The server runs ffmpeg → SfM (Glomap) →
    splatfacto training → `.ply` + `.spz` export. Per-capture
    overrides for training iterations (5K / 15K / 30K presets +
    custom), video extraction fps (clamped to source fps), and
    JPEG quality.
  - *Splat viewer* powered by [Spark][spark] (`@sparkjsdev/spark`)
    embedded in R3F. Cycles between splats / mono points /
    colored points / mesh views, with a per-splat opacity / scale
    quality slider.
  - *Splat editor* — recipe-based ops (opacity threshold, scale
    clamp, bbox / sphere crop + remove, statistical outlier
    removal, DBSCAN keep-largest, lasso-authored keep-indices)
    with in-viewer 3D widgets for bbox / sphere selection. Edits
    are non-destructive: the original `.ply` stays addressable
    while the edited variant lives alongside.
  - *Poisson mesh extraction* — Open3D Poisson runs in a killable
    subprocess against the trained splat .ply, exports `.obj` +
    `.glb` for download or in-viewer rendering.
- **API + worker** (FastAPI + custom polling job queue) running on
  the host GPU via the NVIDIA Container Toolkit. Workers run
  ffmpeg → Glomap → gsplat / Nerfstudio's `splatfacto` → the
  export step, with on-demand mesh + filter steps. Job + capture
  state lives in a single SQLite db.
- **Android app** (Kotlin + ARCore) — records frames + ARCore
  poses locally to the device's filesystem, then on the user's
  request uploads the JPEGs to the studio over the same HTTP
  upload path the web UI uses. No live-stream WebSocket; no
  pairing tokens — assumed-trusted private-server deployment.
- **Caddy** as a reverse proxy in front of api + web (single
  origin makes development tidy; HTTPS isn't required for any
  current flow).
- **GitHub Actions** for CI (lint + build of web / worker /
  android on every PR), GHCR image publish on `main` + `v*` tags,
  draft Release on tag, and a branch-name validator that
  enforces the `feature/` / `fix/` / `chore/` / `docs/` /
  `refactor/` / `release/` / `claude/` / `codex/` prefix from
  CONTRIBUTING.md.

[spark]: https://github.com/sparkjsdev/spark

## What's deferred (see `docs/roadmap.md`)

- AR coverage cones / heatmap / live splat preview on the phone
  during capture.
- Cleaner non-mobile ingestion (drone / DSLR sets with EXIF GPS
  to seed SfM).
- iOS native app (ARKit / LiDAR depth supervision).
- Public-tunnel deployment (Tailscale Funnel / Cloudflare Tunnel).
- Auth (basic account or per-device pairing — currently the server
  assumes a private LAN deployment with trusted clients).

## Quick start (Linux or Windows-WSL2 with NVIDIA Container Toolkit)

```bash
# preflight: docker, gpu, nvidia-container-toolkit
make doctor

# bring up api + worker-gs + web + caddy
make up

# drop a folder of images or a single video at:
#   http://<host>:3000/captures/new
```

The Android client (in `android/`) records captures locally and
uploads via the same HTTP flow when you tap "send". Build + sideload
with the targets below.

### Building the Android app

```bash
make android-bootstrap   # one-time: drops ./gradlew via system gradle
make apk-debug           # builds the debug APK
make apk-install         # builds + adb install
```

### CUDA arch list

`worker/Dockerfile.gs` builds the gsplat extension and Glomap with
multi-arch CUDA (`8.0;8.6;8.9;9.0` — A100 / RTX 30 / RTX 40 / H100)
so the same image works across the common GPU classes. To trim
build time on a constrained host, override at build time:

```bash
docker build \
  --build-arg TORCH_CUDA_ARCH_LIST=8.9 \
  --build-arg CMAKE_CUDA_ARCHITECTURES=89 \
  -f worker/Dockerfile.gs ...
```

## Layout

```
├── docker-compose.yml         # api, worker-gs, web, caddy
├── Makefile                   # up / shell-* / clean / apk-*
├── caddy/                     # reverse proxy config
├── scripts/                   # doctor, mkcert-bootstrap
├── worker/                    # Python: FastAPI api + worker-gs
│   ├── app/api/               #   HTTP + WS routers
│   ├── app/jobs/              #   job store / events / runner
│   ├── app/sessions/          #   video frame extraction
│   └── app/pipeline/          #   extract / sfm / train / export / mesh / filter
├── web/                       # Next.js + R3F + Tailwind + Spark
├── android/                   # Kotlin + ARCore + HTTP upload
└── .github/workflows/         # ci, build-images, release, branch-name
```

## License

TBD.

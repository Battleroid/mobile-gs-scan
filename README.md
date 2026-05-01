# mobile-gs-scan

Self-hosted 3D Gaussian Splatting capture studio. Walk around an object or
room with your phone, stream frames + ARCore poses to a single GPU machine
on your LAN, get back a colored 3DGS scene viewable in the browser and
exportable to common formats.

In the same family as Scaniverse / Kiri Engine, but the splat trainer
runs on your own RTX 4090 (in WSL2, via Docker) instead of someone
else's cloud.

> **Status: PR #1 — capture-to-viewer happy path.** A working foundation,
> not a finished product. The phone capture loop, server pipeline, and
> web viewer all wire end-to-end, but the AR overlay is minimal and the
> live on-device splat preview / Poisson mesh / Scaniverse-style
> coverage cones are explicitly deferred to follow-up PRs. The full
> roadmap is in [`docs/roadmap.md`](docs/roadmap.md).

## What's in v1

- **Web UI** (Next.js + React Three Fiber) with three flows:
  - *Drag-and-drop* a folder of images or a video — server runs Glomap
    SfM → splatfacto training → `.ply` + `.spz` export.
  - *Live phone capture* — start a session in the browser, pair the
    phone via QR, stream frames as you walk around, finalize, view.
  - *Splat viewer* powered by [Spark][spark] (`@sparkjsdev/spark`)
    embedded in R3F.
- **API + worker** (FastAPI + custom polling job queue, mirroring
  lingbot-map-studio's pattern) running on the host GPU via the NVIDIA
  Container Toolkit. Workers run Glomap, gsplat / Nerfstudio's
  `splatfacto`, and the export step. Session-state and job-state both
  live in a single SQLite db.
- **Android app** (Kotlin + ARCore) for the live capture path. Captures
  RGB frames + per-frame poses + intrinsics, streams them over a single
  WebSocket to the API, shows a minimal AR overlay (frame counter,
  bounding capture volume).
- **Mobile-web PWA fallback** at `/m/<token>` for phones where you don't
  want to install the Android app — `getUserMedia` + WebSocket frame
  streaming, no AR. Server-side SfM recovers the poses.
- **Caddy + mkcert** for LAN-only HTTPS so `getUserMedia` works on the
  phone (mobile browsers refuse the camera prompt over plain HTTP).
- **Make targets** for the entire dev loop — including the Android app:
  `make doctor` → `make up-https` → scan QR on phone → capture → view.
  `make android-bootstrap` + `make apk-debug` / `apk-install` for the
  Android side.
- **GitHub Actions** for CI (lint + build of web / worker / android on
  every PR), GHCR image publish on `main` + `v*` tags, draft Release
  on tag, and a branch-name validator that enforces the
  `feature/` / `fix/` / `chore/` / `docs/` / `refactor/` / `release/`
  prefix from CONTRIBUTING.md.

[spark]: https://github.com/sparkjsdev/spark

## What's deferred (see `docs/roadmap.md`)

- Full Scaniverse-style AR coverage cones / heatmap / live splat preview
  on the phone during capture.
- 2DGS + TSDF + Poisson mesh worker (`.obj` / `.glb` export).
- Cleaner non-mobile ingestion (drone / DSLR sets with EXIF).
- iOS native app (ARKit / LiDAR depth supervision).
- Public-tunnel deployment (Tailscale Funnel / Cloudflare Tunnel).
- Auth.

## Quick start (host: Windows + 4090 + WSL2 Ubuntu)

```bash
# preflight: docker, gpu, nvidia-container-toolkit
make doctor

# bring up over https on the LAN
make up-https
# → printed: rootCA URL + capture URL (with QR codes)

# from your phone (same wifi):
#   1. scan the rootCA QR, install the cert
#   2. scan the capture URL → either open in mobile-web PWA, OR
#      open the Android app (sideload .apk) and let it deep-link
```

For the drag-and-drop happy path you don't need the phone — visit
`https://localhost/captures/new` from the host machine, drop a folder
of images, and you'll see a finished splat in a few minutes.

### Building the Android app

```bash
make android-bootstrap   # one-time: drops ./gradlew via system gradle
make apk-debug           # builds the debug APK
make apk-install         # builds + adb install
```

## Layout

```
├── docker-compose.yml         # api, worker-gs, web, caddy
├── Makefile                   # up / up-https / shell-* / clean / apk-*
├── caddy/                     # reverse proxy config + LAN TLS certs
├── scripts/                   # doctor, mkcert-bootstrap
├── worker/                    # Python: FastAPI api + worker-gs
│   ├── app/api/               #   HTTP + WS routers
│   ├── app/jobs/              #   job store / events / runner
│   ├── app/sessions/          #   capture-session store + ws ingest
│   └── app/pipeline/          #   sfm / train / export / mesh
├── web/                       # Next.js 16 + R3F + Tailwind + Spark
├── android/                   # Kotlin + ARCore + WebSocket client
└── .github/workflows/         # ci, build-images, release, branch-name
```

## License

TBD.

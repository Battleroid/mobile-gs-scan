# Architecture

Single-machine, single-user studio. One physical box (Linux or
Windows-WSL2 with NVIDIA Container Toolkit). Four services run on
it.

## Services

```
caddy → web (Next.js) | api (FastAPI) → worker-gs (CUDA)
                      │
                      ▼ /data bind mount
                      studio.sqlite + captures/ + scenes/
```

- **caddy** reverse-proxies web + api on a single origin.
- **web** is Next.js standalone build with the R3F splat viewer +
  splat editor + mesh panel.
- **api** is FastAPI: capture lifecycle, multipart upload, SQLite
  store, in-memory event bus, job claim API. WebSocket endpoints
  serve event subscriptions only — frame ingest is HTTP multipart.
- **worker-gs** polls the api for queued jobs (`extract` / `sfm` /
  `train` / `export` / `mesh` / `filter`), heartbeats, runs the
  right pipeline step, writes artifacts under
  `/data/scenes/<id>/`.

## On-disk under `./data`

```
studio.sqlite                    captures + jobs + scenes + events
captures/<id>/
  source/<file>.{mp4,mov,…}      raw video, pre-extraction (video uploads only)
  frames/NNNNNN.jpg              one JPEG per frame (image uploads + post-extract)
  meta.json                      per-capture metadata
  thumbs/                        thumbnail cache
scenes/<id>/
  sfm/                           glomap or arcore_native workspace
  train/                         nerfstudio output dir
  edit/                          filter step output (.ply + .spz)
  mesh/                          Poisson mesh output (.obj + .glb)
  export/{scene.ply,scene.spz}
```

## Capture state machine

```
created → uploading → queued → processing → completed
*                            → failed | canceled
```

## Job state machine

```
queued → claimed → running → completed | failed
*                          → canceled | requeued (heartbeat lapses 60s)
```

## Pipeline

The dispatch order is `extract → sfm → train → export`, with
`mesh` and `filter` available as on-demand follow-ups. The shape
of each step:

```
finalize
  extract:
    if capture has source/<video>: ffmpeg → frames/NNNNNN.jpg
                                   at user-chosen fps + jpeg quality
    else (image set): no-op success
  sfm:
    if has_pose: write COLMAP-shaped workspace from poses.jsonl
                 (backend=arcore_native; no real SfM run)
    else:        glomap mapper → scenes/<id>/sfm/sparse/
  train:
    ns-train splatfacto --data sfm/
       --max-num-iterations <capture.meta.train_iters | $GS_TRAIN_ITERS>
  export:
    ns-export gaussian-splat → scene.ply
    spz_pack scene.ply       → scene.spz
mesh   (on-demand, POST /api/scenes/{id}/mesh):
    Open3D Poisson on scene.ply → scene.obj + scene.glb
filter (on-demand, POST /api/scenes/{id}/edit):
    recipe-based ops on scene.ply → edit/scene.{ply,spz}
```

Each long-running subprocess (`ffmpeg`, `glomap`, `ns-train`,
`ns-export`, the Poisson child) registers with `_running` so the
worker heartbeat can SIGKILL it on user-requested cancel.

## Browser ↔ api wire protocol

- HTTP REST under `/api/captures` and `/api/scenes` for the
  lifecycle + artifact endpoints (multipart upload, finalize,
  trigger mesh / edit, etc.).
- WebSocket at `/api/captures/<id>/events` and
  `/api/scenes/<id>/events` for live progress subscriptions
  (snapshot frame on connect, event frames thereafter).

There is no frame-streaming WebSocket and no pair-token handshake.
The Android client uploads recorded drafts via the same multipart
HTTP path the web drag-drop flow uses; private LAN / trusted
clients are assumed.

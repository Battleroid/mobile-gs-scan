# Architecture

Single-machine, single-user studio. One physical box (Windows + 4090
+ WSL2). All five services run on it.

## Services

```
caddy (TLS) → web (Next.js) | api (FastAPI) → worker-gs (CUDA)
                            │
                            ▼ /data bind mount
                            studio.sqlite + captures/ + scenes/
```

- **caddy** terminates TLS, fronts both web and api on the same
  origin so the WS stays same-origin from the phone.
- **web** is Next.js 16 standalone build with the R3F splat viewer.
- **api** is FastAPI: capture session lifecycle, frame ingest WS,
  SQLite store, in-memory event bus, job claim API.
- **worker-gs** polls the api for queued jobs (`sfm`/`train`/
  `export`/`mesh`), heartbeats, runs the right pipeline step,
  writes artifacts under `/data/scenes/<id>/`.

## On-disk under `./data`

```
studio.sqlite                    captures + jobs + scenes + events
captures/<id>/{frames/,poses.jsonl,meta.json,thumbs/}
scenes/<id>/{sfm/,train/,export/{scene.ply,scene.spz},status.json}
```

## Capture state machine

```
created → pairing  → streaming → queued → processing → completed
created → uploading → queued    → processing → completed
*       → failed | canceled
```

## Job state machine

```
queued → claimed → running → completed | failed
* → canceled | requeued (heartbeat lapses 60s)
```

## Frame stream wire protocol (WebSocket)

Client → server, alternating per frame:
  1. JSON header `{type:"frame", idx, ts, pose?, intrinsics?}`
  2. binary JPEG bytes

Client control: `{type:"session", device, intrinsics, has_pose}`,
`{type:"heartbeat"}`, `{type:"finalize", reason}`.

Server: `{type:"ack", frames_received, frames_dropped}` every 16
frames, `{type:"limit", reason, cap}` on cap-hit, `{type:"queued",
scene_id}` after finalize.

Browser-side event subscription is a separate WS at
`/api/captures/<id>/events`.

## Pipeline

```
finalize
  if has_pose: write COLMAP-format poses, skip SfM
  else:        glomap mapper → scenes/<id>/sfm/sparse/
  ns-train splatfacto --data sfm/ --max-num-iterations $GS_TRAIN_ITERS
  ns-export gaussian-splat → scene.ply
  spz_pack scene.ply > scene.spz
```

Mesh / Poisson ships in PR #2.

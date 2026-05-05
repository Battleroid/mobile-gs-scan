# Pebble · android (`mobile-gs-scan/android`)

Native Android capture client. Kotlin + ARCore + OkHttp. The Pebble
brand is rolling out to the web first; the Android UI rebuild is
queued behind that. The on-disk paths and `applicationId`
(`dev.battleroid.mobilegsscan`) stay the same — the rename is
user-facing only.

## What it does

1. Lets you set the studio URL once (e.g. `http://studio.local:8000`
   or `http://192.168.1.42:8000`) — stored in `SharedPreferences`.
2. Opens a foreground `CaptureActivity` with an ARCore session — RGB
   frames + per-frame world-space camera pose. Encodes each frame to
   JPEG locally to the device's filesystem (`DraftStore`).
3. On finish, lets you upload the draft to the studio over HTTP. The
   app POSTs to `/api/captures` (capture-create) → `/api/captures/{id}/upload`
   (multipart batches of JPEGs) → `/api/captures/{id}/finalize`. The
   server runs SfM (Glomap) → splatfacto → export, same flow as the
   web drag-drop path. Drafts that fail to upload stay on the device
   for a retry.
4. Renders a minimal AR overlay: a wireframe bounding cube + a frame
   counter / dropped counter.

There's no live frame stream / WebSocket / pair-token handshake — the
upload is a plain HTTP POST sequence. The studio is assumed-trusted
on a private LAN. A future auth design will reintroduce per-device
authorization.

## Building

The repo doesn't ship the Gradle wrapper jar. Bootstrap it once on a
fresh clone with `make android-bootstrap` (which runs `gradle wrapper`
under the hood). Then:

```bash
make apk-debug              # build debug APK
make apk-install            # build + adb install
make apk-release            # build release APK (unsigned)
make android-clean          # clean build outputs
```

Open the app, paste the studio URL into Settings, record a draft,
then tap "send" to push it to the studio.

## Network

ARCore captures produce sizable JPEG streams (tens of MB for a short
session). Use the same wifi as the studio. The HTTP upload path is
plain HTTP — no certificate setup needed; if you put the studio
behind a TLS-fronted reverse proxy (Caddy / nginx with a real cert
or a corporate internal CA), the app's `network_security_config.xml`
trusts user-added CAs in debug builds.

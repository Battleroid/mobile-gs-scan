# mobile-gs-scan/android

Native Android capture client. Kotlin + ARCore + OkHttp WebSocket.

## What it does today (PR #1)

1. Lets you set the studio URL once (e.g. `https://studio.local` or
   `https://192.168.1.42`) — stored in `SharedPreferences`.
2. Resolves a pair token after you scan the studio's QR code (via the
   system camera, then deep-linking back to the app — see
   `AndroidManifest.xml`'s `<intent-filter>` on `MainActivity`).
3. Opens a foreground `CaptureActivity` with an ARCore session — RGB
   frames + per-frame world-space camera pose.
4. Encodes frames to JPEG, streams them + the pose matrix + the
   ARCore-reported intrinsics to the studio over a single WebSocket
   following the wire protocol in `docs/architecture.md`.
5. Renders a minimal AR overlay: a wireframe bounding cube + a frame
   counter / dropped counter.
6. On finish, calls the WebSocket `finalize` message + opens the
   browser to the capture detail page so the user can watch the
   pipeline run.

## Deferred to PR #2 / #4

- Coverage cones (which surfaces have been seen and from how many
  angles), missing-surface heatmap, capture-volume hint.
- Brush / Spark-WebGPU on-device live splat preview.

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

Open the app, paste the studio URL into Settings, then either:

- scan the studio QR with the system camera (the app intercepts via
  the deep-link intent filter), or
- scan the QR with the in-app scanner inside CaptureActivity.

## Network

ARCore captures are big — multi-MB/s sustained. Use the same wifi as
the studio. The mkcert root CA must be installed on the phone first
(see `make up-https` output). The app's
`network_security_config.xml` trusts user-added CAs in debug builds.

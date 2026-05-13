# Changelog

All notable changes to this project are documented here.
The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/);
versioning follows [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Maintained automatically by [release-please](https://github.com/googleapis/release-please).
Every push to the default branch refreshes a "release PR" that
bundles conventional-commit messages (``feat``, ``fix``,
``feat!``, …) since the last `vX.Y.Z` tag into a proposed version
bump + a freshly-rendered entry below. Merging that release PR
cuts the tag, attaches the Android APK to a fresh GitHub Release,
and commits the new section here. Until that first release PR is
merged, the **Unreleased** section below is a hand-authored
roll-up so a reader landing here today still has signal.

## [Unreleased]

### Highlights so far

- **Worker pipeline.** `extract → sfm → train → export → thumbnail → orbit`
  with cooperative cancellation, two-band claim, and filter-triggered
  thumbnail/orbit regen against an edited PLY.
- **Web app (Pebble).** Cream-paper rebrand, capture cards with PNG/MP4
  thumbnails, splat viewer/editor/mesh/log panels, WS reconnect with
  HTTP existence probe.
- **Android app (Pebble).** Compose rebuild of home/AR-capture/draft/
  capture/job/settings/profile/sign-in, Coil thumbnails everywhere,
  Geist fonts, edge-to-edge cutout safety.
- **Thumbnail upgrade.** Server-rendered PNG thumbnail per scene; 48-
  frame gsplat orbit MP4 backfilled after export.

// Bundle the embedded splat viewer for the Android client.
//
// Reads web/src/android-splat/entry.ts, inlines Three.js + Spark + the
// OrbitControls helper into one IIFE, and writes the bundle to
// android/app/src/main/assets/splat/viewer.bundle.js. The Android
// SplatViewerActivity loads index.html (committed alongside the
// bundle) and the bundle handles everything else.
//
// Rebuild via:  pnpm --filter pebble-web run android:splat-bundle
// (or directly: node scripts/bundle-android-splat.mjs)
//
// The output is checked into the repo so the Android Gradle build
// doesn't depend on a node toolchain. Re-run this script and commit
// the diff whenever Three.js / Spark / the entry change.

import esbuild from "esbuild";
import { fileURLToPath } from "node:url";
import { dirname, resolve } from "node:path";

const here = dirname(fileURLToPath(import.meta.url));
const repoRoot = resolve(here, "..", "..");
const entry = resolve(repoRoot, "web/src/android-splat/entry.ts");
const out = resolve(
  repoRoot,
  "android/app/src/main/assets/splat/viewer.bundle.js",
);

await esbuild.build({
  entryPoints: [entry],
  bundle: true,
  format: "iife",
  // Modern Chrome WebView on Android 8+ supports ES2020 natively.
  // Target it directly so we don't ship needless polyfill bytes.
  target: "es2020",
  minify: true,
  sourcemap: false,
  outfile: out,
  // No external imports — everything (three, spark, OrbitControls) is
  // inlined so the WebView only fetches `viewer.bundle.js` from
  // /android_asset/.
  external: [],
  legalComments: "none",
  define: {
    "process.env.NODE_ENV": JSON.stringify("production"),
  },
  loader: {
    ".glsl": "text",
    ".wasm": "binary",
  },
  logLevel: "info",
});

console.log(`wrote ${out}`);

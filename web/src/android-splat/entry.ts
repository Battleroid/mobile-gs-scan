// Embedded splat viewer for the Android client.
//
// Bundled by web/scripts/bundle-android-splat.mjs into a single IIFE
// that's shipped inside `android/app/src/main/assets/splat/`. The
// Android `SplatViewerActivity` loads `index.html` via
// `WebViewAssetLoader` (cream-paper background, no chrome) and the
// HTML pulls in this bundle.
//
// Inputs come from the URL query string:
//   ?spz=<url>      required — splat asset, served by WebViewAssetLoader
//                   from app private storage (downloaded by the activity
//                   ahead of time).
//   &poster=<url>   optional — PNG/MP4 shown until the splat is on screen
//   &bg=<hex>       optional — clear color (default Pebble cream FBF6EC)
//
// Why a bundled in-app WebView and not native GLES: native renderer is
// a multi-week effort with no off-the-shelf Android library that
// supports .spz (see the PR-D research). The web app already ships a
// Spark-based viewer; reusing it here keeps visuals identical and the
// renderer maintained in one place. Compared to opening the system
// browser (the prior fallback), the bundled HTML has no nav chrome and
// loads instantly from disk.

import * as THREE from "three";
import { OrbitControls } from "three/examples/jsm/controls/OrbitControls.js";

declare global {
  interface Window {
    __pebbleSplat?: {
      ready: boolean;
      error?: string;
    };
  }
}

const params = new URLSearchParams(window.location.search);
const spzUrl = params.get("spz");
const bg = params.get("bg") ?? "#FBF6EC";

const status = document.getElementById("status") as HTMLDivElement | null;
const root = document.getElementById("root") as HTMLDivElement;

const setStatus = (s: string) => {
  if (status) status.textContent = s;
};
const setError = (err: string) => {
  setStatus(err);
  window.__pebbleSplat = { ready: false, error: err };
};

if (!spzUrl) {
  setError("missing ?spz=<url>");
  throw new Error("missing spz URL");
}

const renderer = new THREE.WebGLRenderer({
  antialias: true,
  // alpha=false lets the Pebble cream background paint through cleanly
  // without the WebView's transparent backdrop bleeding device chrome
  // through behind it.
  alpha: false,
  powerPreference: "high-performance",
});
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.setSize(window.innerWidth, window.innerHeight, false);
renderer.setClearColor(new THREE.Color(bg), 1);
root.appendChild(renderer.domElement);

const scene = new THREE.Scene();
scene.background = new THREE.Color(bg);

const camera = new THREE.PerspectiveCamera(
  50,
  window.innerWidth / window.innerHeight,
  0.01,
  500,
);
camera.position.set(0, 0, 3);

const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.dampingFactor = 0.08;
controls.zoomSpeed = 0.8;
controls.rotateSpeed = 0.6;
controls.panSpeed = 0.6;
// Splatfacto scenes are typically O(1m); cap the zoom range to keep
// the user inside something sensible without having to hand-tune per
// scene. The viewer activity can override by passing the splat bbox in
// a future iteration.
controls.minDistance = 0.05;
controls.maxDistance = 50;

setStatus("loading…");

void (async () => {
  try {
    const spark = await import("@sparkjsdev/spark");
    const sparkRenderer = new spark.SparkRenderer({
      renderer,
      // Spark's default sigma clip; 2.0 matches the web SplatViewer's
      // mid-quality. Tunable later via a settings chip if needed.
      maxStdDev: 2.0,
    });
    scene.add(sparkRenderer);

    const splat = new spark.SplatMesh({ url: spzUrl });
    scene.add(splat);

    // Center + frame the splat once it has positions. Spark resolves
    // its `initialized` promise after the binary parse + GPU upload
    // completes; we use it as the "first paint can happen" signal.
    const splatAny = splat as unknown as {
      initialized?: Promise<unknown>;
      computeBounds?: () => THREE.Box3 | null;
    };
    if (splatAny.initialized) {
      await splatAny.initialized;
    }
    // Best-effort bbox framing. Spark exposes a `computeBounds` helper
    // on SplatMesh in 0.1.x; if it ever disappears we just leave the
    // default camera and OrbitControls' targeting at the origin.
    try {
      const bbox = splatAny.computeBounds?.();
      if (bbox) {
        const center = bbox.getCenter(new THREE.Vector3());
        const size = bbox.getSize(new THREE.Vector3()).length();
        controls.target.copy(center);
        camera.position
          .copy(center)
          .add(new THREE.Vector3(0, 0, Math.max(size * 1.2, 0.5)));
        controls.update();
      }
    } catch {
      // bbox helper missing or threw — non-fatal.
    }

    setStatus("");
    window.__pebbleSplat = { ready: true };
  } catch (err) {
    setError(`splat load failed: ${(err as Error).message}`);
  }
})();

const onResize = () => {
  const w = window.innerWidth;
  const h = window.innerHeight;
  renderer.setSize(w, h, false);
  camera.aspect = w / h;
  camera.updateProjectionMatrix();
};
window.addEventListener("resize", onResize);

let raf = 0;
const animate = () => {
  raf = requestAnimationFrame(animate);
  controls.update();
  renderer.render(scene, camera);
};
animate();

// Activity-level navigation away (back press) destroys the WebView; we
// still clean up explicitly on pagehide so DevTools-attached debugging
// sessions don't leak a runaway rAF loop in the background.
window.addEventListener("pagehide", () => {
  cancelAnimationFrame(raf);
  renderer.dispose();
});

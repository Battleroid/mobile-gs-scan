"use client";
// Three.js + react-three-fiber wrapper around Spark
// (https://github.com/sparkjsdev/spark, npm: @sparkjsdev/spark).
//
// Spark exposes two THREE.js objects we add to the scene:
//   - SparkRenderer: hooks into the THREE.js render pipeline so
//     splats and regular meshes can intermix. Takes a `maxStdDev`
//     option (default ~3.0) that trades quality for performance —
//     higher means more gaussians evaluated per pixel.
//   - SplatMesh:     extends THREE.Object3D, loads a .ply / .spz /
//     .splat / .ksplat URL.
//
// We import dynamically because the package touches `window` at
// import time and would crash during Next.js's server-render pass.
//
// Controls overlay (top-right of the canvas):
//   - fullscreen     toggle the HTML fullscreen API on the
//                    container div
//   - new tab        open the same scene in a dedicated
//                    /viewer?url=... page (shareable URL)
//   - splats / points  toggle between the SplatMesh render and a
//                    THREE.Points cloud loaded from the same .ply
//                    via PLYLoader (debug / inspection view)
//   - quality        slider that maps to SparkRenderer.maxStdDev,
//                    1.0 → 6.0
import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import { Canvas, useThree } from "@react-three/fiber";
import { OrbitControls } from "@react-three/drei";
import * as THREE from "three";

interface Props {
  /** /api/scenes/<id>/artifacts/{ply,spz} URL for the splat. */
  url: string;
  /**
   * Optional .ply URL for the points-mode toggle. Recommended to
   * pass the .ply variant explicitly even when ``url`` points at
   * the .spz — the PLYLoader fallback can't read .spz.
   */
  pointsUrl?: string;
  className?: string;
  /**
   * When true the viewer fills its container and shows a minimal
   * close-button only (used by /viewer page). When false, normal
   * embedded mode with full controls overlay.
   */
  fillScreen?: boolean;
}

export function SplatViewer({ url, pointsUrl, className, fillScreen }: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [pointsMode, setPointsMode] = useState(false);
  const [maxStdDev, setMaxStdDev] = useState(3.0);
  const [isFullscreen, setIsFullscreen] = useState(false);

  const onFullscreen = useCallback(() => {
    const el = containerRef.current;
    if (!el) return;
    if (document.fullscreenElement) {
      document.exitFullscreen().catch(() => {});
    } else {
      el.requestFullscreen().catch(() => {});
    }
  }, []);

  useEffect(() => {
    const handler = () => setIsFullscreen(!!document.fullscreenElement);
    document.addEventListener("fullscreenchange", handler);
    return () => document.removeEventListener("fullscreenchange", handler);
  }, []);

  const onPopOut = useCallback(() => {
    const params = new URLSearchParams({ url });
    if (pointsUrl) params.set("pointsUrl", pointsUrl);
    window.open(`/viewer?${params.toString()}`, "_blank", "noopener");
  }, [url, pointsUrl]);

  return (
    <div
      ref={containerRef}
      className={
        className ??
        (fillScreen
          ? "fixed inset-0 bg-black"
          : "h-[60vh] w-full bg-black relative")
      }
    >
      <Canvas
        camera={{ position: [3, 2, 3], fov: 50, near: 0.05, far: 500 }}
        gl={{ antialias: false, powerPreference: "high-performance" }}
      >
        <color attach="background" args={["#0b0b0d"]} />
        <Suspense fallback={null}>
          <SplatScene
            url={url}
            pointsUrl={pointsUrl}
            pointsMode={pointsMode}
            maxStdDev={maxStdDev}
          />
        </Suspense>
        <OrbitControls makeDefault enableDamping target={[0, 0, 0]} />
      </Canvas>

      {/* Controls overlay. Top-right corner so it doesn't cover the
          subject most users orbit around the centre of. Translucent
          black bg + monospace text, matches the rest of the UI. */}
      <div className="absolute top-2 right-2 flex flex-col gap-2 bg-black/70 px-3 py-2 text-xs font-mono text-fg">
        <button
          type="button"
          onClick={onFullscreen}
          className="text-left hover:text-accent"
        >
          {isFullscreen ? "exit fullscreen" : "fullscreen"}
        </button>
        <button
          type="button"
          onClick={onPopOut}
          className="text-left hover:text-accent"
        >
          new tab ↗
        </button>
        <button
          type="button"
          onClick={() => setPointsMode((p) => !p)}
          className="text-left hover:text-accent"
        >
          {pointsMode ? "→ splats" : "→ points"}
        </button>
        <label className="flex flex-col gap-0.5">
          <span className="text-muted">quality {maxStdDev.toFixed(1)}</span>
          <input
            type="range"
            min="1"
            max="6"
            step="0.1"
            value={maxStdDev}
            onChange={(e) => setMaxStdDev(parseFloat(e.target.value))}
            className="w-32 accent-accent"
          />
        </label>
      </div>
    </div>
  );
}

function SplatScene({
  url,
  pointsUrl,
  pointsMode,
  maxStdDev,
}: {
  url: string;
  pointsUrl?: string;
  pointsMode: boolean;
  maxStdDev: number;
}) {
  const { gl, scene } = useThree();
  const groupRef = useRef<THREE.Group>(null);
  const [error, setError] = useState<string | null>(null);
  const splatRef = useRef<THREE.Object3D | null>(null);
  const sparkRef = useRef<THREE.Object3D | null>(null);
  const pointsRef = useRef<THREE.Points | null>(null);

  useEffect(() => {
    let cancelled = false;
    const groupAtMount = groupRef.current;

    (async () => {
      try {
        const mod = await import("@sparkjsdev/spark").catch(() => null);
        if (cancelled) return;

        // Always load the points view in parallel. We use
        // pointsUrl when provided (caller knows .ply is available),
        // otherwise fall back to the same url — PLYLoader will
        // succeed on .ply and fail on .spz, which we treat as
        // "no points view". Toggle hides the splat or vice versa.
        const sourceForPoints = pointsUrl ?? url;
        try {
          const { PLYLoader } = await import(
            "three/examples/jsm/loaders/PLYLoader.js"
          );
          const loader = new PLYLoader();
          const geom = await loader.loadAsync(sourceForPoints);
          if (cancelled) return;
          const points = new THREE.Points(
            geom,
            new THREE.PointsMaterial({
              size: 0.01,
              vertexColors: geom.hasAttribute("color"),
              color: geom.hasAttribute("color") ? 0xffffff : 0x66ccff,
            }),
          );
          points.visible = false;
          groupAtMount?.add(points);
          pointsRef.current = points;
        } catch {
          // Points-mode unavailable. Toggle still flips state but
          // there's nothing to show; splat path keeps working.
        }

        if (mod) {
          const { SparkRenderer, SplatMesh } = mod as {
            SparkRenderer: new (opts: {
              renderer: THREE.WebGLRenderer;
              maxStdDev?: number;
            }) => THREE.Object3D;
            SplatMesh: new (opts: { url: string }) => THREE.Object3D;
          };

          const spark = new SparkRenderer({ renderer: gl, maxStdDev });
          scene.add(spark);
          sparkRef.current = spark;

          const splat = new SplatMesh({ url });
          groupAtMount?.add(splat);
          splatRef.current = splat;
        } else if (pointsRef.current) {
          // Spark not resolvable — default the points view on so
          // the user sees something rather than a blank canvas.
          pointsRef.current.visible = true;
        }
      } catch (e) {
        setError((e as Error).message);
      }
    })();

    return () => {
      cancelled = true;
      if (splatRef.current) {
        groupAtMount?.remove(splatRef.current);
        const dispose = (splatRef.current as unknown as { dispose?: () => void }).dispose;
        try { dispose?.(); } catch { /* ignore */ }
      }
      if (sparkRef.current) {
        scene.remove(sparkRef.current);
        const dispose = (sparkRef.current as unknown as { dispose?: () => void }).dispose;
        try { dispose?.(); } catch { /* ignore */ }
      }
      if (pointsRef.current) {
        groupAtMount?.remove(pointsRef.current);
        pointsRef.current.geometry.dispose();
        (pointsRef.current.material as THREE.Material).dispose();
      }
      splatRef.current = null;
      sparkRef.current = null;
      pointsRef.current = null;
    };
  }, [url, pointsUrl, gl, scene]);

  // Apply pointsMode toggle without re-creating either object.
  useEffect(() => {
    if (splatRef.current) splatRef.current.visible = !pointsMode;
    if (pointsRef.current) pointsRef.current.visible = pointsMode;
  }, [pointsMode]);

  // Apply maxStdDev change. SparkRenderer exposes maxStdDev as a
  // settable property; if a future Spark version drops it the
  // assignment is a harmless no-op (caught below).
  useEffect(() => {
    const spark = sparkRef.current as unknown as { maxStdDev?: number } | null;
    if (spark) {
      try { spark.maxStdDev = maxStdDev; } catch { /* ignore */ }
    }
  }, [maxStdDev]);

  if (error) {
    return (
      <mesh>
        <boxGeometry args={[1, 1, 1]} />
        <meshBasicMaterial color="hotpink" wireframe />
      </mesh>
    );
  }
  return <group ref={groupRef} />;
}

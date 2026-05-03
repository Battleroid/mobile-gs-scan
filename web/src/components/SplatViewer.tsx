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
//   - view mode      three-way cycle: splats (default Spark
//                    render) → points (mono cyan PLY point cloud)
//                    → colored points (per-vertex color from the
//                    PLY when available, else mono fallback)
//   - quality        slider that maps to SparkRenderer.maxStdDev,
//                    1.0 → 6.0
import { Suspense, useCallback, useEffect, useRef, useState } from "react";
import { Canvas, useThree } from "@react-three/fiber";
import { OrbitControls, TransformControls } from "@react-three/drei";
import * as THREE from "three";

type ViewMode = "splats" | "points" | "colored";

/**
 * In-viewer selection widget driven by the editor panel. The viewer
 * renders a draggable wireframe box or sphere and reports its
 * world-space transform back to the parent on mouseup so the recipe
 * state stays canonical (recipe = source of truth, widget = editor).
 *
 *   - bbox: position = centre, scale = full extent (geometry is unit
 *           cube centred at origin, so scale.x is the full x extent
 *           — half-extent maths happens at the round-trip boundary).
 *   - sphere: position = centre, scale = diameter (geometry is a
 *             sphere of radius 0.5, so scale.x is the diameter).
 */
export type SelectionWidget =
  | {
      kind: "bbox";
      min: [number, number, number];
      max: [number, number, number];
    }
  | {
      kind: "sphere";
      center: [number, number, number];
      radius: number;
    };

export type WidgetMode = "translate" | "scale";

const VIEW_MODE_LABELS: Record<ViewMode, string> = {
  splats: "splats",
  points: "points",
  colored: "colored points",
};

// 3DGS / splatfacto store color as the DC term of an SH expansion.
// Inversion to linear RGB is `color = 0.5 + SH_C0 * f_dc` where
// SH_C0 = 1 / (2 * sqrt(pi)). Same constant Spark and inria's
// reference 3DGS renderer use.
const SH_C0 = 0.28209479177387814;

interface Props {
  /** /api/scenes/<id>/artifacts/{ply,spz} URL for the splat. */
  url: string;
  /**
   * Optional .ply URL for the points-mode views. Recommended to
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
  /**
   * Optional in-scene selection widget. When set, the viewer
   * renders a draggable wireframe gizmo over the splat; on
   * mouseup it reports the new transform back via
   * onSelectionCommit. Editor panels use this to author bbox /
   * sphere recipe ops visually.
   */
  selection?: SelectionWidget | null;
  onSelectionCommit?: (next: SelectionWidget) => void;
}

export function SplatViewer({
  url,
  pointsUrl,
  className,
  fillScreen,
  selection,
  onSelectionCommit,
}: Props) {
  const containerRef = useRef<HTMLDivElement>(null);
  const [viewMode, setViewMode] = useState<ViewMode>("splats");
  const [maxStdDev, setMaxStdDev] = useState(3.0);
  const [isFullscreen, setIsFullscreen] = useState(false);
  const [widgetMode, setWidgetMode] = useState<WidgetMode>("translate");

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

  // T / S toggle the widget mode while a selection is active. We
  // gate on the input target so typing in a number field elsewhere
  // on the page doesn't accidentally swap modes.
  useEffect(() => {
    if (!selection) return;
    const handler = (e: KeyboardEvent) => {
      const target = e.target as HTMLElement | null;
      if (target && (target.tagName === "INPUT" || target.tagName === "TEXTAREA")) {
        return;
      }
      if (e.key === "t" || e.key === "T") setWidgetMode("translate");
      if (e.key === "s" || e.key === "S") setWidgetMode("scale");
    };
    window.addEventListener("keydown", handler);
    return () => window.removeEventListener("keydown", handler);
  }, [selection]);

  const onPopOut = useCallback(() => {
    const params = new URLSearchParams({ url });
    if (pointsUrl) params.set("pointsUrl", pointsUrl);
    window.open(`/viewer?${params.toString()}`, "_blank", "noopener");
  }, [url, pointsUrl]);

  const cycleViewMode = useCallback(() => {
    setViewMode((m) =>
      m === "splats" ? "points" : m === "points" ? "colored" : "splats",
    );
  }, []);

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
            viewMode={viewMode}
            maxStdDev={maxStdDev}
          />
          {selection && onSelectionCommit && (
            <SelectionGizmo
              selection={selection}
              mode={widgetMode}
              onCommit={onSelectionCommit}
            />
          )}
        </Suspense>
        <OrbitControls makeDefault enableDamping target={[0, 0, 0]} />
      </Canvas>

      {selection && onSelectionCommit && (
        <div className="absolute top-2 left-2 flex flex-col gap-1 bg-black/70 px-3 py-2 text-xs font-mono text-fg">
          <span className="text-muted">
            editing {selection.kind} (drag handles to {widgetMode})
          </span>
          <div className="flex gap-2">
            <button
              type="button"
              onClick={() => setWidgetMode("translate")}
              className={
                widgetMode === "translate" ? "text-accent" : "hover:text-accent"
              }
            >
              translate (T)
            </button>
            <button
              type="button"
              onClick={() => setWidgetMode("scale")}
              className={
                widgetMode === "scale" ? "text-accent" : "hover:text-accent"
              }
            >
              scale (S)
            </button>
          </div>
        </div>
      )}

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
          onClick={cycleViewMode}
          className="text-left hover:text-accent"
          title="cycle view: splats → points → colored points"
        >
          view: {VIEW_MODE_LABELS[viewMode]}
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
  viewMode,
  maxStdDev,
}: {
  url: string;
  pointsUrl?: string;
  viewMode: ViewMode;
  maxStdDev: number;
}) {
  const { gl, scene } = useThree();
  const groupRef = useRef<THREE.Group>(null);
  const [error, setError] = useState<string | null>(null);
  const splatRef = useRef<THREE.Object3D | null>(null);
  const sparkRef = useRef<THREE.Object3D | null>(null);
  const pointsRef = useRef<THREE.Points | null>(null);
  const monoMaterialRef = useRef<THREE.PointsMaterial | null>(null);
  const coloredMaterialRef = useRef<THREE.PointsMaterial | null>(null);
  const hasVertexColorsRef = useRef(false);
  // The setup effect captures maxStdDev once at mount to seed the
  // SparkRenderer constructor. Subsequent slider drags shouldn't
  // tear the renderer down (would re-fetch + rebuild the splat on
  // every tick), so we DELIBERATELY exclude maxStdDev from the
  // setup-effect's deps array. The second effect at the bottom of
  // this component applies live changes by writing to
  // sparkRef.current.maxStdDev. This ref carries the current value
  // through to the setup effect without making it a dep — kept in
  // sync from inside that same effect so we don't write to it
  // during render (forbidden by react-hooks/refs).
  const maxStdDevRef = useRef(maxStdDev);

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
        // "no points view".
        const sourceForPoints = pointsUrl ?? url;
        try {
          const { PLYLoader } = await import(
            "three/examples/jsm/loaders/PLYLoader.js"
          );
          const loader = new PLYLoader();
          // Splatfacto / 3DGS .ply files store color as the DC
          // term of an SH expansion under the property names
          // f_dc_0/1/2 instead of the standard PLY red/green/
          // blue. Without this hint PLYLoader drops them and the
          // "colored points" view always falls back to mono cyan.
          // Pull them through as a 3-component custom attribute
          // we convert to a real `color` attribute below.
          loader.setCustomPropertyNameMapping({
            shDc: ["f_dc_0", "f_dc_1", "f_dc_2"],
          });
          const geom = await loader.loadAsync(sourceForPoints);
          if (cancelled) return;

          // Prefer a real RGB color attribute if the PLY had one
          // (e.g. a points3D.ply seed file from COLMAP). Otherwise
          // synthesize one from the SH DC coefficients with the
          // standard 3DGS inversion: `linear = 0.5 + SH_C0 * f_dc`.
          // Splatfacto can produce slightly out-of-[0,1] values;
          // clamp before assigning since PointsMaterial expects
          // normalized RGB.
          let hasColor = geom.hasAttribute("color");
          if (!hasColor && geom.hasAttribute("shDc")) {
            const sh = geom.getAttribute("shDc") as THREE.BufferAttribute;
            const colors = new Float32Array(sh.count * 3);
            for (let i = 0; i < sh.count; i++) {
              const r = 0.5 + SH_C0 * sh.getX(i);
              const g = 0.5 + SH_C0 * sh.getY(i);
              const b = 0.5 + SH_C0 * sh.getZ(i);
              colors[i * 3 + 0] = Math.max(0, Math.min(1, r));
              colors[i * 3 + 1] = Math.max(0, Math.min(1, g));
              colors[i * 3 + 2] = Math.max(0, Math.min(1, b));
            }
            geom.setAttribute(
              "color",
              new THREE.BufferAttribute(colors, 3),
            );
            // Drop the source attribute now that it's been
            // converted; nothing else reads it and it doubles
            // the per-vertex memory cost.
            geom.deleteAttribute("shDc");
            hasColor = true;
          }
          hasVertexColorsRef.current = hasColor;

          // Mono material: always available, used for "points" mode
          // and as the fallback for "colored" when the geometry has
          // no color attribute.
          const mono = new THREE.PointsMaterial({
            size: 0.01,
            vertexColors: false,
            color: 0x66ccff,
          });
          monoMaterialRef.current = mono;

          // Colored material: only meaningful when the PLY has a
          // color attribute (real RGB or synthesized from SH DC).
          // Allocate it anyway so the swap path is unconditional;
          // the "colored" view falls back to mono below when
          // hasColor is false.
          const colored = new THREE.PointsMaterial({
            size: 0.01,
            vertexColors: hasColor,
            color: 0xffffff,
          });
          coloredMaterialRef.current = colored;

          const points = new THREE.Points(geom, mono);
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

          const spark = new SparkRenderer({
            renderer: gl,
            maxStdDev: maxStdDevRef.current,
          });
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
      }
      monoMaterialRef.current?.dispose();
      coloredMaterialRef.current?.dispose();
      splatRef.current = null;
      sparkRef.current = null;
      pointsRef.current = null;
      monoMaterialRef.current = null;
      coloredMaterialRef.current = null;
    };
  }, [url, pointsUrl, gl, scene]);

  // Apply view-mode change without re-creating either object.
  // Swaps the points material reference for the colored variant so
  // we don't allocate per click.
  useEffect(() => {
    const splat = splatRef.current;
    const points = pointsRef.current;
    if (splat) splat.visible = viewMode === "splats";
    if (points) {
      points.visible = viewMode !== "splats";
      if (viewMode === "colored" && hasVertexColorsRef.current && coloredMaterialRef.current) {
        points.material = coloredMaterialRef.current;
      } else if (monoMaterialRef.current) {
        // "points" mode, or "colored" fallback when the PLY lacks
        // vertex colors.
        points.material = monoMaterialRef.current;
      }
    }
  }, [viewMode]);

  // Apply maxStdDev change. SparkRenderer exposes maxStdDev as a
  // settable property; if a future Spark version drops it the
  // assignment is a harmless no-op (caught below). Also keeps
  // maxStdDevRef in sync so the setup effect (which reads the
  // initial value at mount-time) sees the latest after a re-mount.
  useEffect(() => {
    maxStdDevRef.current = maxStdDev;
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

/**
 * In-scene draggable bbox / sphere widget driven by the editor.
 *
 * Geometry choice keeps the world-space ↔ recipe round-trip simple:
 *   - bbox uses a unit cube; mesh.scale.{x,y,z} = the full extent.
 *   - sphere uses a radius-0.5 sphere; mesh.scale.x = the diameter.
 *
 * We re-seed the mesh transform every time the incoming `selection`
 * key changes (recipe was applied, edit cleared, switched between
 * bbox/sphere, etc) so the gizmo follows the source of truth. Mid-
 * drag, TransformControls writes directly to the mesh; we only push
 * the new transform back to the recipe on dragging-changed=false to
 * avoid 60 fps recipe state churn.
 */
function SelectionGizmo({
  selection,
  mode,
  onCommit,
}: {
  selection: SelectionWidget;
  mode: WidgetMode;
  onCommit: (next: SelectionWidget) => void;
}) {
  // Use a state-tracked target rather than nesting <mesh> inside
  // <TransformControls>. drei v10's TransformControls binds via the
  // `object` prop; passing an external ref + a re-render trigger
  // (the useState below) is the documented "stable" way to attach
  // it. Nesting the mesh as a child caused the gizmo's pointer
  // listeners to detach on every parent re-render, which manifested
  // as "I can only interact while moving the mouse, no click ever
  // takes" — listeners were torn down between pointerdown and the
  // raycast that would have grabbed the handle.
  const [targetMesh, setTargetMesh] = useState<THREE.Mesh | null>(null);
  const meshCallbackRef = useCallback((node: THREE.Mesh | null) => {
    setTargetMesh(node);
  }, []);

  // Seed the mesh transform from the incoming selection. Runs after
  // mount + on selection identity change. TransformControls mutates
  // mesh.position/scale directly mid-drag, so we deliberately
  // re-seed only when the recipe-derived key changes (parent
  // commits a new value or switches kinds).
  const seedKey = JSON.stringify(selection);
  useEffect(() => {
    const m = targetMesh;
    if (!m) return;
    if (selection.kind === "bbox") {
      const [mnx, mny, mnz] = selection.min;
      const [mxx, mxy, mxz] = selection.max;
      m.position.set((mnx + mxx) / 2, (mny + mxy) / 2, (mnz + mxz) / 2);
      m.scale.set(
        Math.max(0.001, mxx - mnx),
        Math.max(0.001, mxy - mny),
        Math.max(0.001, mxz - mnz),
      );
    } else {
      m.position.set(...selection.center);
      const d = Math.max(0.001, selection.radius * 2);
      m.scale.set(d, d, d);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [seedKey, targetMesh]);

  const commit = useCallback(() => {
    const m = targetMesh;
    if (!m) return;
    if (selection.kind === "bbox") {
      const cx = m.position.x, cy = m.position.y, cz = m.position.z;
      const sx = Math.abs(m.scale.x) / 2;
      const sy = Math.abs(m.scale.y) / 2;
      const sz = Math.abs(m.scale.z) / 2;
      onCommit({
        kind: "bbox",
        min: [cx - sx, cy - sy, cz - sz],
        max: [cx + sx, cy + sy, cz + sz],
      });
    } else {
      // Sphere is uniformly scaled even if the user wrangled a
      // non-uniform scale: collapse to the average so the recipe
      // stays a true sphere.
      const avg =
        (Math.abs(m.scale.x) + Math.abs(m.scale.y) + Math.abs(m.scale.z)) / 3;
      onCommit({
        kind: "sphere",
        center: [m.position.x, m.position.y, m.position.z],
        radius: avg / 2,
      });
    }
  }, [selection.kind, onCommit, targetMesh]);

  // dragging-changed → commit on mouse-up + manually pause/resume
  // OrbitControls. The auto-pause that drei is supposed to wire up
  // when both controls have makeDefault doesn't fire reliably here
  // (presumably because TC and OC fight over which is the "default"
  // when both ask for it), so OrbitControls was happily eating the
  // drag — handles highlighted on click but the camera rotated
  // instead of the gizmo moving. Reach into useThree's controls
  // slot and flip enabled directly.
  const tcRef = useRef<{
    addEventListener: (k: string, fn: (e: { value: boolean }) => void) => void;
    removeEventListener: (k: string, fn: (e: { value: boolean }) => void) => void;
  } | null>(null);
  const setTcRef = useCallback((v: unknown) => {
    tcRef.current = v as typeof tcRef.current;
  }, []);
  const { invalidate, controls } = useThree();
  useEffect(() => {
    const tc = tcRef.current;
    if (!tc) return;
    const onDrag = (e: { value: boolean }) => {
      // Toggle the OrbitControls (or whatever is registered as the
      // default Drei controls) on/off based on whether we're
      // mid-drag. Doing it manually because drei's auto-pause was
      // unreliable in this combo of versions.
      const oc = controls as { enabled?: boolean } | null;
      if (oc && "enabled" in oc) {
        oc.enabled = !e.value;
      }
      if (!e.value) commit();
      invalidate();
    };
    tc.addEventListener("dragging-changed", onDrag);
    return () => {
      tc.removeEventListener("dragging-changed", onDrag);
      // If the gizmo unmounts mid-drag we'd otherwise leave
      // OrbitControls disabled forever.
      const oc = controls as { enabled?: boolean } | null;
      if (oc && "enabled" in oc) oc.enabled = true;
    };
  }, [commit, invalidate, targetMesh, controls]);

  const color = selection.kind === "bbox" ? "#ffae42" : "#ff5fa2";

  return (
    <>
      <mesh ref={meshCallbackRef} raycast={NOOP_RAYCAST}>
        {selection.kind === "bbox" ? (
          <boxGeometry args={[1, 1, 1]} />
        ) : (
          <sphereGeometry args={[0.5, 24, 16]} />
        )}
        <meshBasicMaterial color={color} wireframe />
      </mesh>
      {targetMesh && (
        <TransformControls
          ref={setTcRef}
          object={targetMesh}
          mode={mode}
          size={1.6}
          space="world"
        />
      )}
    </>
  );
}

// Hoisted so it's reference-stable across renders.
const NOOP_RAYCAST = () => {};

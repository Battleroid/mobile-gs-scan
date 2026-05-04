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

type ViewMode = "splats" | "points" | "colored" | "mesh";

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

/**
 * Mutable handle the points geometry exposes to the SelectionGizmo
 * so the gizmo can paint points inside the bbox / sphere yellow on
 * every drag tick. Stored as a ref so writes don't trigger React
 * renders. SplatScene writes; SelectionGizmo reads.
 */
export interface PointsHighlightApi {
  /** xyz triples — read-only. */
  positions: Float32Array;
  /** rgb triples used as the base palette (cyan in mono mode,
   *  SH-derived RGB in colored mode). The gizmo writes selectively
   *  into ``liveColors`` and falls back to this for outside points. */
  baseColors: Float32Array;
  /** rgb triples actually rendered — gizmo overwrites then sets
   *  attribute.needsUpdate. */
  liveColors: Float32Array;
  /** the BufferAttribute backing liveColors. */
  attribute: THREE.BufferAttribute;
  /** Set by SelectionGizmo while a widget is mounted; cleared on
   *  unmount. SplatScene calls this after its view-mode palette
   *  reset so the yellow mask gets re-applied immediately rather
   *  than waiting for the next TC change event (which doesn't
   *  fire on viewMode swaps). */
  repaint?: () => void;
}

// Yellow used to paint points inside the active bbox / sphere
// widget. Matches the bbox widget's orange family without being
// too close to it; the magenta sphere widget's color contrasts
// fine against it too.
const HIGHLIGHT_RGB = [1.0, 0.85, 0.0] as const;

const VIEW_MODE_LABELS: Record<ViewMode, string> = {
  splats: "splats",
  points: "points",
  colored: "colored points",
  mesh: "mesh",
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
  /**
   * Optional .glb / .obj URL for the mesh view-mode. When either
   * is set, the viewer's mode-cycle gains a 4th "mesh" option
   * that hides the splats / points and renders the reconstructed
   * surface instead. GLB is preferred when both are available
   * (faster GLTFLoader, materials handled uniformly); OBJ is
   * the fallback.
   */
  meshGlbUrl?: string;
  meshObjUrl?: string;
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
  meshGlbUrl,
  meshObjUrl,
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
  // Shared by SplatScene (writes the points geometry handle) and
  // SelectionGizmo (reads positions + writes highlight colors). Ref
  // rather than state so the per-tick highlight redraws don't
  // re-render React.
  const highlightApiRef = useRef<PointsHighlightApi | null>(null);

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

  // Mesh URL is required for the mesh mode to be meaningful — skip
  // the mode in the cycle when no mesh has been extracted yet so
  // the user doesn't land on an empty canvas.
  const meshAvailable = !!(meshGlbUrl || meshObjUrl);
  const cycleViewMode = useCallback(() => {
    setViewMode((m) => {
      if (m === "splats") return "points";
      if (m === "points") return "colored";
      if (m === "colored") return meshAvailable ? "mesh" : "splats";
      return "splats";
    });
  }, [meshAvailable]);
  // If the mesh disappears (discarded mid-session) while we're in
  // mesh mode, bounce back to splats. Render-time adjust pattern.
  const [prevMeshAvail, setPrevMeshAvail] = useState(meshAvailable);
  if (prevMeshAvail !== meshAvailable) {
    setPrevMeshAvail(meshAvailable);
    if (!meshAvailable && viewMode === "mesh") setViewMode("splats");
  }

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
        {/* Lights only matter for the mesh view-mode (Spark + the
            points materials render unlit); cheap to leave in
            permanently. */}
        <ambientLight intensity={0.4} />
        <directionalLight position={[5, 5, 5]} intensity={0.9} />
        <directionalLight position={[-5, -3, -5]} intensity={0.4} />
        <Suspense fallback={null}>
          <SplatScene
            url={url}
            pointsUrl={pointsUrl}
            meshGlbUrl={meshGlbUrl}
            meshObjUrl={meshObjUrl}
            viewMode={viewMode}
            maxStdDev={maxStdDev}
            highlightApiRef={highlightApiRef}
          />
          {selection && onSelectionCommit && (
            <SelectionGizmo
              selection={selection}
              mode={widgetMode}
              onCommit={onSelectionCommit}
              highlightApiRef={highlightApiRef}
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
  meshGlbUrl,
  meshObjUrl,
  viewMode,
  maxStdDev,
  highlightApiRef,
}: {
  url: string;
  pointsUrl?: string;
  meshGlbUrl?: string;
  meshObjUrl?: string;
  viewMode: ViewMode;
  maxStdDev: number;
  highlightApiRef: React.MutableRefObject<PointsHighlightApi | null>;
}) {
  const { gl, scene } = useThree();
  const groupRef = useRef<THREE.Group>(null);
  const [error, setError] = useState<string | null>(null);
  const splatRef = useRef<THREE.Object3D | null>(null);
  const sparkRef = useRef<THREE.Object3D | null>(null);
  const pointsRef = useRef<THREE.Points | null>(null);
  // Mesh object loaded for the "mesh" view-mode. Lazy-attached when
  // the user first switches to the mode (or remounts on URL change).
  const meshRef = useRef<THREE.Object3D | null>(null);
  const monoMaterialRef = useRef<THREE.PointsMaterial | null>(null);
  const coloredMaterialRef = useRef<THREE.PointsMaterial | null>(null);
  const hasVertexColorsRef = useRef(false);
  // Base color palettes per view mode. The active geometry color
  // attribute is the "live" buffer; viewMode switches and gizmo
  // highlights both write into it. Restored to monoBase /
  // coloredBase on view-mode change.
  const monoBaseRef = useRef<Float32Array | null>(null);
  const coloredBaseRef = useRef<Float32Array | null>(null);
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
          // Capture the SH-derived RGB into a separate Float32Array
          // we can use as the "colored mode" base palette; the
          // active color attribute on the geometry becomes a
          // mutable live buffer that the SelectionGizmo overwrites
          // when highlighting points inside the active bbox / sphere.
          let coloredBase: Float32Array | null = null;
          if (geom.hasAttribute("color")) {
            // Real RGB attribute (e.g. points3D.ply seed file).
            const c = geom.getAttribute("color") as THREE.BufferAttribute;
            coloredBase = new Float32Array(c.array as Float32Array);
          } else if (geom.hasAttribute("shDc")) {
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
            coloredBase = new Float32Array(colors);
          }
          hasVertexColorsRef.current = hasColor;

          // Build a mono base palette (uniform cyan per-vertex). We
          // use a per-vertex color attribute even in mono mode so
          // the SelectionGizmo can paint individual points yellow
          // without having to thrash material types.
          const positionAttr = geom.getAttribute("position") as THREE.BufferAttribute;
          const monoBase = new Float32Array(positionAttr.count * 3);
          // 0x66ccff → r=0.4, g=0.8, b=1.0
          for (let i = 0; i < positionAttr.count; i++) {
            monoBase[i * 3 + 0] = 0.4;
            monoBase[i * 3 + 1] = 0.8;
            monoBase[i * 3 + 2] = 1.0;
          }
          // If we don't already have a color attribute (no SH, no
          // RGB), seed the live one with the mono palette.
          if (!geom.hasAttribute("color")) {
            geom.setAttribute(
              "color",
              new THREE.BufferAttribute(new Float32Array(monoBase), 3),
            );
          }
          monoBaseRef.current = monoBase;
          coloredBaseRef.current = coloredBase;

          // Both materials now use vertexColors=true; the difference
          // between mono / colored modes is which base palette gets
          // copied into the geometry's color attribute on view-mode
          // change. A separate "live" material per mode would make
          // the highlight gizmo's life harder.
          const mono = new THREE.PointsMaterial({
            size: 0.01,
            vertexColors: true,
            color: 0xffffff,
          });
          monoMaterialRef.current = mono;

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
          // Publish the highlight handle for SelectionGizmo. Default
          // base = mono palette since the initial view is splats /
          // points (mono). Updated on every viewMode swap below.
          const colorAttr = geom.getAttribute("color") as THREE.BufferAttribute;
          highlightApiRef.current = {
            positions: positionAttr.array as Float32Array,
            baseColors: monoBase,
            liveColors: colorAttr.array as Float32Array,
            attribute: colorAttr,
          };
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
      monoBaseRef.current = null;
      coloredBaseRef.current = null;
      highlightApiRef.current = null;
    };
    // highlightApiRef is a stable mutable ref; the eslint rule
    // wants it listed even though it doesn't gate effect re-runs.
  }, [url, pointsUrl, gl, scene, highlightApiRef]);

  // Apply view-mode change without re-creating either object.
  // Swaps the points material AND the live color buffer's contents
  // so the gizmo highlight + the new base palette agree. Also
  // toggles the mesh's visibility — its lazy load is handled by a
  // separate effect below.
  useEffect(() => {
    const splat = splatRef.current;
    const points = pointsRef.current;
    const mesh = meshRef.current;
    if (splat) splat.visible = viewMode === "splats";
    if (mesh) mesh.visible = viewMode === "mesh";
    if (!points) return;
    points.visible = viewMode === "points" || viewMode === "colored";

    const useColored =
      viewMode === "colored" &&
      hasVertexColorsRef.current &&
      coloredMaterialRef.current !== null;
    const material = useColored
      ? coloredMaterialRef.current
      : monoMaterialRef.current;
    if (material) points.material = material;

    // Decide the active base palette and copy it into the live
    // color buffer so the new view starts un-highlighted. Update
    // the SelectionGizmo's handle so a subsequent highlight pass
    // writes against the correct base.
    const base =
      useColored && coloredBaseRef.current !== null
        ? coloredBaseRef.current
        : monoBaseRef.current;
    const api = highlightApiRef.current;
    if (api && base) {
      api.liveColors.set(base);
      api.baseColors = base;
      api.attribute.needsUpdate = true;
      // If a SelectionGizmo is mounted, ask it to re-apply the
      // yellow mask against the freshly-restored base palette.
      // Without this, switching mono ↔ colored ↔ splats with an
      // active widget would clear the highlight until the user
      // touched the gizmo again.
      api.repaint?.();
    }
  }, [viewMode, highlightApiRef]);

  // Lazy-load the reconstructed mesh when needed and when its url
  // changes. We DON'T load on mount because the user may never
  // switch into mesh mode; loading 1M-triangle GLBs eagerly would
  // be a waste.
  const meshUrl = meshGlbUrl ?? meshObjUrl ?? null;
  const meshKind: "glb" | "obj" | null = meshGlbUrl
    ? "glb"
    : meshObjUrl
      ? "obj"
      : null;
  useEffect(() => {
    if (!meshUrl || !meshKind) {
      // No mesh available: tear down anything from a prior URL.
      const stale = meshRef.current;
      if (stale) {
        groupRef.current?.remove(stale);
        _disposeMeshTree(stale);
        meshRef.current = null;
      }
      return;
    }

    let cancelled = false;
    const groupAtMount = groupRef.current;
    let added: THREE.Object3D | null = null;

    (async () => {
      try {
        if (meshKind === "glb") {
          const { GLTFLoader } = await import(
            "three/examples/jsm/loaders/GLTFLoader.js"
          );
          const gltf = await new GLTFLoader().loadAsync(meshUrl);
          if (cancelled) return;
          added = gltf.scene;
        } else {
          const { OBJLoader } = await import(
            "three/examples/jsm/loaders/OBJLoader.js"
          );
          const obj = await new OBJLoader().loadAsync(meshUrl);
          if (cancelled) return;
          // OBJLoader doesn't apply a default material when the .mtl
          // is missing — surfaces render black under our lighting.
          // Drop a flat grey lambert so the mesh always shows up.
          obj.traverse((child) => {
            if ((child as THREE.Mesh).isMesh) {
              const m = child as THREE.Mesh;
              m.material = new THREE.MeshStandardMaterial({
                color: 0xb8bcc2,
                roughness: 0.85,
                metalness: 0.05,
              });
            }
          });
          added = obj;
        }
        if (added && groupAtMount) {
          // Mesh starts hidden; the view-mode effect above flips
          // visibility based on the current mode (so loading a
          // mesh while in splats mode doesn't pop it onscreen).
          added.visible = viewMode === "mesh";
          groupAtMount.add(added);
          meshRef.current = added;
        }
      } catch (e) {
        console.error("mesh load failed", e);
      }
    })();

    return () => {
      cancelled = true;
      if (added && groupAtMount) {
        groupAtMount.remove(added);
        _disposeMeshTree(added);
      }
      if (meshRef.current === added) meshRef.current = null;
    };
    // viewMode intentionally excluded — toggling modes shouldn't
    // re-fetch the mesh; visibility-only is handled by the other
    // effect.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [meshUrl, meshKind]);

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
  highlightApiRef,
}: {
  selection: SelectionWidget;
  mode: WidgetMode;
  onCommit: (next: SelectionWidget) => void;
  highlightApiRef: React.MutableRefObject<PointsHighlightApi | null>;
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

  // Hoisted out of the OC-coordination block below because the
  // highlight repainter needs `invalidate` and runs earlier in
  // this function. `controls` + `gl` are still consumed by the
  // OC-pause effects further down.
  const { invalidate, controls, gl } = useThree();

  // Repaint the highlight from the live mesh transform. Hoisted out
  // of the listener-effect below so the seed effect can call it too:
  // form-driven changes (number-field edits, Reset, recipe re-seed)
  // update mesh.position/scale via the seed but never fire a TC
  // 'change' event, so the previous version left the yellow mask
  // stale until the user touched the gizmo again.
  const repaintHighlight = useCallback(() => {
    const m = targetMesh;
    if (!m) return;
    const api = highlightApiRef.current;
    if (!api) return;
    const { positions, baseColors, liveColors, attribute } = api;
    const cx = m.position.x, cy = m.position.y, cz = m.position.z;
    // mesh.scale is the unit cube's full extent / sphere's
    // diameter; halve to compare against |p - center|. Math.abs
    // guards against an in-progress negative scale (commit pins
    // to abs on drag-end but the live mesh can be negative
    // mid-drag in scale mode).
    const sx = Math.abs(m.scale.x) / 2;
    const sy = Math.abs(m.scale.y) / 2;
    const sz = Math.abs(m.scale.z) / 2;
    const r =
      (Math.abs(m.scale.x) + Math.abs(m.scale.y) + Math.abs(m.scale.z)) / 6;
    const r2 = r * r;
    const isBbox = selection.kind === "bbox";
    const n = positions.length / 3;
    const [hr, hg, hb] = HIGHLIGHT_RGB;
    for (let i = 0; i < n; i++) {
      const px = positions[i * 3];
      const py = positions[i * 3 + 1];
      const pz = positions[i * 3 + 2];
      let inside: boolean;
      if (isBbox) {
        inside =
          Math.abs(px - cx) <= sx &&
          Math.abs(py - cy) <= sy &&
          Math.abs(pz - cz) <= sz;
      } else {
        const dx = px - cx, dy = py - cy, dz = pz - cz;
        inside = dx * dx + dy * dy + dz * dz <= r2;
      }
      if (inside) {
        liveColors[i * 3] = hr;
        liveColors[i * 3 + 1] = hg;
        liveColors[i * 3 + 2] = hb;
      } else {
        liveColors[i * 3] = baseColors[i * 3];
        liveColors[i * 3 + 1] = baseColors[i * 3 + 1];
        liveColors[i * 3 + 2] = baseColors[i * 3 + 2];
      }
    }
    attribute.needsUpdate = true;
    invalidate();
  }, [selection.kind, targetMesh, invalidate, highlightApiRef]);

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
    // The seed just moved the mesh — repaint so the yellow mask
    // matches the new bounds without waiting for a TC interaction.
    repaintHighlight();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [seedKey, targetMesh, repaintHighlight]);

  const commit = useCallback(() => {
    const m = targetMesh;
    if (!m) return;
    // Normalize the mesh to abs scale BEFORE building the recipe.
    // three.js TransformControls in scale mode lets the user drag
    // a handle past origin, which leaves mesh.scale negative; the
    // gizmo then renders its arrows mirror-flipped because the
    // group is mirrored along that axis. The recipe was already
    // pulling Math.abs() so the saved bounds were correct, but the
    // mesh kept the negative scale, so the next drag started with
    // an inverted arrow. Pin the live mesh transform to positive
    // here so the very next render shows the gizmo right way round.
    m.scale.set(
      Math.abs(m.scale.x) || 0.001,
      Math.abs(m.scale.y) || 0.001,
      Math.abs(m.scale.z) || 0.001,
    );
    if (selection.kind === "bbox") {
      const cx = m.position.x, cy = m.position.y, cz = m.position.z;
      const sx = m.scale.x / 2;
      const sy = m.scale.y / 2;
      const sz = m.scale.z / 2;
      onCommit({
        kind: "bbox",
        min: [cx - sx, cy - sy, cz - sz],
        max: [cx + sx, cy + sy, cz + sz],
      });
    } else {
      // Sphere is uniformly scaled even if the user wrangled a
      // non-uniform scale: collapse to the average so the recipe
      // stays a true sphere.
      const avg = (m.scale.x + m.scale.y + m.scale.z) / 3;
      onCommit({
        kind: "sphere",
        center: [m.position.x, m.position.y, m.position.z],
        radius: avg / 2,
      });
    }
  }, [selection.kind, onCommit, targetMesh]);

  // OrbitControls coordination is gated on whether the pointerdown
  // landed on a TC handle, NOT on hover. Earlier attempts:
  //
  // - Listening to `dragging-changed`: OC's pointerdown handler
  //   runs before TC's (both listen on the same canvas; OC was
  //   registered first because TC mounts after the targetMesh
  //   state pattern), so OC starts orbiting before we can flip
  //   its enabled flag.
  // - Listening to TC's `change` event (axis or dragging flip):
  //   solves the pointerdown timing because hover sets axis well
  //   before click, but then breaks an in-progress orbit when
  //   the user's cursor passes over a handle mid-drag. OC's
  //   pointermove bails as soon as enabled flips false.
  //
  // Capture-phase listener on the canvas runs BEFORE any bubble-
  // phase same-element listeners (including OC's pointerdown).
  // At that moment, TC's hover state has already populated
  // `tc.axis` from prior pointermove events, so we can tell
  // whether the click is on a handle and only disable OC then.
  // A mid-orbit cursor pass over a handle never triggers a
  // pointerdown, so OC keeps running.
  type TcInstance = {
    addEventListener: (k: string, fn: (e: { value?: boolean }) => void) => void;
    removeEventListener: (k: string, fn: (e: { value?: boolean }) => void) => void;
    axis: string | null;
    dragging: boolean;
  };
  const tcRef = useRef<TcInstance | null>(null);
  const setTcRef = useCallback((v: unknown) => {
    tcRef.current = v as TcInstance | null;
  }, []);
  // Read the default controls (OrbitControls, the only one with
  // makeDefault) through a ref so the helper below doesn't capture
  // `controls` as a dep — that rerouting keeps the
  // react-hooks/immutability lint happy with the imperative mutation
  // of `controls.enabled` we have to do here. There's no reactive
  // setter on three.js OrbitControls; toggling .enabled directly is
  // the documented API. Sync via effect to dodge the
  // "Cannot update refs during render" rule.
  const controlsRef = useRef<unknown>(null);
  useEffect(() => {
    controlsRef.current = controls;
  }, [controls]);
  const setOcEnabled = useCallback((enabled: boolean) => {
    const oc = controlsRef.current as { enabled?: boolean } | null;
    if (oc && "enabled" in oc) {
      oc.enabled = enabled;
    }
  }, []);
  useEffect(() => {
    const canvas = gl.domElement;
    const onPointerDown = (event: PointerEvent) => {
      const tc = tcRef.current;
      // Only the left button starts a TransformControls drag —
      // right/middle clicks would otherwise disable OC without
      // ever firing dragging-changed=false to re-enable it,
      // stranding the orbit broken for the rest of the session.
      if (event.button !== 0) return;
      // tc.axis is non-null when the cursor is over a handle; TC
      // has already set it from earlier pointermove processing.
      if (tc && tc.axis !== null) {
        setOcEnabled(false);
        // Belt-and-braces: even on a left-button down we might
        // not get a drag-end if the user releases without
        // moving (TC starts dragging on first move). Hook a
        // one-shot pointerup that always re-enables OC if the
        // drag listener didn't already do it. document-level so
        // we catch releases off-canvas too.
        const onUp = () => {
          setOcEnabled(true);
          document.removeEventListener("pointerup", onUp);
          document.removeEventListener("pointercancel", onUp);
        };
        document.addEventListener("pointerup", onUp);
        document.addEventListener("pointercancel", onUp);
      }
    };
    canvas.addEventListener("pointerdown", onPointerDown, { capture: true });
    return () => {
      canvas.removeEventListener("pointerdown", onPointerDown, {
        capture: true,
      });
    };
  }, [gl, setOcEnabled]);

  useEffect(() => {
    const tc = tcRef.current;
    if (!tc) return;
    const onDrag = (e: { value?: boolean }) => {
      if (e.value === false) {
        // Drag ended — commit the recipe and restore OC. (If the
        // drag never started because the click missed all handles,
        // this branch doesn't fire and OC stays enabled.)
        commit();
        setOcEnabled(true);
        invalidate();
      }
    };
    tc.addEventListener("dragging-changed", onDrag);
    return () => {
      tc.removeEventListener("dragging-changed", onDrag);
      // Belt-and-braces: an unmount mid-drag without a final
      // drag-end event would otherwise leave OC stranded.
      setOcEnabled(true);
    };
  }, [commit, invalidate, targetMesh, setOcEnabled]);

  // Highlight points inside the active widget yellow on every TC
  // change (hover, drag, axis swap). Calls the hoisted
  // repaintHighlight helper that the seed effect also uses for
  // form-driven updates.
  useEffect(() => {
    const tc = tcRef.current;
    if (!tc) return;
    if (!targetMesh) return;
    const onChange = () => repaintHighlight();
    // First pass on mount + every TC change (hover, drag, axis
    // swap). 'change' fires often during a drag — that's what we
    // want for live tracking, and at ~1M points a single pass is
    // ~10 ms on a modern machine.
    onChange();
    tc.addEventListener("change", onChange);
    // Register so SplatScene's view-mode effect can re-apply the
    // mask after a palette reset (mono↔colored↔splats swaps
    // overwrite the live buffer with the base palette and would
    // otherwise drop the yellow until the next TC interaction).
    // Capture the ref into the closure so the cleanup uses the
    // same handle the effect saw at fire-time. SplatScene only
    // replaces highlightApiRef.current on a full URL/Spark
    // remount, but capturing here silences the stale-ref lint
    // rule and reads cleaner.
    const apiAtMount = highlightApiRef.current;
    if (apiAtMount) apiAtMount.repaint = repaintHighlight;
    return () => {
      tc.removeEventListener("change", onChange);
      // Restore base palette so deactivating the widget removes
      // the yellow highlight.
      if (apiAtMount) {
        apiAtMount.repaint = undefined;
        apiAtMount.liveColors.set(apiAtMount.baseColors);
        apiAtMount.attribute.needsUpdate = true;
        invalidate();
      }
    };
  }, [targetMesh, invalidate, highlightApiRef, repaintHighlight]);

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

// Recursively dispose geometry + material(s) attached to every
// THREE.Mesh under the given root. Both loaders (GLTF / OBJ) pull
// in non-trivial geometry buffers, so failing to dispose on
// teardown leaks GPU memory across re-extracts.
function _disposeMeshTree(root: THREE.Object3D): void {
  root.traverse((child) => {
    const m = child as THREE.Mesh;
    if (m.geometry) m.geometry.dispose();
    if (m.material) {
      const mat = m.material as THREE.Material | THREE.Material[];
      if (Array.isArray(mat)) mat.forEach((x) => x.dispose());
      else mat.dispose();
    }
  });
}

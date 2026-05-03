"use client";
// On-demand Poisson mesh extraction panel. Renders below the
// SplatViewer + SplatEditor on a completed scene. Lets the user
// kick off ns-export poisson with a few tunables, watches progress
// over the scene WS (mirrored on scene.mesh_progress to match the
// filter pattern), and renders the resulting OBJ / GLB inline once
// it lands.
//
// The mesh is reconstructed from the trained Gaussian-splatting
// checkpoint, NOT from the (filtered) PLY — same source as the
// viewer's splats. That keeps it independent of the edit pipeline:
// you can mesh + filter in either order, and discarding the edit
// doesn't touch the mesh.
import { Suspense, useEffect, useMemo, useRef, useState } from "react";
import { Canvas, useThree } from "@react-three/fiber";
import { OrbitControls } from "@react-three/drei";
import * as THREE from "three";
import { api } from "@/lib/api";
import type { MeshParams, MeshStatus, Scene } from "@/lib/types";

const NORMAL_METHODS: MeshParams["normal_method"][] = [
  "open3d",
  "open3d_with_normals",
];

interface ParamsState {
  num_points: number;
  remove_outliers: boolean;
  normal_method: NonNullable<MeshParams["normal_method"]>;
  use_bounding_box: boolean;
}

const DEFAULT_PARAMS: ParamsState = {
  num_points: 1_000_000,
  remove_outliers: true,
  normal_method: "open3d",
  use_bounding_box: false,
};

function paramsFromScene(scene: Scene): ParamsState {
  const p = scene.mesh_params ?? {};
  return {
    num_points: p.num_points ?? DEFAULT_PARAMS.num_points,
    remove_outliers: p.remove_outliers ?? DEFAULT_PARAMS.remove_outliers,
    normal_method:
      (p.normal_method as ParamsState["normal_method"]) ??
      DEFAULT_PARAMS.normal_method,
    use_bounding_box: p.use_bounding_box ?? DEFAULT_PARAMS.use_bounding_box,
  };
}

interface Props {
  scene: Scene;
  meshProgress: { progress: number; message: string | null } | null;
}

export function MeshPanel({ scene, meshProgress }: Props) {
  const [params, setParams] = useState<ParamsState>(() => paramsFromScene(scene));
  const [submitting, setSubmitting] = useState(false);
  const [discarding, setDiscarding] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Re-seed when the server-side params change (recipe applied,
  // discard, etc). Same render-time pattern as SplatEditor to dodge
  // react-hooks/set-state-in-effect.
  const paramsKey = JSON.stringify(scene.mesh_params ?? null);
  const [prevParamsKey, setPrevParamsKey] = useState(paramsKey);
  if (prevParamsKey !== paramsKey) {
    setPrevParamsKey(paramsKey);
    setParams(paramsFromScene(scene));
  }

  const status: MeshStatus = scene.mesh_status;
  const isRunning = status === "queued" || status === "running";
  const hasMesh = status === "completed" && !!scene.mesh_obj_url;
  const progressPct = Math.round((meshProgress?.progress ?? 0) * 100);

  const onExtract = async () => {
    setSubmitting(true);
    setError(null);
    try {
      await api.triggerSceneMesh(scene.id, params);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSubmitting(false);
    }
  };

  const onDiscard = async () => {
    if (
      !window.confirm(
        "Discard this mesh? The .obj / .glb will be removed; the splat is untouched.",
      )
    ) {
      return;
    }
    setDiscarding(true);
    setError(null);
    try {
      await api.clearSceneMesh(scene.id);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setDiscarding(false);
    }
  };

  const meshUrl = useMemo(() => {
    if (!hasMesh) return null;
    // Prefer GLB when available — three.js's GLTFLoader is faster +
    // handles materials uniformly, and the worker emits both when
    // trimesh is installed. Fall back to OBJ otherwise.
    const rel = scene.mesh_glb_url ?? scene.mesh_obj_url;
    return rel ? api.base() + rel : null;
  }, [hasMesh, scene.mesh_glb_url, scene.mesh_obj_url]);
  const meshKind: "glb" | "obj" = scene.mesh_glb_url ? "glb" : "obj";

  return (
    <section className="border border-rule p-4 space-y-4">
      <header>
        <h2 className="text-sm text-muted">extract mesh (poisson)</h2>
      </header>

      {hasMesh && meshUrl && (
        <MeshViewer url={meshUrl} kind={meshKind} />
      )}

      {isRunning && (
        <div className="space-y-1 text-xs">
          <p className="text-warn">
            extracting… {progressPct}%
            {meshProgress?.message ? ` · ${meshProgress.message}` : ""}
          </p>
          <div className="h-1 bg-rule">
            <div
              className="h-full bg-accent transition-all"
              style={{ width: `${progressPct}%` }}
            />
          </div>
        </div>
      )}

      {status === "failed" && scene.mesh_error && (
        <p className="text-xs text-danger">last extract failed: {scene.mesh_error}</p>
      )}

      <fieldset className="space-y-3" disabled={isRunning || submitting}>
        <legend className="text-xs text-muted">params</legend>
        <label
          className="flex flex-col gap-0.5 text-xs"
          title="Number of points sampled from the trained Gaussians for Poisson surface reconstruction. More points = denser, more detailed mesh, but longer extraction + larger output."
        >
          <span className="text-muted">point sample count</span>
          <input
            type="number"
            value={params.num_points}
            min={10_000}
            step={50_000}
            onChange={(e) => {
              const n = parseInt(e.target.value, 10);
              if (Number.isFinite(n) && n > 0) {
                setParams((s) => ({ ...s, num_points: n }));
              }
            }}
            className="w-32 bg-transparent border-b border-rule px-1 focus:outline-none focus:border-accent"
          />
        </label>
        <label
          className="flex items-center gap-2 text-sm cursor-pointer"
          title="Run Open3D's statistical outlier removal on the sampled point cloud before reconstruction. Cleans up most spurious surfaces; turn off if your scene has thin geometry that gets shaved."
        >
          <input
            type="checkbox"
            checked={params.remove_outliers}
            onChange={(e) =>
              setParams((s) => ({ ...s, remove_outliers: e.target.checked }))
            }
            className="accent-accent"
          />
          remove outliers
        </label>
        <label
          className="flex flex-col gap-0.5 text-xs"
          title="How point normals are estimated. open3d uses a local PCA fit; open3d_with_normals reuses any normals attached to the trained gaussians (better when the scan covered most viewing angles)."
        >
          <span className="text-muted">normal method</span>
          <select
            value={params.normal_method}
            onChange={(e) =>
              setParams((s) => ({
                ...s,
                normal_method: e.target.value as ParamsState["normal_method"],
              }))
            }
            className="w-48 bg-transparent border-b border-rule px-1 focus:outline-none focus:border-accent"
          >
            {NORMAL_METHODS.map((m) => (
              <option key={m} value={m}>
                {m}
              </option>
            ))}
          </select>
        </label>
        <label
          className="flex items-center gap-2 text-sm cursor-pointer"
          title="Crop point sampling to the trained scene bbox. Helpful when stray gaussians sit far outside the subject; only enable if your training run set sensible scene bounds."
        >
          <input
            type="checkbox"
            checked={params.use_bounding_box}
            onChange={(e) =>
              setParams((s) => ({ ...s, use_bounding_box: e.target.checked }))
            }
            className="accent-accent"
          />
          use trained bounding box
        </label>
      </fieldset>

      <div className="flex flex-wrap items-center gap-3 text-xs">
        <button
          type="button"
          onClick={onExtract}
          disabled={isRunning || submitting}
          className="border border-rule px-3 py-1 hover:bg-rule disabled:opacity-50"
        >
          {submitting
            ? "queueing…"
            : hasMesh
              ? "re-extract"
              : "extract mesh"}
        </button>
        {hasMesh && (
          <button
            type="button"
            onClick={onDiscard}
            disabled={isRunning || discarding}
            className="text-danger underline hover:text-fg disabled:opacity-40"
          >
            {discarding ? "discarding…" : "discard mesh"}
          </button>
        )}
        {scene.mesh_obj_url && (
          <a
            href={api.base() + scene.mesh_obj_url}
            download
            className="underline hover:text-fg"
          >
            download .obj
          </a>
        )}
        {scene.mesh_glb_url && (
          <a
            href={api.base() + scene.mesh_glb_url}
            download
            className="underline hover:text-fg"
          >
            download .glb
          </a>
        )}
        {error && <span className="text-danger">{error}</span>}
      </div>
    </section>
  );
}

function MeshViewer({ url, kind }: { url: string; kind: "glb" | "obj" }) {
  return (
    <div className="h-[60vh] w-full bg-black relative">
      <Canvas
        camera={{ position: [3, 2, 3], fov: 50, near: 0.05, far: 500 }}
        gl={{ antialias: true, powerPreference: "high-performance" }}
      >
        <color attach="background" args={["#0b0b0d"]} />
        <ambientLight intensity={0.4} />
        <directionalLight position={[5, 5, 5]} intensity={0.9} />
        <directionalLight position={[-5, -3, -5]} intensity={0.4} />
        <Suspense fallback={null}>
          <MeshContent url={url} kind={kind} />
        </Suspense>
        <OrbitControls makeDefault enableDamping target={[0, 0, 0]} />
      </Canvas>
    </div>
  );
}

function MeshContent({ url, kind }: { url: string; kind: "glb" | "obj" }) {
  const { scene } = useThree();
  const groupRef = useRef<THREE.Group>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    const groupAtMount = groupRef.current;
    let added: THREE.Object3D | null = null;

    (async () => {
      try {
        if (kind === "glb") {
          const { GLTFLoader } = await import(
            "three/examples/jsm/loaders/GLTFLoader.js"
          );
          const loader = new GLTFLoader();
          const gltf = await loader.loadAsync(url);
          if (cancelled) return;
          added = gltf.scene;
        } else {
          const { OBJLoader } = await import(
            "three/examples/jsm/loaders/OBJLoader.js"
          );
          const loader = new OBJLoader();
          const obj = await loader.loadAsync(url);
          if (cancelled) return;
          // OBJLoader doesn't apply a default material when the .mtl
          // is missing — surfaces render black under our lighting.
          // Drop a flat grey lambert across every mesh so the user
          // gets a usable preview either way.
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
          groupAtMount.add(added);
        }
      } catch (e) {
        setError((e as Error).message);
      }
    })();

    return () => {
      cancelled = true;
      if (added && groupAtMount) {
        groupAtMount.remove(added);
        added.traverse((child) => {
          const m = child as THREE.Mesh;
          if (m.geometry) m.geometry.dispose();
          if (m.material) {
            const mat = m.material as THREE.Material | THREE.Material[];
            if (Array.isArray(mat)) mat.forEach((x) => x.dispose());
            else mat.dispose();
          }
        });
      }
    };
  }, [url, kind, scene]);

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

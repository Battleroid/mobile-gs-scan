"use client";
// Live-preview filter editor for a finalized splat.
//
// Cheap ops (opacity threshold, bbox crop, sphere remove) preview in
// the browser by toggling per-gaussian opacity on Spark's SplatMesh —
// no GPU buffer re-upload, no server roundtrip. Heavy ops (scale clamp,
// SOR, DBSCAN) are server-only; the UI shows a hint and applies them
// only when the user clicks "Apply & save".
//
// On Apply, we PUT the recipe to /api/scenes/{id}/edit and switch the
// viewer source to the edited artifact once the worker reports done.
import { Suspense, useCallback, useEffect, useMemo, useRef, useState } from "react";
import { Canvas, useThree } from "@react-three/fiber";
import { OrbitControls } from "@react-three/drei";
import * as THREE from "three";
import type { FilterRecipe, FilterRecipeOp, Scene } from "@/lib/types";
import { api } from "@/lib/api";

interface OpacityState {
  enabled: boolean;
  min: number; // alpha threshold, 0..1
}
interface BBoxState {
  enabled: boolean;
  min: [number, number, number];
  max: [number, number, number];
}
interface SphereState {
  enabled: boolean;
  center: [number, number, number];
  radius: number;
}
interface ScaleClampState {
  enabled: boolean;
  maxScale: number;
}
interface SORState {
  enabled: boolean;
  k: number;
  stdMul: number;
}
interface DBSCANState {
  enabled: boolean;
  eps: number;
  minSamples: number;
}

interface RecipeState {
  opacity: OpacityState;
  bbox: BBoxState;
  sphere: SphereState;
  scaleClamp: ScaleClampState;
  sor: SORState;
  dbscan: DBSCANState;
}

const DEFAULT_RECIPE: RecipeState = {
  opacity: { enabled: true, min: 0.05 },
  bbox: { enabled: false, min: [-2, -1, -2], max: [2, 2, 2] },
  sphere: { enabled: false, center: [0, 0, 0], radius: 0.3 },
  scaleClamp: { enabled: false, maxScale: 0.5 },
  sor: { enabled: false, k: 24, stdMul: 2.0 },
  dbscan: { enabled: false, eps: 0.05, minSamples: 30 },
};

function buildRecipe(state: RecipeState): FilterRecipe {
  const ops: FilterRecipeOp[] = [];
  if (state.opacity.enabled) {
    ops.push({ type: "opacity_threshold", min: state.opacity.min });
  }
  if (state.scaleClamp.enabled) {
    ops.push({ type: "scale_clamp", max_scale: state.scaleClamp.maxScale });
  }
  if (state.bbox.enabled) {
    ops.push({ type: "bbox_crop", min: state.bbox.min, max: state.bbox.max });
  }
  if (state.sphere.enabled) {
    ops.push({
      type: "sphere_remove",
      center: state.sphere.center,
      radius: state.sphere.radius,
    });
  }
  if (state.sor.enabled) {
    ops.push({ type: "sor", k: state.sor.k, std_multiplier: state.sor.stdMul });
  }
  if (state.dbscan.enabled) {
    ops.push({
      type: "dbscan_keep_largest",
      eps: state.dbscan.eps,
      min_samples: state.dbscan.minSamples,
    });
  }
  return { ops };
}

interface Props {
  scene: Scene;
}

export function SplatEditor({ scene }: Props) {
  const baselineUrl = api.base() + (scene.spz_url ?? scene.ply_url ?? "");
  const editedUrl = scene.edited_spz_url
    ? api.base() + scene.edited_spz_url
    : scene.edited_ply_url
      ? api.base() + scene.edited_ply_url
      : null;
  // Keep the live-preview ply URL stable — Spark would otherwise refetch
  // on edited-url swaps, defeating the point of an instant preview.
  const livePlyUrl = scene.ply_url ? api.base() + scene.ply_url : null;

  const [view, setView] = useState<"baseline" | "preview" | "edited">(
    editedUrl ? "edited" : "baseline",
  );
  const [recipe, setRecipe] = useState<RecipeState>(DEFAULT_RECIPE);
  const [applying, setApplying] = useState(false);
  const [discarding, setDiscarding] = useState(false);
  const [actionError, setActionError] = useState<string | null>(null);
  const [previewStats, setPreviewStats] = useState<{
    kept: number;
    total: number;
  } | null>(null);

  const filterJob = useMemo(
    () =>
      [...scene.jobs]
        .reverse()
        .find((j) => j.kind === "filter") ?? null,
    [scene.jobs],
  );

  // When the server edit completes, swap the viewer to the edited artifact.
  // Intentional state-update-in-effect: the only signal that a server-side
  // filter just produced a new artifact is the websocket-driven prop flip,
  // and we want a one-time view transition reacting to it.
  useEffect(() => {
    if (scene.edit_status === "completed" && editedUrl && !applying) {
      // eslint-disable-next-line react-hooks/set-state-in-effect
      setView("edited");
    }
  }, [scene.edit_status, editedUrl, applying]);

  async function onApply() {
    setApplying(true);
    setActionError(null);
    try {
      await api.upsertSceneEdit(scene.id, buildRecipe(recipe));
    } catch (e) {
      setActionError((e as Error).message);
    } finally {
      setApplying(false);
    }
  }

  async function onDiscard() {
    setDiscarding(true);
    setActionError(null);
    try {
      await api.clearSceneEdit(scene.id);
      setView("baseline");
    } catch (e) {
      setActionError((e as Error).message);
    } finally {
      setDiscarding(false);
    }
  }

  function onReset() {
    setRecipe(DEFAULT_RECIPE);
    setPreviewStats(null);
  }

  function nukeOrigin() {
    setRecipe((s) => ({
      ...s,
      sphere: { enabled: true, center: [0, 0, 0], radius: 0.5 },
    }));
    setView("preview");
  }

  const editing = view === "preview";
  const viewerUrl =
    view === "edited" && editedUrl
      ? editedUrl
      : view === "baseline"
        ? baselineUrl
        : livePlyUrl ?? baselineUrl;

  return (
    <div className="space-y-3">
      <div className="flex items-center gap-3 text-xs">
        <span className="text-muted">view:</span>
        <ViewToggle
          label="original"
          active={view === "baseline"}
          onClick={() => setView("baseline")}
        />
        <ViewToggle
          label="live preview"
          active={view === "preview"}
          onClick={() => setView("preview")}
          disabled={!livePlyUrl}
        />
        <ViewToggle
          label="edited"
          active={view === "edited"}
          onClick={() => setView("edited")}
          disabled={!editedUrl}
        />
        {filterJob &&
          (filterJob.status === "running" ||
            filterJob.status === "queued" ||
            filterJob.status === "claimed") && (
            <span className="text-warn ml-auto">
              filtering… {Math.round(filterJob.progress * 100)}%
              {filterJob.progress_msg && ` · ${filterJob.progress_msg}`}
            </span>
          )}
      </div>

      <div className="h-[60vh] w-full bg-black">
        {editing && livePlyUrl ? (
          <Canvas
            camera={{ position: [3, 2, 3], fov: 50, near: 0.05, far: 500 }}
            gl={{ antialias: false, powerPreference: "high-performance" }}
          >
            <color attach="background" args={["#0b0b0d"]} />
            <Suspense fallback={null}>
              <PreviewSplatScene
                url={livePlyUrl}
                recipe={recipe}
                onStats={setPreviewStats}
              />
            </Suspense>
            <OrbitControls makeDefault enableDamping target={[0, 0, 0]} />
          </Canvas>
        ) : (
          <Canvas
            key={viewerUrl}
            camera={{ position: [3, 2, 3], fov: 50, near: 0.05, far: 500 }}
            gl={{ antialias: false, powerPreference: "high-performance" }}
          >
            <color attach="background" args={["#0b0b0d"]} />
            <Suspense fallback={null}>
              <SparkSplatScene url={viewerUrl} />
            </Suspense>
            <OrbitControls makeDefault enableDamping target={[0, 0, 0]} />
          </Canvas>
        )}
      </div>

      <div className="border border-rule p-4 space-y-4 text-sm">
        <h3 className="text-xs uppercase tracking-wider text-muted">
          edit / clean up the splat
        </h3>

        <fieldset className="space-y-2">
          <legend className="text-xs text-muted">
            cheap filters (live preview)
          </legend>

          <ToggleRow
            label="opacity threshold"
            enabled={recipe.opacity.enabled}
            onToggle={(v) =>
              setRecipe((s) => ({ ...s, opacity: { ...s.opacity, enabled: v } }))
            }
          >
            <NumberInput
              value={recipe.opacity.min}
              min={0}
              max={1}
              step={0.01}
              onChange={(v) =>
                setRecipe((s) => ({
                  ...s,
                  opacity: { ...s.opacity, min: v },
                }))
              }
            />
          </ToggleRow>

          <ToggleRow
            label="bounding box"
            enabled={recipe.bbox.enabled}
            onToggle={(v) =>
              setRecipe((s) => ({ ...s, bbox: { ...s.bbox, enabled: v } }))
            }
          >
            <Vec3Input
              label="min"
              value={recipe.bbox.min}
              onChange={(v) =>
                setRecipe((s) => ({ ...s, bbox: { ...s.bbox, min: v } }))
              }
            />
            <Vec3Input
              label="max"
              value={recipe.bbox.max}
              onChange={(v) =>
                setRecipe((s) => ({ ...s, bbox: { ...s.bbox, max: v } }))
              }
            />
          </ToggleRow>

          <ToggleRow
            label="sphere remove"
            enabled={recipe.sphere.enabled}
            onToggle={(v) =>
              setRecipe((s) => ({ ...s, sphere: { ...s.sphere, enabled: v } }))
            }
          >
            <Vec3Input
              label="center"
              value={recipe.sphere.center}
              onChange={(v) =>
                setRecipe((s) => ({
                  ...s,
                  sphere: { ...s.sphere, center: v },
                }))
              }
            />
            <NumberInput
              label="radius"
              value={recipe.sphere.radius}
              min={0}
              step={0.05}
              onChange={(v) =>
                setRecipe((s) => ({
                  ...s,
                  sphere: { ...s.sphere, radius: v },
                }))
              }
            />
            <button
              type="button"
              onClick={nukeOrigin}
              className="text-xs underline hover:text-fg ml-auto"
            >
              nuke origin cloud
            </button>
          </ToggleRow>
        </fieldset>

        <fieldset className="space-y-2">
          <legend className="text-xs text-muted">
            heavier filters (server-only — preview after Apply)
          </legend>
          <ToggleRow
            label="scale clamp"
            enabled={recipe.scaleClamp.enabled}
            onToggle={(v) =>
              setRecipe((s) => ({
                ...s,
                scaleClamp: { ...s.scaleClamp, enabled: v },
              }))
            }
          >
            <NumberInput
              label="max"
              value={recipe.scaleClamp.maxScale}
              min={0}
              step={0.05}
              onChange={(v) =>
                setRecipe((s) => ({
                  ...s,
                  scaleClamp: { ...s.scaleClamp, maxScale: v },
                }))
              }
            />
          </ToggleRow>
          <ToggleRow
            label="outlier removal (SOR)"
            enabled={recipe.sor.enabled}
            onToggle={(v) =>
              setRecipe((s) => ({ ...s, sor: { ...s.sor, enabled: v } }))
            }
          >
            <NumberInput
              label="k"
              value={recipe.sor.k}
              min={1}
              step={1}
              onChange={(v) =>
                setRecipe((s) => ({ ...s, sor: { ...s.sor, k: v } }))
              }
            />
            <NumberInput
              label="σ-mult"
              value={recipe.sor.stdMul}
              min={0}
              step={0.1}
              onChange={(v) =>
                setRecipe((s) => ({ ...s, sor: { ...s.sor, stdMul: v } }))
              }
            />
          </ToggleRow>
          <ToggleRow
            label="DBSCAN keep-largest"
            enabled={recipe.dbscan.enabled}
            onToggle={(v) =>
              setRecipe((s) => ({
                ...s,
                dbscan: { ...s.dbscan, enabled: v },
              }))
            }
          >
            <NumberInput
              label="ε"
              value={recipe.dbscan.eps}
              min={0}
              step={0.01}
              onChange={(v) =>
                setRecipe((s) => ({
                  ...s,
                  dbscan: { ...s.dbscan, eps: v },
                }))
              }
            />
            <NumberInput
              label="min-samples"
              value={recipe.dbscan.minSamples}
              min={1}
              step={1}
              onChange={(v) =>
                setRecipe((s) => ({
                  ...s,
                  dbscan: { ...s.dbscan, minSamples: v },
                }))
              }
            />
          </ToggleRow>
        </fieldset>

        <div className="flex flex-wrap gap-3 items-center pt-2">
          <button
            type="button"
            onClick={onApply}
            disabled={applying || scene.edit_status === "running"}
            className="border border-rule px-3 py-1 text-xs hover:bg-rule disabled:opacity-50"
          >
            apply &amp; save
          </button>
          <button
            type="button"
            onClick={onReset}
            className="text-xs underline hover:text-fg"
          >
            reset
          </button>
          {scene.edit_status !== "none" && (
            <button
              type="button"
              onClick={onDiscard}
              disabled={discarding}
              className="text-xs underline hover:text-danger disabled:opacity-50"
            >
              discard edit
            </button>
          )}
          <span className="ml-auto text-xs text-muted">
            {previewStats &&
              editing &&
              `preview: ${previewStats.kept.toLocaleString()} / ${previewStats.total.toLocaleString()}`}
          </span>
        </div>

        {actionError && (
          <p className="text-danger text-xs">{actionError}</p>
        )}
        {scene.edit_status === "failed" && scene.edit_error && (
          <p className="text-danger text-xs">
            edit failed: {scene.edit_error}
          </p>
        )}
      </div>

      <div className="flex gap-3 text-xs">
        {scene.ply_url && (
          <a
            href={api.base() + scene.ply_url}
            download
            className="underline hover:text-fg"
          >
            download original .ply
          </a>
        )}
        {scene.edited_ply_url && (
          <a
            href={api.base() + scene.edited_ply_url}
            download
            className="underline hover:text-fg"
          >
            download edited .ply
          </a>
        )}
        {scene.spz_url && (
          <a
            href={api.base() + scene.spz_url}
            download
            className="underline hover:text-fg"
          >
            download original .spz
          </a>
        )}
        {scene.edited_spz_url && (
          <a
            href={api.base() + scene.edited_spz_url}
            download
            className="underline hover:text-fg"
          >
            download edited .spz
          </a>
        )}
      </div>
    </div>
  );
}

function ViewToggle({
  label,
  active,
  onClick,
  disabled,
}: {
  label: string;
  active: boolean;
  onClick: () => void;
  disabled?: boolean;
}) {
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={disabled}
      className={
        "border px-2 py-0.5 " +
        (active
          ? "border-fg text-fg"
          : "border-rule text-muted hover:text-fg") +
        (disabled ? " opacity-50 cursor-not-allowed" : "")
      }
    >
      {label}
    </button>
  );
}

function ToggleRow({
  label,
  enabled,
  onToggle,
  children,
}: {
  label: string;
  enabled: boolean;
  onToggle: (v: boolean) => void;
  children: React.ReactNode;
}) {
  return (
    <div className="flex items-center gap-3 flex-wrap">
      <label className="flex items-center gap-2 min-w-[12rem]">
        <input
          type="checkbox"
          checked={enabled}
          onChange={(e) => onToggle(e.target.checked)}
        />
        <span>{label}</span>
      </label>
      <div className="flex items-center gap-2 flex-wrap">{children}</div>
    </div>
  );
}

function NumberInput({
  value,
  onChange,
  label,
  ...rest
}: {
  value: number;
  onChange: (v: number) => void;
  label?: string;
} & Omit<React.InputHTMLAttributes<HTMLInputElement>, "value" | "onChange">) {
  return (
    <label className="flex items-center gap-1 text-xs">
      {label && <span className="text-muted">{label}</span>}
      <input
        type="number"
        value={value}
        onChange={(e) => onChange(Number(e.target.value))}
        className="w-20 bg-transparent border border-rule px-1 py-0.5 text-xs"
        {...rest}
      />
    </label>
  );
}

function Vec3Input({
  label,
  value,
  onChange,
}: {
  label: string;
  value: [number, number, number];
  onChange: (v: [number, number, number]) => void;
}) {
  return (
    <span className="flex items-center gap-1 text-xs">
      <span className="text-muted">{label}</span>
      {(["x", "y", "z"] as const).map((axis, i) => (
        <input
          key={axis}
          type="number"
          step={0.05}
          value={value[i]}
          onChange={(e) => {
            const next: [number, number, number] = [...value];
            next[i] = Number(e.target.value);
            onChange(next);
          }}
          className="w-16 bg-transparent border border-rule px-1 py-0.5 text-xs"
        />
      ))}
    </span>
  );
}

function SparkSplatScene({ url }: { url: string }) {
  const { gl, scene } = useThree();
  const groupRef = useRef<THREE.Group>(null);

  useEffect(() => {
    let cancelled = false;
    let spark: THREE.Object3D | null = null;
    let splat: THREE.Object3D | null = null;
    let fallback: THREE.Points | null = null;
    const groupAtMount = groupRef.current;

    (async () => {
      try {
        const mod = await import("@sparkjsdev/spark").catch(async () => {
          const { PLYLoader } = await import(
            "three/examples/jsm/loaders/PLYLoader.js"
          );
          const loader = new PLYLoader();
          const geom = await loader.loadAsync(url);
          if (cancelled) return null;
          fallback = new THREE.Points(
            geom,
            new THREE.PointsMaterial({
              size: 0.02,
              vertexColors: geom.hasAttribute("color"),
              color: geom.hasAttribute("color") ? 0xffffff : 0x66ccff,
            }),
          );
          groupAtMount?.add(fallback);
          return null;
        });
        if (!mod || cancelled) return;

        const { SparkRenderer, SplatMesh } = mod as {
          SparkRenderer: new (opts: { renderer: THREE.WebGLRenderer }) => THREE.Object3D;
          SplatMesh: new (opts: { url: string }) => THREE.Object3D;
        };

        spark = new SparkRenderer({ renderer: gl });
        scene.add(spark);
        splat = new SplatMesh({ url });
        groupAtMount?.add(splat);
      } catch {
        // viewer dispose paths handle fall-throughs.
      }
    })();

    return () => {
      cancelled = true;
      if (splat) {
        groupAtMount?.remove(splat);
        try { (splat as unknown as { dispose?: () => void }).dispose?.(); } catch {}
      }
      if (spark) {
        scene.remove(spark);
        try { (spark as unknown as { dispose?: () => void }).dispose?.(); } catch {}
      }
      if (fallback) {
        groupAtMount?.remove(fallback);
        fallback.geometry.dispose();
        (fallback.material as THREE.Material).dispose();
      }
    };
  }, [url, gl, scene]);

  return <group ref={groupRef} />;
}

interface PLYAttrs {
  count: number;
  xyz: Float32Array; // 3*N
  opacity: Float32Array; // raw logits, length N
}

function PreviewSplatScene({
  url,
  recipe,
  onStats,
}: {
  url: string;
  recipe: RecipeState;
  onStats: (s: { kept: number; total: number }) => void;
}) {
  const { gl, scene } = useThree();
  const groupRef = useRef<THREE.Group>(null);
  const splatRef = useRef<unknown>(null);
  const attrsRef = useRef<PLYAttrs | null>(null);
  const fallbackRef = useRef<{
    points: THREE.Points;
    sizes: Float32Array;
  } | null>(null);

  const applyMask = useCallback(
    (state: RecipeState) => {
      const attrs = attrsRef.current;
      if (!attrs) return;
      const { count, xyz, opacity } = attrs;
      const mask = new Uint8Array(count);
      let kept = 0;
      const op = state.opacity;
      const bb = state.bbox;
      const sp = state.sphere;
      const r2 = sp.radius * sp.radius;
      for (let i = 0; i < count; i++) {
        let keep = true;
        if (op.enabled) {
          const a = 1 / (1 + Math.exp(-opacity[i]));
          if (a <= op.min) keep = false;
        }
        const x = xyz[i * 3];
        const y = xyz[i * 3 + 1];
        const z = xyz[i * 3 + 2];
        if (keep && bb.enabled) {
          if (
            x < bb.min[0] || x > bb.max[0] ||
            y < bb.min[1] || y > bb.max[1] ||
            z < bb.min[2] || z > bb.max[2]
          ) {
            keep = false;
          }
        }
        if (keep && sp.enabled) {
          const dx = x - sp.center[0];
          const dy = y - sp.center[1];
          const dz = z - sp.center[2];
          if (dx * dx + dy * dy + dz * dz <= r2) keep = false;
        }
        mask[i] = keep ? 1 : 0;
        if (keep) kept++;
      }
      onStats({ kept, total: count });

      const splat = splatRef.current as
        | { packedSplats?: { setSplat?: (i: number, ...rest: unknown[]) => void } }
        | null;
      if (
        splat &&
        typeof splat === "object" &&
        splat.packedSplats &&
        typeof splat.packedSplats.setSplat === "function"
      ) {
        for (let i = 0; i < count; i++) {
          try {
            (splat.packedSplats as unknown as {
              setSplat: (i: number, opts: { opacity?: number }) => void;
            }).setSplat(i, {
              opacity: mask[i] ? 1 / (1 + Math.exp(-opacity[i])) : 0,
            });
          } catch {
            break;
          }
        }
        return;
      }

      const fb = fallbackRef.current;
      if (fb) {
        const { points } = fb;
        const pos = points.geometry.getAttribute(
          "position",
        ) as THREE.BufferAttribute;
        for (let i = 0; i < count; i++) {
          if (!mask[i]) {
            pos.setXYZ(i, NaN, NaN, NaN); // NaN positions get culled by the GPU
          } else {
            pos.setXYZ(i, xyz[i * 3], xyz[i * 3 + 1], xyz[i * 3 + 2]);
          }
        }
        pos.needsUpdate = true;
      }
    },
    [onStats],
  );

  useEffect(() => {
    let cancelled = false;
    let spark: THREE.Object3D | null = null;
    let splat: THREE.Object3D | null = null;
    const groupAtMount = groupRef.current;

    (async () => {
      try {
        const { PLYLoader } = await import(
          "three/examples/jsm/loaders/PLYLoader.js"
        );
        const loader = new PLYLoader();
        const geom = await loader.loadAsync(url);
        if (cancelled) return;
        const pos = geom.getAttribute("position");
        const opacityAttr = geom.getAttribute("opacity");
        const n = pos.count;
        const xyz = new Float32Array(n * 3);
        for (let i = 0; i < n; i++) {
          xyz[i * 3] = pos.getX(i);
          xyz[i * 3 + 1] = pos.getY(i);
          xyz[i * 3 + 2] = pos.getZ(i);
        }
        const opacity = new Float32Array(n);
        if (opacityAttr) {
          for (let i = 0; i < n; i++) opacity[i] = opacityAttr.getX(i);
        } else {
          opacity.fill(2.2); // sigmoid(2.2) ≈ 0.9
        }
        attrsRef.current = { count: n, xyz, opacity };

        const mod = await import("@sparkjsdev/spark").catch(() => null);
        if (mod && !cancelled) {
          const { SparkRenderer, SplatMesh } = mod as {
            SparkRenderer: new (opts: { renderer: THREE.WebGLRenderer }) => THREE.Object3D;
            SplatMesh: new (opts: { url: string }) => THREE.Object3D;
          };
          spark = new SparkRenderer({ renderer: gl });
          scene.add(spark);
          splat = new SplatMesh({ url });
          groupAtMount?.add(splat);
          splatRef.current = splat;
        } else if (!cancelled) {
          const sizes = new Float32Array(n);
          sizes.fill(2.0);
          geom.setAttribute("size", new THREE.BufferAttribute(sizes, 1));
          const mat = new THREE.PointsMaterial({
            size: 0.02,
            vertexColors: geom.hasAttribute("color"),
            color: geom.hasAttribute("color") ? 0xffffff : 0x66ccff,
            sizeAttenuation: true,
          });
          const points = new THREE.Points(geom, mat);
          fallbackRef.current = { points, sizes };
          groupAtMount?.add(points);
        }
        applyMask(recipe);
      } catch {
        // render nothing — caller's empty Canvas is fine
      }
    })();

    return () => {
      cancelled = true;
      if (splat) {
        groupAtMount?.remove(splat);
        try { (splat as unknown as { dispose?: () => void }).dispose?.(); } catch {}
      }
      if (spark) {
        scene.remove(spark);
        try { (spark as unknown as { dispose?: () => void }).dispose?.(); } catch {}
      }
      if (fallbackRef.current) {
        const { points } = fallbackRef.current;
        groupAtMount?.remove(points);
        points.geometry.dispose();
        (points.material as THREE.Material).dispose();
        fallbackRef.current = null;
      }
      attrsRef.current = null;
      splatRef.current = null;
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [url, gl, scene]);

  useEffect(() => {
    const handle = setTimeout(() => applyMask(recipe), 80);
    return () => clearTimeout(handle);
  }, [recipe, applyMask]);

  return <group ref={groupRef} />;
}

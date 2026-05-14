"use client";
// On-demand mesh extraction panel. Renders below the SplatViewer +
// SplatEditor on a completed scene. Lets the user kick off the
// worker's mesh pipeline with a few tunables, watches progress
// over the scene WS (mirrored on scene.mesh_progress to match the
// filter pattern). The mesh is VIEWED in the main SplatViewer via
// the "mesh" view-mode — this panel only owns the controls.
//
// Tiered: the user picks a fidelity tier (low / standard / higher).
// **Low** is TSDF fusion of gsplat-rendered RGB+depth views from
// the trained splatfacto checkpoint — output is a vertex-colored
// OBJ + GLB, fast.
// **Standard** is the OpenMVS classical photogrammetry pipeline
// (InterfaceCOLMAP → DensifyPointCloud → ReconstructMesh →
// RefineMesh → TextureMesh) — output is a UV-mapped textured OBJ
// + MTL + JPG bundle, slower but visually crisp.
// **Higher** (2DGS / SuGaR retrain) is still scaffolded for a
// future PR and renders as a "coming soon" chip.
//
// Independent of the edit pipeline: you can mesh + filter in
// either order, and discarding the edit doesn't touch the mesh.
import { useState } from "react";
import { api } from "@/lib/api";
import type { MeshStatus, Scene } from "@/lib/types";
import { BigButton, Eyebrow } from "@/components/pebble";

type MeshTier = "low" | "standard" | "higher";

type MvsTextureSize = 1024 | 2048 | 4096 | 8192;
const MVS_TEXTURE_SIZES: MvsTextureSize[] = [1024, 2048, 4096, 8192];

interface ParamsState {
  tier: MeshTier;
  // Low-tier (TSDF) knobs.
  n_views: number;
  remove_outliers: boolean;
  use_bounding_box: boolean;
  // Low-tier quality knobs (the "advanced" section). Documented
  // in worker/app/pipeline/mesh.py DEFAULT_PARAMS.
  use_edited_splat: boolean;
  alpha_min: number;
  bbox_percentile_low: number;
  bbox_percentile_high: number;
  floater_opacity_min: number;
  floater_scale_max_pct: number;
  // Standard-tier (OpenMVS) knobs.
  mvs_dense_views: number;
  mvs_texture_size: MvsTextureSize;
  mvs_refine_iters: number;
}

const DEFAULT_PARAMS: ParamsState = {
  tier: "low",
  n_views: 96,
  remove_outliers: true,
  use_bounding_box: false,
  use_edited_splat: true,
  alpha_min: 0.5,
  bbox_percentile_low: 10,
  bbox_percentile_high: 90,
  floater_opacity_min: 0.05,
  floater_scale_max_pct: 95,
  mvs_dense_views: 3,
  mvs_texture_size: 4096,
  mvs_refine_iters: 2,
};

// Tier labels keyed to the API enum. ``active=false`` flips the
// chip into a disabled "coming soon" state; users can read the
// description but the API will 422 the request.
const TIER_INFO: Record<MeshTier, { label: string; sub: string; active: boolean }> = {
  low: {
    label: "Low",
    sub: "fast · vertex-colored",
    active: true,
  },
  standard: {
    label: "Standard",
    sub: "textured · UV mapped",
    active: true,
  },
  higher: {
    label: "Higher",
    sub: "mesh-aware retrain",
    active: false,
  },
};

function paramsFromScene(scene: Scene): ParamsState {
  const p = scene.mesh_params ?? {};
  // Coerce unknown / non-active tiers back to "low" so older
  // mesh_params rows or forward-rolled values surface sensibly.
  const rawTier = p.tier;
  const tier: MeshTier = rawTier === "standard" || rawTier === "higher" || rawTier === "low"
    ? (rawTier as MeshTier)
    : DEFAULT_PARAMS.tier;
  const rawTexSize = p.mvs_texture_size;
  const mvs_texture_size: MvsTextureSize =
    rawTexSize === 1024 || rawTexSize === 2048 || rawTexSize === 4096 || rawTexSize === 8192
      ? rawTexSize
      : DEFAULT_PARAMS.mvs_texture_size;
  const numIn = (v: unknown, lo: number, hi: number, fb: number): number =>
    typeof v === "number" && v >= lo && v <= hi ? v : fb;
  return {
    tier: TIER_INFO[tier].active ? tier : DEFAULT_PARAMS.tier,
    n_views: typeof p.n_views === "number" && p.n_views >= 24
      ? p.n_views
      : DEFAULT_PARAMS.n_views,
    remove_outliers: p.remove_outliers ?? DEFAULT_PARAMS.remove_outliers,
    use_bounding_box: p.use_bounding_box ?? DEFAULT_PARAMS.use_bounding_box,
    use_edited_splat: p.use_edited_splat ?? DEFAULT_PARAMS.use_edited_splat,
    alpha_min: numIn(p.alpha_min, 0, 1, DEFAULT_PARAMS.alpha_min),
    bbox_percentile_low: numIn(p.bbox_percentile_low, 0, 100, DEFAULT_PARAMS.bbox_percentile_low),
    bbox_percentile_high: numIn(p.bbox_percentile_high, 0, 100, DEFAULT_PARAMS.bbox_percentile_high),
    floater_opacity_min: numIn(p.floater_opacity_min, 0, 1, DEFAULT_PARAMS.floater_opacity_min),
    floater_scale_max_pct: numIn(p.floater_scale_max_pct, 0, 100, DEFAULT_PARAMS.floater_scale_max_pct),
    mvs_dense_views: typeof p.mvs_dense_views === "number" && p.mvs_dense_views >= 2 && p.mvs_dense_views <= 7
      ? p.mvs_dense_views
      : DEFAULT_PARAMS.mvs_dense_views,
    mvs_texture_size,
    mvs_refine_iters: typeof p.mvs_refine_iters === "number" && p.mvs_refine_iters >= 0 && p.mvs_refine_iters <= 4
      ? p.mvs_refine_iters
      : DEFAULT_PARAMS.mvs_refine_iters,
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
  // Gate the discard button + download links on artefact URL
  // presence, not on `status === "completed"`. The server keeps
  // mesh_obj_path / mesh_glb_path populated through a failed or
  // canceled re-extract (only DELETE /mesh nulls them), so a
  // strict status check would hide a still-valid prior mesh.
  const hasMesh = !!scene.mesh_obj_url;
  const progressPct = Math.round((meshProgress?.progress ?? 0) * 100);

  const onExtract = async () => {
    setSubmitting(true);
    setError(null);
    try {
      // Merge the existing scene.mesh_params first so any keys the
      // UI doesn't expose (voxel_size / sdf_trunc_mult / depth_trunc,
      // set via the API directly or via a future advanced-tunables
      // panel) survive a UI re-extract. Without this, clicking
      // Extract would silently clobber whatever advanced tuning a
      // power user had on the scene back to the worker defaults —
      // the trigger endpoint stores the submitted params verbatim
      // as the scene's active mesh_params.
      //
      // Legacy keys from the prior Poisson tier (num_points / depth /
      // density_quantile / normal_method) are dropped: passing them
      // back through would be ignored by the TSDF subprocess but
      // pollutes the persisted row. Active-tier keys only.
      const rest = scene.mesh_params ?? {};
      const cleanRest: Record<string, unknown> = {};
      for (const [k, v] of Object.entries(rest)) {
        if (k === "num_points" || k === "depth" || k === "density_quantile" || k === "normal_method") {
          continue;
        }
        cleanRest[k] = v;
      }
      await api.triggerSceneMesh(scene.id, {
        ...cleanRest,
        ...params,
      });
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

  return (
    <section className="space-y-4 rounded-lg border border-rule bg-surface p-5">
      <header className="flex items-baseline justify-between gap-3">
        <div>
          <Eyebrow className="!text-[10px] !tracking-[0.08em]">mesh</Eyebrow>
          <div className="text-[18px] font-bold tracking-[-0.02em]">
            Extract mesh · {TIER_INFO[params.tier].label.toLowerCase()}
          </div>
        </div>
        {hasMesh && (
          <span className="font-mono text-[11px] text-inkSoft">
            switch viewer to <b>mesh</b> to preview
          </span>
        )}
      </header>

      {/* Tier selector. Three-way segmented control; standard +
          higher are non-clickable today (coming-soon pill). Click
          handler is gated on tier.active rather than disabled prop
          so the focus ring still works for keyboard users. */}
      <div className="space-y-2">
        <Eyebrow className="!text-[10px] !tracking-[0.08em]">fidelity tier</Eyebrow>
        <div role="radiogroup" className="grid grid-cols-1 gap-2 sm:grid-cols-3">
          {(Object.keys(TIER_INFO) as MeshTier[]).map((t) => {
            const info = TIER_INFO[t];
            const selected = params.tier === t;
            const clickable = info.active && !isRunning && !submitting;
            return (
              <button
                key={t}
                type="button"
                role="radio"
                aria-checked={selected}
                aria-disabled={!info.active}
                disabled={!info.active}
                onClick={
                  clickable
                    ? () => setParams((s) => ({ ...s, tier: t }))
                    : undefined
                }
                className={
                  "flex flex-col items-start gap-0.5 rounded-md border px-3 py-2 text-left transition-colors " +
                  (selected
                    ? "border-accent bg-accent/10 text-fg"
                    : info.active
                      ? "border-rule bg-bg text-fg hover:border-accent/50"
                      : "border-rule/60 bg-bg/60 text-inkSoft cursor-not-allowed")
                }
              >
                <span className="flex items-center gap-2 text-sm font-semibold">
                  {info.label}
                  {!info.active && (
                    <span className="rounded-full bg-accent/15 px-1.5 py-px font-mono text-[9px] uppercase tracking-wider text-accent">
                      coming soon
                    </span>
                  )}
                </span>
                <span className="font-mono text-[10px] tracking-wide text-inkSoft">
                  {info.sub}
                </span>
              </button>
            );
          })}
        </div>
      </div>

      {isRunning && (
        <div className="space-y-1">
          <p className="font-mono text-[11px] text-accent">
            extracting… {progressPct}%
            {meshProgress?.message ? ` · ${meshProgress.message}` : ""}
          </p>
          <div className="h-[6px] overflow-hidden rounded-full bg-rule">
            <div
              className="h-full bg-accent transition-all"
              style={{ width: `${progressPct}%` }}
            />
          </div>
        </div>
      )}

      {status === "failed" && scene.mesh_error && (
        <p className="rounded-sm border border-danger/30 bg-danger/5 p-2 font-mono text-[11px] text-danger">
          last extract failed: {scene.mesh_error}
        </p>
      )}

      {/* Tier-specific knob sets. The fieldset stays mounted so its
          ``disabled`` prop reliably gates inputs while a job is in
          flight; we just swap which controls render. */}
      <fieldset
        className="grid grid-cols-1 gap-3 sm:grid-cols-3"
        disabled={isRunning || submitting}
      >
        {params.tier === "low" ? (
          <>
            <label
              className="flex flex-col gap-1"
              title="How many camera viewpoints are rendered from the trained splat and fused into the TSDF volume. More views = more complete surface coverage + cleaner color, longer extraction time. 96 is dense enough for most subjects."
            >
              <Eyebrow className="!text-[10px] !tracking-[0.08em]">
                viewpoint density
              </Eyebrow>
              <input
                type="number"
                value={params.n_views}
                min={24}
                max={360}
                step={12}
                onChange={(e) => {
                  const n = parseInt(e.target.value, 10);
                  if (Number.isFinite(n) && n >= 24 && n <= 360) {
                    setParams((s) => ({ ...s, n_views: n }));
                  }
                }}
                className="rounded-sm border border-rule bg-bg px-3 py-2 font-mono text-sm text-fg focus:border-accent focus:outline-none disabled:opacity-60"
              />
            </label>
            <label
              className="flex cursor-pointer items-center gap-2 self-end pb-2 text-sm"
              title="Drop small disconnected triangle clusters (< 1% of the largest cluster's size) after marching cubes. Cleans floaters from silhouette-edge depth noise. DOES NOT close holes — partial-surface output is intentional. Turn off if a legitimate isolated island of geometry gets dropped."
            >
              <input
                type="checkbox"
                checked={params.remove_outliers}
                onChange={(e) =>
                  setParams((s) => ({ ...s, remove_outliers: e.target.checked }))
                }
                className="accent-accent"
              />
              remove floaters
            </label>
            <label
              className="flex cursor-pointer items-center gap-2 self-end pb-2 text-sm"
              title="Crop the extracted mesh to the splat's robust 1st/99th percentile bounding box. Helpful when stray gaussians dragged the mesh into empty space; leave off to keep the full extent."
            >
              <input
                type="checkbox"
                checked={params.use_bounding_box}
                onChange={(e) =>
                  setParams((s) => ({ ...s, use_bounding_box: e.target.checked }))
                }
                className="accent-accent"
              />
              crop to bounding box
            </label>
          </>
        ) : (
          // Standard-tier (OpenMVS) knob set. Same three-column
          // grid as low tier so the panel doesn't reflow on tier
          // switch; the controls are independent of each other.
          <>
            <label
              className="flex flex-col gap-1"
              title="Number of views fused at each densification step. Higher = cleaner dense cloud but more expensive. OpenMVS recommends 3–5 for typical photogrammetry workloads."
            >
              <Eyebrow className="!text-[10px] !tracking-[0.08em]">
                dense views (fuse)
              </Eyebrow>
              <input
                type="number"
                value={params.mvs_dense_views}
                min={2}
                max={7}
                step={1}
                onChange={(e) => {
                  const n = parseInt(e.target.value, 10);
                  if (Number.isFinite(n) && n >= 2 && n <= 7) {
                    setParams((s) => ({ ...s, mvs_dense_views: n }));
                  }
                }}
                className="rounded-sm border border-rule bg-bg px-3 py-2 font-mono text-sm text-fg focus:border-accent focus:outline-none disabled:opacity-60"
              />
            </label>
            <label
              className="flex flex-col gap-1"
              title="Texture atlas page size in pixels. Power of 2. 4096 hits a reasonable sharpness / download balance for phone-resolution captures; 8192 starts giving diminishing returns above 4K input."
            >
              <Eyebrow className="!text-[10px] !tracking-[0.08em]">
                texture page size
              </Eyebrow>
              <select
                value={params.mvs_texture_size}
                onChange={(e) => {
                  const v = parseInt(e.target.value, 10) as MvsTextureSize;
                  if (MVS_TEXTURE_SIZES.includes(v)) {
                    setParams((s) => ({ ...s, mvs_texture_size: v }));
                  }
                }}
                className="rounded-sm border border-rule bg-bg px-3 py-2 font-mono text-sm text-fg focus:border-accent focus:outline-none disabled:opacity-60"
              >
                {MVS_TEXTURE_SIZES.map((sz) => (
                  <option key={sz} value={sz}>
                    {sz}px
                  </option>
                ))}
              </select>
            </label>
            <label
              className="flex flex-col gap-1"
              title="RefineMesh iterations. 0 skips refine entirely (use for RAM-constrained hosts; mesh remains usable, just less detailed). Higher values polish surface detail at the cost of wall time and memory."
            >
              <Eyebrow className="!text-[10px] !tracking-[0.08em]">
                refine iters (0 skips)
              </Eyebrow>
              <input
                type="number"
                value={params.mvs_refine_iters}
                min={0}
                max={4}
                step={1}
                onChange={(e) => {
                  const n = parseInt(e.target.value, 10);
                  if (Number.isFinite(n) && n >= 0 && n <= 4) {
                    setParams((s) => ({ ...s, mvs_refine_iters: n }));
                  }
                }}
                className="rounded-sm border border-rule bg-bg px-3 py-2 font-mono text-sm text-fg focus:border-accent focus:outline-none disabled:opacity-60"
              />
            </label>
          </>
        )}
      </fieldset>

      {/* Advanced quality knobs — only meaningful for the low
          tier (TSDF). Collapsed by default to keep the panel
          uncluttered for the common case; power users open the
          ``<details>`` to tune away "bubble" / floating-geometry
          artifacts. Every knob is documented in
          worker/app/pipeline/mesh.py:DEFAULT_PARAMS. */}
      {params.tier === "low" && (
        <details className="rounded-md border border-rule bg-bg/70 px-3 py-2 text-sm">
          <summary className="cursor-pointer select-none font-mono text-[11px] uppercase tracking-wide text-inkSoft">
            advanced · mesh quality
          </summary>
          <div className="space-y-3 pt-3">
            <label
              className="flex cursor-pointer items-center gap-2 text-sm"
              title="When the splat editor has cleaned up floaters, use the edited PLY as the mesh source. Off forces the raw export — useful for debugging. Has no effect when no edit has been applied."
            >
              <input
                type="checkbox"
                checked={params.use_edited_splat}
                onChange={(e) =>
                  setParams((s) => ({ ...s, use_edited_splat: e.target.checked }))
                }
                disabled={isRunning || submitting}
                className="accent-accent"
              />
              prefer edited splat (when available)
            </label>
            <div className="grid grid-cols-1 gap-3 sm:grid-cols-2">
              <label
                className="flex flex-col gap-1"
                title="Pixels with accumulated alpha below this threshold are treated as 'no observation' and skipped during TSDF integration. Raise to clip more background; lower (or 0) to integrate every rendered pixel. 0.5 is the sweet spot for typical phone captures."
              >
                <Eyebrow className="!text-[10px] !tracking-[0.08em]">
                  alpha gate
                </Eyebrow>
                <input
                  type="number"
                  value={params.alpha_min}
                  min={0}
                  max={1}
                  step={0.05}
                  disabled={isRunning || submitting}
                  onChange={(e) => {
                    const n = parseFloat(e.target.value);
                    if (Number.isFinite(n) && n >= 0 && n <= 1) {
                      setParams((s) => ({ ...s, alpha_min: n }));
                    }
                  }}
                  className="rounded-sm border border-rule bg-bg px-3 py-2 font-mono text-sm text-fg focus:border-accent focus:outline-none disabled:opacity-60"
                />
              </label>
              <label
                className="flex flex-col gap-1"
                title="Robust bbox percentile range (low / high) used to fit the dome camera path, cap depth integration distance, and crop the post-mesh AABB. Tighter (closer to 25/75) clips more floaters at the cost of dropping legitimate edge geometry; looser (closer to 0/100) keeps everything including outliers."
              >
                <Eyebrow className="!text-[10px] !tracking-[0.08em]">
                  bbox percentile · low / high
                </Eyebrow>
                <div className="flex items-center gap-2">
                  <input
                    type="number"
                    value={params.bbox_percentile_low}
                    min={0}
                    max={100}
                    step={1}
                    disabled={isRunning || submitting}
                    onChange={(e) => {
                      const n = parseFloat(e.target.value);
                      if (Number.isFinite(n) && n >= 0 && n < params.bbox_percentile_high) {
                        setParams((s) => ({ ...s, bbox_percentile_low: n }));
                      }
                    }}
                    className="w-full rounded-sm border border-rule bg-bg px-3 py-2 font-mono text-sm text-fg focus:border-accent focus:outline-none disabled:opacity-60"
                  />
                  <span className="font-mono text-[11px] text-inkSoft">/</span>
                  <input
                    type="number"
                    value={params.bbox_percentile_high}
                    min={0}
                    max={100}
                    step={1}
                    disabled={isRunning || submitting}
                    onChange={(e) => {
                      const n = parseFloat(e.target.value);
                      if (Number.isFinite(n) && n > params.bbox_percentile_low && n <= 100) {
                        setParams((s) => ({ ...s, bbox_percentile_high: n }));
                      }
                    }}
                    className="w-full rounded-sm border border-rule bg-bg px-3 py-2 font-mono text-sm text-fg focus:border-accent focus:outline-none disabled:opacity-60"
                  />
                </div>
              </label>
              <label
                className="flex flex-col gap-1"
                title="Drop gaussians with post-sigmoid opacity below this value before the dome render. Raise to be more aggressive (0.10 cleans most training noise; 0.20 starts cutting real geometry). 0 disables the prune entirely."
              >
                <Eyebrow className="!text-[10px] !tracking-[0.08em]">
                  floater opacity_min
                </Eyebrow>
                <input
                  type="number"
                  value={params.floater_opacity_min}
                  min={0}
                  max={1}
                  step={0.01}
                  disabled={isRunning || submitting}
                  onChange={(e) => {
                    const n = parseFloat(e.target.value);
                    if (Number.isFinite(n) && n >= 0 && n <= 1) {
                      setParams((s) => ({ ...s, floater_opacity_min: n }));
                    }
                  }}
                  className="rounded-sm border border-rule bg-bg px-3 py-2 font-mono text-sm text-fg focus:border-accent focus:outline-none disabled:opacity-60"
                />
              </label>
              <label
                className="flex flex-col gap-1"
                title="Drop the top percentile of gaussians by max-axis scale (the wildly-stretched 'sheet' gaussians that produce smeared depth). 95 = drop top 5%. 100 disables the prune entirely."
              >
                <Eyebrow className="!text-[10px] !tracking-[0.08em]">
                  floater scale_max_pct
                </Eyebrow>
                <input
                  type="number"
                  value={params.floater_scale_max_pct}
                  min={0}
                  max={100}
                  step={1}
                  disabled={isRunning || submitting}
                  onChange={(e) => {
                    const n = parseFloat(e.target.value);
                    if (Number.isFinite(n) && n >= 0 && n <= 100) {
                      setParams((s) => ({ ...s, floater_scale_max_pct: n }));
                    }
                  }}
                  className="rounded-sm border border-rule bg-bg px-3 py-2 font-mono text-sm text-fg focus:border-accent focus:outline-none disabled:opacity-60"
                />
              </label>
            </div>
          </div>
        </details>
      )}

      <div className="flex flex-wrap items-center gap-3">
        <BigButton
          onClick={onExtract}
          disabled={isRunning || submitting}
        >
          {submitting
            ? "Queueing…"
            : hasMesh
              ? "Re-extract"
              : "Extract mesh"}
        </BigButton>
        {hasMesh && (
          <BigButton
            variant="secondary"
            onClick={onDiscard}
            disabled={isRunning || discarding}
            className="!text-danger"
          >
            {discarding ? "Discarding…" : "Discard mesh"}
          </BigButton>
        )}
        {error && (
          <span className="font-mono text-[11px] text-danger">{error}</span>
        )}
      </div>

      {/* Standard-tier downloads. The OBJ + MTL + per-page JPGs
          have to travel together for a Blender / Cinema-4D import
          to find the textures; surface them as individual links
          so the user can grab the whole set. Low-tier (vertex-
          colored OBJ alone) doesn't render this block — its
          single download is the implicit "switch viewer to mesh"
          path. */}
      {hasMesh && scene.mesh_tex_urls && scene.mesh_tex_urls.length > 0 && (
        <div className="space-y-2 border-t border-rule pt-3">
          <Eyebrow className="!text-[10px] !tracking-[0.08em]">
            textured bundle downloads
          </Eyebrow>
          <div className="flex flex-wrap gap-2 text-[11px]">
            {scene.mesh_obj_url && (
              <a
                href={scene.mesh_obj_url}
                download="scene.obj"
                className="rounded-sm border border-rule px-2 py-1 font-mono text-fg hover:border-accent"
              >
                scene.obj
              </a>
            )}
            <a
              href={scene.mesh_obj_url?.replace(/scene\.obj$/, "scene.mtl")}
              download="scene.mtl"
              className="rounded-sm border border-rule px-2 py-1 font-mono text-fg hover:border-accent"
            >
              scene.mtl
            </a>
            {scene.mesh_tex_urls.map((u, i) => {
              const name = u.split("/").pop() ?? `scene_tex${i}.jpg`;
              return (
                <a
                  key={u}
                  href={u}
                  download={name}
                  className="rounded-sm border border-rule px-2 py-1 font-mono text-fg hover:border-accent"
                >
                  {name}
                </a>
              );
            })}
            {scene.mesh_glb_url && (
              <a
                href={scene.mesh_glb_url}
                download="scene.glb"
                className="rounded-sm border border-rule px-2 py-1 font-mono text-fg hover:border-accent"
              >
                scene.glb
              </a>
            )}
          </div>
        </div>
      )}
    </section>
  );
}

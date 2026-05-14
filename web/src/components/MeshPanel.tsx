"use client";
// On-demand mesh extraction panel. Renders below the SplatViewer +
// SplatEditor on a completed scene. Lets the user kick off the
// worker's mesh pipeline with a few tunables, watches progress
// over the scene WS (mirrored on scene.mesh_progress to match the
// filter pattern). The mesh is VIEWED in the main SplatViewer via
// the "mesh" view-mode — this panel only owns the controls.
//
// Tiered: the user picks a fidelity tier (low / standard / higher);
// only the **low** tier is implemented today (TSDF fusion of
// gsplat-rendered RGB+depth views from the trained splatfacto
// checkpoint, output = vertex-colored OBJ + GLB). Standard
// (OpenMVS textured) and higher (2DGS / SuGaR retrain) are
// scaffolded but show a "coming soon" treatment until their
// backends land. Picking either disabled tier is impossible at
// the UI level and rejected at the API level too.
//
// Independent of the edit pipeline: you can mesh + filter in
// either order, and discarding the edit doesn't touch the mesh.
import { useState } from "react";
import { api } from "@/lib/api";
import type { MeshStatus, Scene } from "@/lib/types";
import { BigButton, Eyebrow } from "@/components/pebble";

type MeshTier = "low" | "standard" | "higher";

interface ParamsState {
  tier: MeshTier;
  n_views: number;
  remove_outliers: boolean;
  use_bounding_box: boolean;
}

const DEFAULT_PARAMS: ParamsState = {
  tier: "low",
  n_views: 96,
  remove_outliers: true,
  use_bounding_box: false,
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
    active: false,
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
  return {
    tier: TIER_INFO[tier].active ? tier : DEFAULT_PARAMS.tier,
    n_views: typeof p.n_views === "number" && p.n_views >= 24
      ? p.n_views
      : DEFAULT_PARAMS.n_views,
    remove_outliers: p.remove_outliers ?? DEFAULT_PARAMS.remove_outliers,
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

      <fieldset
        className="grid grid-cols-1 gap-3 sm:grid-cols-3"
        disabled={isRunning || submitting}
      >
        <label
          className="flex flex-col gap-1"
          title="How many camera viewpoints are rendered from the trained splat and fused into the TSDF volume. More views = more complete surface coverage + cleaner color, longer extraction time. 96 is dense enough for most subjects."
        >
          <Eyebrow className="!text-[10px] !tracking-[0.08em]">
            viewpoint density · low only
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
      </fieldset>

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
    </section>
  );
}

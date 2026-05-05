"use client";
// On-demand Poisson mesh extraction panel. Renders below the
// SplatViewer + SplatEditor on a completed scene. Lets the user
// kick off the worker's Open3D-Poisson pipeline with a few
// tunables, watches progress over the scene WS (mirrored on
// scene.mesh_progress to match the filter pattern). The mesh is
// VIEWED in the main SplatViewer via the "mesh" view-mode — this
// panel only owns the controls.
//
// The mesh is reconstructed from the trained Gaussian-splatting
// scene's exported .ply (gaussian centres → Open3D Poisson),
// independent of the edit pipeline: you can mesh + filter in
// either order, and discarding the edit doesn't touch the mesh.
import { useState } from "react";
import { api } from "@/lib/api";
import type { MeshStatus, Scene } from "@/lib/types";
import { BigButton, Eyebrow } from "@/components/pebble";

interface ParamsState {
  num_points: number;
  remove_outliers: boolean;
  use_bounding_box: boolean;
}

const DEFAULT_PARAMS: ParamsState = {
  num_points: 1_000_000,
  remove_outliers: true,
  use_bounding_box: false,
};

function paramsFromScene(scene: Scene): ParamsState {
  const p = scene.mesh_params ?? {};
  return {
    num_points: p.num_points ?? DEFAULT_PARAMS.num_points,
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
      // UI doesn't expose (depth / density_quantile, set via the
      // API directly or via a future advanced-tunables panel)
      // survive a UI re-extract. Without this, clicking Extract
      // would silently clobber whatever advanced tuning a power
      // user had on the scene back to the worker defaults — the
      // trigger endpoint stores the submitted params verbatim as
      // the scene's active mesh_params.
      // normal_method is forced to "open3d" because the worker
      // allowlist no longer accepts anything else; the param
      // shape stays here so future expansions stay backward-
      // compatible without a UI rev.
      await api.triggerSceneMesh(scene.id, {
        ...(scene.mesh_params ?? {}),
        ...params,
        normal_method: "open3d",
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
            Extract mesh (Poisson)
          </div>
        </div>
        {hasMesh && (
          <span className="font-mono text-[11px] text-inkSoft">
            switch viewer to <b>mesh</b> to preview
          </span>
        )}
      </header>

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
          title="Number of points sampled from the trained Gaussians for Poisson surface reconstruction. More points = denser, more detailed mesh, but longer extraction + larger output."
        >
          <Eyebrow className="!text-[10px] !tracking-[0.08em]">
            point sample count
          </Eyebrow>
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
            className="rounded-sm border border-rule bg-bg px-3 py-2 font-mono text-sm text-fg focus:border-accent focus:outline-none disabled:opacity-60"
          />
        </label>
        <label
          className="flex cursor-pointer items-center gap-2 self-end pb-2 text-sm"
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
          className="flex cursor-pointer items-center gap-2 self-end pb-2 text-sm"
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

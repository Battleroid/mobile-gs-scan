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
      // normal_method is fixed to "open3d" server-side; the param
      // shape stays here so future expansions stay backward-
      // compatible without a UI rev.
      await api.triggerSceneMesh(scene.id, {
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
    <section className="border border-rule p-4 space-y-4">
      <header>
        <h2 className="text-sm text-muted">extract mesh (poisson)</h2>
        {hasMesh && (
          <p className="text-xs text-muted mt-1">
            view in the splat viewer above — switch the “view” cycle to
            “mesh”.
          </p>
        )}
      </header>

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

"use client";
// Filter / cleanup editor for an exported splat.
//
// Renders below the SplatViewer once a scene has produced a .ply.
// User toggles ops on/off, tweaks parameters, and clicks Apply &
// save to PUT the recipe to /api/scenes/{id}/edit. The worker runs
// the filter pipeline; progress streams back via the scene
// websocket (mirrored on `scene.edit_progress`). On completion the
// page swaps the viewer source over to the edited artifact.
//
// Live in-browser preview (per-gaussian opacity toggle via Spark's
// SplatMesh.setSplat) is a Phase-1.5 follow-up; the recipe DSL +
// apply/download flow is the load-bearing piece and works without
// it.
import { forwardRef, useEffect, useImperativeHandle, useState } from "react";
import { api } from "@/lib/api";
import type { EditOp, EditRecipe, Scene } from "@/lib/types";
import type { SelectionWidget } from "@/components/SplatViewer";

export interface SplatEditorHandle {
  /** Apply a widget commit (bbox or sphere drag) to the form state.
   *  Recipe is NOT auto-saved; user still hits Apply. */
  commitWidget: (next: SelectionWidget) => void;
  /** Snapshot the current bbox / sphere form values for seeding a
   *  fresh widget. Lets the page activate the gizmo with whatever
   *  the user has been typing into the number fields, not just the
   *  last-saved recipe. */
  snapshotWidget: (kind: "bbox" | "sphere") => SelectionWidget;
}

interface OpsState {
  opacity: { enabled: boolean; min: number };
  scale: { enabled: boolean; max_scale: number };
  bbox: {
    enabled: boolean;
    min: [number, number, number];
    max: [number, number, number];
  };
  sphere: {
    enabled: boolean;
    center: [number, number, number];
    radius: number;
  };
  sor: { enabled: boolean; k: number; std_multiplier: number };
  dbscan: { enabled: boolean; eps: number; min_samples: number };
  /**
   * Ops the form doesn't render UI for (today: keep_indices from a
   * future lasso flow / hand-edited recipes; tomorrow: anything we
   * add server-side ahead of UI). Captured verbatim so the round-
   * trip through the form preserves them — otherwise applying the
   * recipe via the UI would silently drop them.
   */
  extras: EditOp[];
}

const DEFAULT_OPS: OpsState = {
  opacity: { enabled: false, min: 0.05 },
  scale: { enabled: false, max_scale: 0.5 },
  bbox: { enabled: false, min: [-2, -1, -2], max: [2, 2, 2] },
  sphere: { enabled: false, center: [0, 0, 0], radius: 0.3 },
  sor: { enabled: false, k: 24, std_multiplier: 2.0 },
  dbscan: { enabled: false, eps: 0.05, min_samples: 30 },
  extras: [],
};

interface Props {
  scene: Scene;
  /** Live-streamed progress while the filter job is running. */
  editProgress: { progress: number; message: string | null } | null;
  /** Result summary from the most recently completed filter. */
  lastEditResult: { kept: number; total: number; at: number } | null;
  /** Which artifact the parent viewer is currently showing. */
  viewing: "original" | "edited";
  onChangeView: (next: "original" | "edited") => void;
  /**
   * In-viewer selection widget controls. The editor decides which
   * op (bbox / sphere) the gizmo is currently bound to and writes
   * the round-tripped selection back into local form state on every
   * commit. Page wires the widget into <SplatViewer>.
   */
  activeWidget: "bbox" | "sphere" | null;
  onActivateWidget: (next: "bbox" | "sphere" | null) => void;
  /**
   * Fires whenever the bbox / sphere form values change while a
   * widget is active. Lets the page re-seed the gizmo so number-
   * field edits, Reset, and recipe re-seeds all keep the 3D
   * volume in lock-step with what apply & save will submit.
   */
  onWidgetFormChange?: (next: SelectionWidget) => void;
}

export const SplatEditor = forwardRef<SplatEditorHandle, Props>(function SplatEditor(
  {
    scene,
    editProgress,
    lastEditResult,
    viewing,
    onChangeView,
    activeWidget,
    onActivateWidget,
    onWidgetFormChange,
  }: Props,
  ref,
) {
  const [ops, setOps] = useState<OpsState>(() => recipeToOps(scene.edit_recipe));

  // While a widget is active, mirror form-side edits (number fields,
  // Reset, recipe re-seeds from prop) back to the gizmo so the
  // displayed volume always matches what apply will submit. Without
  // this, widgetSelection only moved on viewer commits and could
  // diverge from the form.
  useEffect(() => {
    if (!activeWidget || !onWidgetFormChange) return;
    if (activeWidget === "bbox") {
      onWidgetFormChange({
        kind: "bbox",
        min: ops.bbox.min,
        max: ops.bbox.max,
      });
    } else {
      onWidgetFormChange({
        kind: "sphere",
        center: ops.sphere.center,
        radius: ops.sphere.radius,
      });
    }
  }, [
    activeWidget,
    onWidgetFormChange,
    ops.bbox.min,
    ops.bbox.max,
    ops.sphere.center,
    ops.sphere.radius,
  ]);

  useImperativeHandle(
    ref,
    () => ({
      commitWidget: (next: SelectionWidget) => {
        setOps((s) => {
          if (next.kind === "bbox") {
            return {
              ...s,
              bbox: { enabled: true, min: next.min, max: next.max },
            };
          }
          return {
            ...s,
            sphere: {
              enabled: true,
              center: next.center,
              radius: Math.max(0, next.radius),
            },
          };
        });
      },
      snapshotWidget: (kind) => {
        if (kind === "bbox") {
          return { kind: "bbox", min: ops.bbox.min, max: ops.bbox.max };
        }
        return {
          kind: "sphere",
          center: ops.sphere.center,
          radius: ops.sphere.radius,
        };
      },
    }),
    [ops.bbox.min, ops.bbox.max, ops.sphere.center, ops.sphere.radius],
  );
  const [submitting, setSubmitting] = useState(false);
  const [discarding, setDiscarding] = useState(false);
  const [error, setError] = useState<string | null>(null);

  // Re-seed the local form when the server-side recipe changes
  // (apply round-trip succeeded, or someone DELETEd the edit). Use
  // the "adjust state on prop change during render" pattern so we
  // don't trip react-hooks/set-state-in-effect — same approach
  // JobLogPanel uses for its follow-mode flag.
  // https://react.dev/learn/you-might-not-need-an-effect#adjusting-some-state-when-a-prop-changes
  const recipeKey = JSON.stringify(scene.edit_recipe ?? null);
  const [prevRecipeKey, setPrevRecipeKey] = useState(recipeKey);
  if (prevRecipeKey !== recipeKey) {
    setPrevRecipeKey(recipeKey);
    setOps(recipeToOps(scene.edit_recipe));
  }

  const isRunning =
    scene.edit_status === "queued" || scene.edit_status === "running";

  const onApply = async () => {
    setSubmitting(true);
    setError(null);
    try {
      await api.upsertSceneEdit(scene.id, opsToRecipe(ops));
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setSubmitting(false);
    }
  };

  const onReset = () => {
    setOps(recipeToOps(scene.edit_recipe));
    setError(null);
  };

  const onDiscard = async () => {
    if (
      !window.confirm(
        "Discard this edit? The edited artifacts will be deleted; the original splat is untouched.",
      )
    ) {
      return;
    }
    setDiscarding(true);
    setError(null);
    try {
      await api.clearSceneEdit(scene.id);
      setOps(DEFAULT_OPS);
      onChangeView("original");
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setDiscarding(false);
    }
  };

  const hasEdit =
    scene.edit_status === "completed" && scene.edited_ply_url !== null;
  const progressPct = Math.round(((editProgress?.progress ?? 0) * 100));

  return (
    <section className="border border-rule p-4 space-y-4">
      <header className="flex items-baseline justify-between gap-4">
        <h2 className="text-sm text-muted">edit / clean up the splat</h2>
        {(hasEdit || isRunning) && (
          <div className="flex items-center gap-2 text-xs">
            <span className="text-muted">view:</span>
            <button
              type="button"
              onClick={() => onChangeView("original")}
              className={
                viewing === "original"
                  ? "underline text-fg"
                  : "text-muted hover:text-fg"
              }
            >
              original
            </button>
            <button
              type="button"
              onClick={() => onChangeView("edited")}
              disabled={!hasEdit}
              className={
                viewing === "edited"
                  ? "underline text-fg"
                  : "text-muted hover:text-fg disabled:opacity-40 disabled:cursor-not-allowed"
              }
            >
              edited
            </button>
          </div>
        )}
      </header>

      {isRunning && (
        <div className="space-y-1 text-xs">
          <p className="text-warn">
            filtering… {progressPct}%
            {editProgress?.message ? ` · ${editProgress.message}` : ""}
          </p>
          <div className="h-1 bg-rule">
            <div
              className="h-full bg-accent transition-all"
              style={{ width: `${progressPct}%` }}
            />
          </div>
        </div>
      )}

      {scene.edit_status === "failed" && scene.edit_error && (
        <p className="text-xs text-danger">
          last apply failed: {scene.edit_error}
        </p>
      )}

      {lastEditResult && !isRunning && scene.edit_status === "completed" && (
        <p className="text-xs text-muted">
          last apply: {lastEditResult.total.toLocaleString()} →{" "}
          {lastEditResult.kept.toLocaleString()} gaussians (
          {Math.round(
            (1 - lastEditResult.kept / Math.max(1, lastEditResult.total)) * 100,
          )}
          % dropped)
        </p>
      )}

      <fieldset className="space-y-3" disabled={isRunning || submitting}>
        <legend className="text-xs text-muted">filters</legend>

        <Toggle
          label="opacity threshold"
          help="Drop gaussians below this rendered opacity (0–1). Cheap fix for faint, fog-like floaters."
          checked={ops.opacity.enabled}
          onChange={(v) =>
            setOps((s) => ({ ...s, opacity: { ...s.opacity, enabled: v } }))
          }
        >
          <NumberField
            label="min"
            value={ops.opacity.min}
            min={0}
            max={1}
            step={0.01}
            onChange={(v) =>
              setOps((s) => ({ ...s, opacity: { ...s.opacity, min: v } }))
            }
          />
        </Toggle>

        <Toggle
          label="scale clamp"
          help="Drop gaussians whose largest axis is bigger than this (metres). Kills oversized 'fuzzy' splats stretched across the scene."
          checked={ops.scale.enabled}
          onChange={(v) =>
            setOps((s) => ({ ...s, scale: { ...s.scale, enabled: v } }))
          }
        >
          <NumberField
            label="max scale (m)"
            value={ops.scale.max_scale}
            min={0.001}
            step={0.05}
            onChange={(v) =>
              setOps((s) => ({
                ...s,
                scale: { ...s.scale, max_scale: v },
              }))
            }
          />
        </Toggle>

        <Toggle
          label="bbox crop (m)"
          help="Keep only gaussians inside this axis-aligned box. Drag the orange box widget in the viewer to set min/max visually."
          checked={ops.bbox.enabled}
          onChange={(v) => {
            setOps((s) => ({ ...s, bbox: { ...s.bbox, enabled: v } }));
            if (!v && activeWidget === "bbox") onActivateWidget(null);
          }}
        >
          <Vec3Field
            label="min"
            value={ops.bbox.min}
            onChange={(v) =>
              setOps((s) => ({ ...s, bbox: { ...s.bbox, min: v } }))
            }
          />
          <Vec3Field
            label="max"
            value={ops.bbox.max}
            onChange={(v) =>
              setOps((s) => ({ ...s, bbox: { ...s.bbox, max: v } }))
            }
          />
          <button
            type="button"
            className={
              activeWidget === "bbox"
                ? "text-accent text-xs underline"
                : "text-muted text-xs underline hover:text-fg"
            }
            onClick={() =>
              onActivateWidget(activeWidget === "bbox" ? null : "bbox")
            }
          >
            {activeWidget === "bbox" ? "stop editing in 3D" : "edit in 3D"}
          </button>
        </Toggle>

        <Toggle
          label="sphere remove"
          help="Drop everything inside a sphere. Use 'nuke origin cloud' for the classic spherical noise around (0,0,0); drag the magenta sphere widget for arbitrary cleanup."
          checked={ops.sphere.enabled}
          onChange={(v) => {
            setOps((s) => ({ ...s, sphere: { ...s.sphere, enabled: v } }));
            if (!v && activeWidget === "sphere") onActivateWidget(null);
          }}
        >
          <Vec3Field
            label="center"
            value={ops.sphere.center}
            onChange={(v) =>
              setOps((s) => ({ ...s, sphere: { ...s.sphere, center: v } }))
            }
          />
          <NumberField
            label="radius"
            value={ops.sphere.radius}
            min={0}
            step={0.05}
            onChange={(v) =>
              setOps((s) => ({
                ...s,
                sphere: { ...s.sphere, radius: v },
              }))
            }
          />
          <button
            type="button"
            className="text-xs text-muted underline hover:text-fg"
            onClick={() =>
              setOps((s) => ({
                ...s,
                sphere: {
                  enabled: true,
                  center: [0, 0, 0],
                  radius: 0.3,
                },
              }))
            }
          >
            nuke origin cloud
          </button>
          <button
            type="button"
            className={
              activeWidget === "sphere"
                ? "text-accent text-xs underline"
                : "text-muted text-xs underline hover:text-fg"
            }
            onClick={() =>
              onActivateWidget(activeWidget === "sphere" ? null : "sphere")
            }
          >
            {activeWidget === "sphere" ? "stop editing in 3D" : "edit in 3D"}
          </button>
        </Toggle>

        <Toggle
          label="outlier removal (SOR)"
          help="Statistical outlier removal: each point's mean distance to its k nearest neighbours; drop those further than (mean + σ·std). Higher k = smoother judgement, lower σ = more aggressive."
          checked={ops.sor.enabled}
          onChange={(v) =>
            setOps((s) => ({ ...s, sor: { ...s.sor, enabled: v } }))
          }
        >
          <NumberField
            label="k"
            value={ops.sor.k}
            min={1}
            step={1}
            onChange={(v) =>
              setOps((s) => ({ ...s, sor: { ...s.sor, k: Math.round(v) } }))
            }
          />
          <NumberField
            label="σ multiplier"
            value={ops.sor.std_multiplier}
            min={0}
            step={0.1}
            onChange={(v) =>
              setOps((s) => ({
                ...s,
                sor: { ...s.sor, std_multiplier: v },
              }))
            }
          />
        </Toggle>

        <Toggle
          label="DBSCAN keep-largest"
          help="Cluster gaussians by spatial density (eps = radius, min_samples = density floor) and keep only the largest cluster. Great for isolating the subject from detached floaters."
          checked={ops.dbscan.enabled}
          onChange={(v) =>
            setOps((s) => ({ ...s, dbscan: { ...s.dbscan, enabled: v } }))
          }
        >
          <NumberField
            label="eps"
            value={ops.dbscan.eps}
            min={0}
            step={0.01}
            onChange={(v) =>
              setOps((s) => ({ ...s, dbscan: { ...s.dbscan, eps: v } }))
            }
          />
          <NumberField
            label="min samples"
            value={ops.dbscan.min_samples}
            min={1}
            step={1}
            onChange={(v) =>
              setOps((s) => ({
                ...s,
                dbscan: { ...s.dbscan, min_samples: Math.round(v) },
              }))
            }
          />
        </Toggle>
      </fieldset>

      <div className="flex flex-wrap items-center gap-3 text-xs">
        <button
          type="button"
          onClick={onApply}
          disabled={isRunning || submitting}
          className="border border-rule px-3 py-1 hover:bg-rule disabled:opacity-50"
        >
          {submitting ? "queueing…" : "apply & save"}
        </button>
        <button
          type="button"
          onClick={onReset}
          disabled={isRunning || submitting}
          className="text-muted underline hover:text-fg disabled:opacity-40"
        >
          reset
        </button>
        {hasEdit && (
          <button
            type="button"
            onClick={onDiscard}
            disabled={isRunning || discarding}
            className="text-danger underline hover:text-fg disabled:opacity-40"
          >
            {discarding ? "discarding…" : "discard edit"}
          </button>
        )}
        {error && <span className="text-danger">{error}</span>}
      </div>
    </section>
  );
});

function Toggle({
  label,
  help,
  checked,
  onChange,
  children,
}: {
  label: string;
  /** One-liner shown inline beneath the toggle when expanded. */
  help?: string;
  checked: boolean;
  onChange: (v: boolean) => void;
  children?: React.ReactNode;
}) {
  return (
    <div className="space-y-1">
      <label
        className="flex items-center gap-2 text-sm cursor-pointer"
        title={help}
      >
        <input
          type="checkbox"
          checked={checked}
          onChange={(e) => onChange(e.target.checked)}
          className="accent-accent"
        />
        {label}
      </label>
      {checked && (
        <div className="ml-6 space-y-1">
          {help && <p className="text-[11px] text-muted leading-snug">{help}</p>}
          <div className="flex flex-wrap items-end gap-3">{children}</div>
        </div>
      )}
    </div>
  );
}

function NumberField({
  label,
  value,
  onChange,
  min,
  max,
  step,
}: {
  label: string;
  value: number;
  onChange: (v: number) => void;
  min?: number;
  max?: number;
  step?: number;
}) {
  return (
    <label className="flex flex-col gap-0.5 text-xs">
      <span className="text-muted">{label}</span>
      <input
        type="number"
        value={value}
        onChange={(e) => {
          const n = parseFloat(e.target.value);
          if (Number.isFinite(n)) onChange(n);
        }}
        min={min}
        max={max}
        step={step ?? 0.01}
        className="w-24 bg-transparent border-b border-rule px-1 focus:outline-none focus:border-accent"
      />
    </label>
  );
}

function Vec3Field({
  label,
  value,
  onChange,
}: {
  label: string;
  value: [number, number, number];
  onChange: (v: [number, number, number]) => void;
}) {
  const set = (i: number) => (n: number) => {
    const next = [...value] as [number, number, number];
    next[i] = n;
    onChange(next);
  };
  return (
    <div className="flex flex-col gap-0.5 text-xs">
      <span className="text-muted">{label}</span>
      <div className="flex gap-1">
        <NumberField label="x" value={value[0]} onChange={set(0)} />
        <NumberField label="y" value={value[1]} onChange={set(1)} />
        <NumberField label="z" value={value[2]} onChange={set(2)} />
      </div>
    </div>
  );
}

/** Derive the gizmo's current value from a recipe (defaults if the
 *  recipe doesn't carry the relevant op yet). The page passes this
 *  to SplatViewer when the user clicks "edit in 3D" on bbox / sphere. */
export function widgetSelectionFromRecipe(
  recipe: EditRecipe | null,
  kind: "bbox" | "sphere",
): SelectionWidget {
  const ops = recipeToOps(recipe);
  if (kind === "bbox") {
    return { kind: "bbox", min: ops.bbox.min, max: ops.bbox.max };
  }
  return {
    kind: "sphere",
    center: ops.sphere.center,
    radius: ops.sphere.radius,
  };
}

/** Patch a recipe with the new bbox/sphere widget value, leaving
 *  every other op untouched. Used by the page when the gizmo
 *  commits — we round-trip through the live recipe so the
 *  SplatEditor's own form re-seeds from authoritative state. */
export function applyWidgetSelectionToRecipe(
  recipe: EditRecipe | null,
  next: SelectionWidget,
): EditRecipe {
  const ops = recipeToOps(recipe);
  if (next.kind === "bbox") {
    ops.bbox = { enabled: true, min: next.min, max: next.max };
  } else {
    ops.sphere = {
      enabled: true,
      center: next.center,
      radius: Math.max(0, next.radius),
    };
  }
  return opsToRecipe(ops);
}

function recipeToOps(recipe: EditRecipe | null): OpsState {
  const out: OpsState = JSON.parse(JSON.stringify(DEFAULT_OPS));
  if (!recipe?.ops) return out;
  for (const op of recipe.ops) {
    switch (op.type) {
      case "opacity_threshold":
        out.opacity = { enabled: true, min: op.min };
        break;
      case "scale_clamp":
        out.scale = { enabled: true, max_scale: op.max_scale };
        break;
      case "bbox_crop":
        out.bbox = { enabled: true, min: op.min, max: op.max };
        break;
      case "sphere_remove":
        out.sphere = {
          enabled: true,
          center: op.center,
          radius: op.radius,
        };
        break;
      case "sor":
        out.sor = {
          enabled: true,
          k: op.k,
          std_multiplier: op.std_multiplier,
        };
        break;
      case "dbscan_keep_largest":
        out.dbscan = {
          enabled: true,
          eps: op.eps,
          min_samples: op.min_samples,
        };
        break;
      default:
        // Unknown to the form (today: keep_indices). Hold onto the
        // raw op so opsToRecipe can re-emit it; otherwise the user's
        // hand-edited or lasso-authored recipe gets silently
        // shredded the first time they click Apply.
        out.extras.push(op);
    }
  }
  return out;
}

function opsToRecipe(ops: OpsState): EditRecipe {
  // Order matters: cheap point-wise filters first, expensive
  // neighbourhood filters last so they only see the trimmed set.
  // Unknown extras (e.g. keep_indices from a future lasso flow)
  // ride along at the end — every supported op is an AND-mask, so
  // tail position keeps the result identical to whatever the user
  // intended.
  const out: EditOp[] = [];
  if (ops.opacity.enabled) {
    out.push({ type: "opacity_threshold", min: ops.opacity.min });
  }
  if (ops.scale.enabled) {
    out.push({ type: "scale_clamp", max_scale: ops.scale.max_scale });
  }
  if (ops.bbox.enabled) {
    out.push({ type: "bbox_crop", min: ops.bbox.min, max: ops.bbox.max });
  }
  if (ops.sphere.enabled) {
    out.push({
      type: "sphere_remove",
      center: ops.sphere.center,
      radius: ops.sphere.radius,
    });
  }
  if (ops.sor.enabled) {
    out.push({
      type: "sor",
      k: ops.sor.k,
      std_multiplier: ops.sor.std_multiplier,
    });
  }
  if (ops.dbscan.enabled) {
    out.push({
      type: "dbscan_keep_largest",
      eps: ops.dbscan.eps,
      min_samples: ops.dbscan.min_samples,
    });
  }
  for (const extra of ops.extras) {
    out.push(extra);
  }
  return { ops: out };
}

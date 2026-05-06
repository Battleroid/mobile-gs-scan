// Mirrors the pydantic DTOs in worker/app/api/captures.py + scenes.py.
// Kept narrow on purpose — only what the UI actually consumes.

export type CaptureStatus =
  | "created"
  | "uploading"
  | "queued"
  | "processing"
  | "completed"
  | "failed"
  | "canceled";

// Single value today; kept as a union (not a literal alias) so a
// future capture source (Android-app direct, drone-set, …) lands
// without re-typing every consumer.
export type CaptureSource = "upload";

export interface Capture {
  id: string;
  name: string;
  status: CaptureStatus;
  source: CaptureSource;
  frame_count: number;
  dropped_count: number;
  has_pose: boolean;
  meta: Record<string, unknown>;
  error: string | null;
  scene_id: string | null;
  created_at: string;
  updated_at: string;
}

export type JobKind =
  | "extract"
  | "sfm"
  | "train"
  | "export"
  | "mesh"
  | "filter"
  // PR-D: server-rendered PNG thumbnail. Soft-failure step that
  // runs after export. Kept in sync with worker/app/jobs/schema.py
  // — exhaustive switches over JobKind would otherwise treat valid
  // thumbnail rows from /api/scenes as impossible and fall through.
  | "thumbnail";

export type EditStatus =
  | "none"
  | "queued"
  | "running"
  | "completed"
  | "failed";

export type MeshStatus =
  | "none"
  | "queued"
  | "running"
  | "completed"
  | "failed";

export interface MeshParams {
  num_points?: number;
  remove_outliers?: boolean;
  normal_method?: "open3d";
  use_bounding_box?: boolean;
  // Octree depth for the screened-Poisson solver. 5–12; the worker
  // rejects anything outside that range. UI doesn't surface a
  // control yet — the server defaults to 9 — but persisted values
  // need to round-trip through the trigger payload spread in
  // MeshPanel without an `as any` cast.
  depth?: number;
  // Quantile threshold for density-based vertex pruning post-
  // Poisson. 0 disables. Same UI/persistence note as `depth`.
  density_quantile?: number;
}

export type EditOp =
  | { type: "opacity_threshold"; min: number }
  | { type: "scale_clamp"; max_scale: number }
  | { type: "bbox_crop"; min: [number, number, number]; max: [number, number, number] }
  // Sphere ops are siblings: crop keeps everything INSIDE the
  // sphere (paired with the in-viewer widget), remove drops what's
  // inside (used by the nuke-origin preset for noise cleanup).
  | { type: "sphere_crop"; center: [number, number, number]; radius: number }
  | { type: "sphere_remove"; center: [number, number, number]; radius: number }
  | { type: "sor"; k: number; std_multiplier: number }
  | { type: "dbscan_keep_largest"; eps: number; min_samples: number }
  // Source-PLY index set authored by the lasso flow (Phase 2.5) or
  // hand-edited recipes. UI doesn't render controls for it but the
  // editor preserves it through the apply round-trip.
  | { type: "keep_indices"; indices: number[] };

export interface EditRecipe {
  ops: EditOp[];
}

export type JobStatus =
  | "queued"
  | "claimed"
  | "running"
  | "completed"
  | "failed"
  | "canceled";

export interface Job {
  id: string;
  kind: JobKind;
  status: JobStatus;
  progress: number;
  progress_msg: string | null;
  error: string | null;
}

export interface Scene {
  id: string;
  capture_id: string;
  status: CaptureStatus;
  error: string | null;
  ply_url: string | null;
  spz_url: string | null;
  edited_ply_url: string | null;
  edited_spz_url: string | null;
  edit_status: EditStatus;
  edit_error: string | null;
  edit_recipe: EditRecipe | null;
  mesh_obj_url: string | null;
  mesh_glb_url: string | null;
  mesh_status: MeshStatus;
  mesh_error: string | null;
  mesh_params: MeshParams | null;
  // PNG thumbnail rendered post-export by the worker's
  // JobKind.thumbnail step. Null when the render hasn't run, the
  // scene is a stub, or ns-render failed. CaptureCard falls back
  // to a chip-tinted gradient placeholder when null.
  thumb_url: string | null;
  jobs: Job[];
  created_at: string;
  completed_at: string | null;
}

export interface ServerEvent {
  topic: string;
  kind: string;
  ts: number;
  data: Record<string, unknown>;
}

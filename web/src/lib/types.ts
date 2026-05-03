// Mirrors the pydantic DTOs in worker/app/api/captures.py + scenes.py.
// Kept narrow on purpose — only what the UI actually consumes.

export type CaptureStatus =
  | "created"
  | "pairing"
  | "streaming"
  | "uploading"
  | "queued"
  | "processing"
  | "completed"
  | "failed"
  | "canceled";

export type CaptureSource = "mobile_native" | "mobile_web" | "upload";

export interface Capture {
  id: string;
  name: string;
  status: CaptureStatus;
  source: CaptureSource;
  pair_token: string | null;
  pair_url: string | null;
  frame_count: number;
  dropped_count: number;
  has_pose: boolean;
  meta: Record<string, unknown>;
  error: string | null;
  scene_id: string | null;
  created_at: string;
  updated_at: string;
}

export type JobKind = "sfm" | "train" | "export" | "mesh" | "filter";

export type EditStatus =
  | "none"
  | "queued"
  | "running"
  | "completed"
  | "failed";

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

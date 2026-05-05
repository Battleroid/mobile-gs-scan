// Thin client over the FastAPI surface.
//
// The base URL is resolved in this order:
//   1. NEXT_PUBLIC_API_BASE if non-empty (set at build time).
//   2. Browser, http://<host>:3000 — the Next dev port that the web
//      container also publishes alongside Caddy. Hitting it directly
//      bypasses the reverse proxy, so /api/* on the same origin lands
//      on Next (which has no such routes) instead of FastAPI. Rewrite
//      to the api container's published port directly so this access
//      path works for desktop dev. The api ships CORSMiddleware so
//      cross-origin from :3000 → :8000 is allowed.
//   3. Otherwise window.location.origin — what we want under
//      https/Caddy where /api/* is reverse-proxied same-origin.
//   4. Server-side rendering: localhost:8000 fallback so the build
//      doesn't crash.
import type {
  Capture,
  CaptureSource,
  EditRecipe,
  MeshParams,
  Scene,
} from "./types";

function apiBase(): string {
  const baked = process.env.NEXT_PUBLIC_API_BASE;
  if (baked && baked !== "") return baked.replace(/\/$/, "");
  if (typeof window !== "undefined") {
    if (
      window.location.protocol === "http:" &&
      window.location.port === "3000"
    ) {
      return `http://${window.location.hostname}:8000`;
    }
    return window.location.origin;
  }
  return "http://localhost:8000";
}

async function jsonReq<T>(path: string, init?: RequestInit): Promise<T> {
  const res = await fetch(apiBase() + path, {
    ...init,
    headers: {
      "Content-Type": "application/json",
      ...(init?.headers ?? {}),
    },
  });
  if (!res.ok) {
    const body = await res.text();
    throw new Error(`${res.status} ${res.statusText}: ${body}`);
  }
  return res.json();
}

export interface JobLogResponse {
  log: string;
  size: number;
  path: string | null;
  available: boolean;
}

export const api = {
  base: apiBase,
  listCaptures: () => jsonReq<Capture[]>("/api/captures"),
  getCapture: (id: string) => jsonReq<Capture>(`/api/captures/${id}`),
  createCapture: (body: {
    name?: string;
    source: CaptureSource;
    has_pose?: boolean;
    meta?: Record<string, unknown>;
  }) =>
    jsonReq<Capture>("/api/captures", {
      method: "POST",
      body: JSON.stringify(body),
    }),
  // Rename a capture. Server trims + validates non-empty; throws on
  // 4xx with the server's error body so the caller can surface it.
  renameCapture: (id: string, name: string) =>
    jsonReq<Capture>(`/api/captures/${id}`, {
      method: "PATCH",
      body: JSON.stringify({ name }),
    }),
  finalize: (id: string) =>
    jsonReq<{ scene_id: string }>(`/api/captures/${id}/finalize`, {
      method: "POST",
      body: JSON.stringify({ reason: "user" }),
    }),
  uploadFiles: async (id: string, files: File[]) => {
    const fd = new FormData();
    for (const f of files) fd.append("files", f, f.name);
    const res = await fetch(`${apiBase()}/api/captures/${id}/upload`, {
      method: "POST",
      body: fd,
    });
    if (!res.ok) throw new Error(await res.text());
    return res.json() as Promise<
      | { accepted: number; total: number }
      | { accepted_video: string; total: number }
    >;
  },
  deleteCapture: (id: string) =>
    fetch(`${apiBase()}/api/captures/${id}`, { method: "DELETE" }),
  getScene: (id: string) => jsonReq<Scene>(`/api/scenes/${id}`),
  artifactUrl: (sceneId: string, kind: "ply" | "spz") =>
    `${apiBase()}/api/scenes/${sceneId}/artifacts/${kind}`,
  // Tail of the subprocess log for a given job. Polled by the
  // collapsible per-step log panel on the capture-detail page.
  // tailBytes caps server-side at 1 MB (default 8 KB), so this is
  // safe to refetchInterval at 2 s while the job is running.
  getJobLog: (id: string, tailBytes = 8192) =>
    jsonReq<JobLogResponse>(`/api/jobs/${id}/log?tail_bytes=${tailBytes}`),
  // Cancel an in-flight job. Idempotent: returns canceled=false if
  // the job was already in a terminal state. The worker that owns
  // the job notices the cancel on its next heartbeat (~5 s) and
  // SIGKILLs any registered subprocess.
  cancelJob: (id: string) =>
    jsonReq<{ ok: boolean; canceled: boolean; status: string }>(
      `/api/jobs/${id}/cancel`,
      { method: "POST" },
    ),
  // Replace this scene's edit recipe and (re)enqueue the filter job.
  // Idempotent — calling again with a different recipe cancels any
  // in-flight filter job and starts fresh.
  upsertSceneEdit: (sceneId: string, recipe: EditRecipe) =>
    jsonReq<Scene>(`/api/scenes/${sceneId}/edit`, {
      method: "PUT",
      body: JSON.stringify({ recipe }),
    }),
  clearSceneEdit: (sceneId: string) =>
    jsonReq<Scene>(`/api/scenes/${sceneId}/edit`, { method: "DELETE" }),
  // Kick off Poisson mesh extraction. Idempotent: re-posting cancels
  // any in-flight mesh job and starts a new run with the new params.
  triggerSceneMesh: (sceneId: string, params?: MeshParams) =>
    jsonReq<Scene>(`/api/scenes/${sceneId}/mesh`, {
      method: "POST",
      body: JSON.stringify({ params: params ?? null }),
    }),
  clearSceneMesh: (sceneId: string) =>
    jsonReq<Scene>(`/api/scenes/${sceneId}/mesh`, { method: "DELETE" }),
};

export function wsUrl(path: string): string {
  const base = apiBase();
  const u = new URL(base);
  u.protocol = u.protocol === "https:" ? "wss:" : "ws:";
  u.pathname = path;
  return u.toString();
}

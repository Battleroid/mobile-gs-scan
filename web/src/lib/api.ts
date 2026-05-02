// Thin client over the FastAPI surface.
//
// The base URL is resolved in this order:
//   1. NEXT_PUBLIC_API_BASE if non-empty (set at build time).
//   2. Otherwise window.location.origin — which is what we want
//      under https/Caddy where the api lives at /api/* on the same
//      origin as the web ui.
//   3. Server-side rendering on Next: localhost:8000 fallback so
//      the build doesn't crash.
import type { Capture, CaptureSource, FilterRecipe, Scene } from "./types";

function apiBase(): string {
  const baked = process.env.NEXT_PUBLIC_API_BASE;
  if (baked && baked !== "") return baked.replace(/\/$/, "");
  if (typeof window !== "undefined") return window.location.origin;
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

export const api = {
  base: apiBase,
  listCaptures: () => jsonReq<Capture[]>("/api/captures"),
  getCapture: (id: string) => jsonReq<Capture>(`/api/captures/${id}`),
  resolvePairToken: (token: string) =>
    jsonReq<Capture>(`/api/captures/by-token/${token}`),
  createCapture: (body: {
    name: string;
    source: CaptureSource;
    has_pose?: boolean;
    meta?: Record<string, unknown>;
  }) =>
    jsonReq<Capture>("/api/captures", {
      method: "POST",
      body: JSON.stringify(body),
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
    return res.json() as Promise<{ accepted: number; total: number }>;
  },
  deleteCapture: (id: string) =>
    fetch(`${apiBase()}/api/captures/${id}`, { method: "DELETE" }),
  getScene: (id: string) => jsonReq<Scene>(`/api/scenes/${id}`),
  artifactUrl: (
    sceneId: string,
    kind: "ply" | "spz",
    opts?: { edit?: boolean },
  ) =>
    `${apiBase()}/api/scenes/${sceneId}/artifacts/${kind}` +
    (opts?.edit ? "?edit=true" : ""),
  upsertSceneEdit: (sceneId: string, recipe: FilterRecipe) =>
    jsonReq<Scene>(`/api/scenes/${sceneId}/edit`, {
      method: "PUT",
      body: JSON.stringify({ recipe }),
    }),
  clearSceneEdit: (sceneId: string) =>
    jsonReq<Scene>(`/api/scenes/${sceneId}/edit`, { method: "DELETE" }),
};

export function wsUrl(path: string): string {
  const base = apiBase();
  const u = new URL(base);
  u.protocol = u.protocol === "https:" ? "wss:" : "ws:";
  u.pathname = path;
  return u.toString();
}

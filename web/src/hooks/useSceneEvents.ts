"use client";
import { useEffect, useRef, useState } from "react";
import { api, wsUrl } from "@/lib/api";
import type { Scene, ServerEvent } from "@/lib/types";

// Capped exponential backoff for WS reconnects (1s → 2s → 4s → 8s
// → 15s). Mirrored in useCaptureEvents — the symptom we're fixing is
// "had to refresh to see progress", which is a silent WS disconnect
// with no client-side reconnect; both hooks need the same recovery.
const RECONNECT_DELAYS_MS = [1_000, 2_000, 4_000, 8_000, 15_000];

// HTTP existence probe — see useCaptureEvents for the full rationale.
// Returns false on 404 (resource deleted, stop retrying), true on a
// 2xx (resource exists, keep retrying), null on 5xx / network error
// (couldn't tell, keep retrying).
async function probeSceneExists(sceneId: string): Promise<boolean | null> {
  try {
    const res = await fetch(`${api.base()}/api/scenes/${sceneId}`, {
      method: "GET",
    });
    if (res.status === 404) return false;
    if (res.ok) return true;
    return null;
  } catch {
    return null;
  }
}

export interface EditResult {
  kept: number;
  total: number;
  /** Wall-clock when the edited event arrived. */
  at: number;
}

export function useSceneEvents(sceneId: string | null): {
  scene: Scene | null;
  lastEvent: ServerEvent | null;
  /**
   * Latest filter-job progress snapshot (if any). Mirrored on the
   * scene WS via `scene.edit_progress` events from the worker —
   * the per-job WS topic for the filter job exists too, but the
   * scene WS doesn't subscribe to jobs that arrived AFTER the WS
   * connected (the filter job is enqueued mid-session). Mirroring
   * on the scene topic keeps a single subscription sufficient.
   */
  editProgress: { progress: number; message: string | null } | null;
  /** Result of the most recently completed filter (kept/total). */
  lastEditResult: EditResult | null;
  /** Same shape as editProgress, for the mesh job. */
  meshProgress: { progress: number; message: string | null } | null;
} {
  const [scene, setScene] = useState<Scene | null>(null);
  const [lastEvent, setLastEvent] = useState<ServerEvent | null>(null);
  const [editProgress, setEditProgress] = useState<{
    progress: number;
    message: string | null;
  } | null>(null);
  const [lastEditResult, setLastEditResult] = useState<EditResult | null>(null);
  const [meshProgress, setMeshProgress] = useState<{
    progress: number;
    message: string | null;
  } | null>(null);

  // Monotonic counter for refreshScene roundtrips. The hook fires an
  // out-of-band re-fetch on both scene.edit_queued and scene.edited
  // so the freshly-enqueued / completed filter job lands in the
  // snapshot's jobs list. Because both promises race independently
  // of each other, a slow queued-time response can resolve AFTER a
  // fast edited-time one and stomp the completed snapshot back to
  // queued. Each refresh captures gen at fire-time and only commits
  // its result if no later refresh has fired since.
  const refreshGen = useRef(0);

  useEffect(() => {
    if (!sceneId) return;
    let cancelled = false;
    // Permanent stop signal set when the HTTP existence probe returns
    // 404 — see useCaptureEvents for the full rationale.
    let permanentlyStopped = false;
    let ws: WebSocket | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let attempt = 0;
    // Per-disconnect-streak probe flag — see useCaptureEvents for
    // the full rationale. Resets on each successful onopen so a
    // scene deleted after a prior session still gets probed when
    // the next reconnect streak starts.
    let streakProbed = false;

    const scheduleReconnect = () => {
      if (cancelled || permanentlyStopped) return;
      const delay =
        RECONNECT_DELAYS_MS[
          Math.min(attempt, RECONNECT_DELAYS_MS.length - 1)
        ];
      attempt += 1;
      reconnectTimer = setTimeout(connect, delay);
    };

    const connect = () => {
      if (cancelled || permanentlyStopped) return;
      const url = wsUrl(`/api/scenes/${sceneId}/events`);
      ws = new WebSocket(url);
      ws.onopen = () => {
        attempt = 0;
        streakProbed = false;
        // No REST snapshot refetch on reconnect. The server sends a
        // ``snapshot`` event on every ``ws.accept()``; ``onmessage``
        // is attached synchronously before any frame can arrive, so
        // the snapshot WS event is guaranteed to reach the client.
        // A concurrent REST fetch would race the in-flight WS
        // ``job.progress`` / ``scene.*`` events on the socket and
        // could roll back recently-applied incremental updates.
      };
      ws.onmessage = (e) => {
      try {
        const evt = JSON.parse(e.data) as ServerEvent;
        setLastEvent(evt);
        if (evt.kind === "snapshot") {
          setScene(evt.data as unknown as Scene);
        } else if (evt.topic.startsWith("job.")) {
          setScene((s) => {
            if (!s) return s;
            const jobId = evt.topic.slice("job.".length);
            return {
              ...s,
              jobs: s.jobs.map((j) =>
                j.id === jobId
                  ? {
                      ...j,
                      progress:
                        (evt.data.progress as number | undefined) ?? j.progress,
                      progress_msg:
                        (evt.data.message as string | undefined) ??
                        j.progress_msg,
                      status: kindToStatus(evt.kind, j.status),
                    }
                  : j,
              ),
            };
          });
        } else if (evt.topic.startsWith("scene.")) {
          if (evt.kind === "scene.completed") {
            setScene((s) => (s ? { ...s, status: "completed" } : s));
          } else if (evt.kind === "scene.failed") {
            setScene((s) => (s ? { ...s, status: "failed" } : s));
          } else if (evt.kind === "scene.edit_queued") {
            setScene((s) => (s ? { ...s, edit_status: "queued" } : s));
            setEditProgress({ progress: 0, message: "queued" });
            // The new filter job didn't exist when this WS opened,
            // so it's missing from the snapshot's jobs list (and
            // there's no per-job subscription — scene.edit_progress
            // is mirrored on the scene topic so we still get
            // progress, but the JobRow + JobLogPanel for the
            // filter need the row to render). Re-fetching the scene
            // pulls the row in so the UI catches up.
            const gen = ++refreshGen.current;
            void refreshScene(sceneId).then((next) => {
              if (next && gen === refreshGen.current) setScene(next);
            });
          } else if (evt.kind === "scene.edit_running") {
            setScene((s) => (s ? { ...s, edit_status: "running" } : s));
          } else if (evt.kind === "scene.edit_progress") {
            setEditProgress({
              progress: (evt.data.progress as number) ?? 0,
              message: (evt.data.message as string | null) ?? null,
            });
          } else if (evt.kind === "scene.edit_failed") {
            setScene((s) =>
              s
                ? {
                    ...s,
                    edit_status: "failed",
                    edit_error: (evt.data.error as string | null) ?? null,
                  }
                : s,
            );
            setEditProgress(null);
            // Same WS-subscription-gap reason as scene.mesh_failed:
            // job.failed never reaches the client because the
            // filter job's topic was created after the WS opened.
            // Refresh so the pipeline list / JobLogPanel reflect
            // the failure without a manual reload.
            const gen = ++refreshGen.current;
            void refreshScene(sceneId).then((next) => {
              if (next && gen === refreshGen.current) setScene(next);
            });
          } else if (evt.kind === "scene.edited") {
            // Rather than splicing in just the new urls here, re-fetch
            // the full Scene so the edited_ply_url / edited_spz_url
            // fields land authoritatively. Use a lightweight one-shot
            // import to avoid a circular module dep with api.ts.
            setEditProgress({ progress: 1, message: "done" });
            setScene((s) => (s ? { ...s, edit_status: "completed" } : s));
            const kept = evt.data.kept as number | undefined;
            const total = evt.data.total as number | undefined;
            if (typeof kept === "number" && typeof total === "number") {
              setLastEditResult({ kept, total, at: Date.now() });
            }
            const gen = ++refreshGen.current;
            void refreshScene(sceneId).then((next) => {
              if (next && gen === refreshGen.current) setScene(next);
            });
          } else if (evt.kind === "scene.edit_cleared") {
            // Same trade-off as scene.mesh_cleared: cancel paths
            // emit this event without deleting the prior recipe /
            // edited artefacts (only DELETE /edit does that).
            // Re-fetch instead of clobbering URLs locally so a
            // canceled re-apply doesn't drop a still-valid edit
            // from the UI.
            setEditProgress(null);
            setLastEditResult(null);
            const gen = ++refreshGen.current;
            void refreshScene(sceneId).then((next) => {
              if (next && gen === refreshGen.current) setScene(next);
            });
          } else if (evt.kind === "scene.mesh_queued") {
            setScene((s) => (s ? { ...s, mesh_status: "queued" } : s));
            setMeshProgress({ progress: 0, message: "queued" });
            // Same rationale as scene.edit_queued: mesh job arrived
            // after the WS opened so the snapshot's jobs list +
            // per-job WS subscription don't include it. Re-fetch
            // pulls the row in.
            const gen = ++refreshGen.current;
            void refreshScene(sceneId).then((next) => {
              if (next && gen === refreshGen.current) setScene(next);
            });
          } else if (evt.kind === "scene.mesh_running") {
            setScene((s) => (s ? { ...s, mesh_status: "running" } : s));
          } else if (evt.kind === "scene.mesh_progress") {
            setMeshProgress({
              progress: (evt.data.progress as number) ?? 0,
              message: (evt.data.message as string | null) ?? null,
            });
          } else if (evt.kind === "scene.mesh_failed") {
            setScene((s) =>
              s
                ? {
                    ...s,
                    mesh_status: "failed",
                    mesh_error: (evt.data.error as string | null) ?? null,
                  }
                : s,
            );
            setMeshProgress(null);
            // Mesh jobs queued after the scene WS opened don't have
            // a per-job topic subscription, so a job.failed event
            // never reaches us; the row in scene.jobs stays at
            // "queued" until a manual reload. Re-fetch the snapshot
            // so the pipeline list (and its JobLogPanel) reflect
            // the failure. Same pattern as scene.meshed.
            const gen = ++refreshGen.current;
            void refreshScene(sceneId).then((next) => {
              if (next && gen === refreshGen.current) setScene(next);
            });
          } else if (evt.kind === "scene.meshed") {
            setMeshProgress({ progress: 1, message: "done" });
            setScene((s) => (s ? { ...s, mesh_status: "completed" } : s));
            const gen = ++refreshGen.current;
            void refreshScene(sceneId).then((next) => {
              if (next && gen === refreshGen.current) setScene(next);
            });
          } else if (evt.kind === "scene.mesh_cleared") {
            // Don't naively null mesh_*_url in local state: cancel
            // paths emit scene.mesh_cleared *without* deleting the
            // prior mesh artefacts from disk (only DELETE /mesh
            // does that). Re-fetch the canonical snapshot — if the
            // server nulled the paths (discard) the fetch returns
            // them as null; if the artefacts are still on disk
            // (cancel of a re-extract over an existing mesh) the
            // fetch returns them, and a still-valid mesh stays
            // visible.
            setMeshProgress(null);
            const gen = ++refreshGen.current;
            void refreshScene(sceneId).then((next) => {
              if (next && gen === refreshGen.current) setScene(next);
            });
          }
        }
      } catch {
        // ignore
      }
      };
      ws.onclose = () => {
        if (cancelled || permanentlyStopped) return;
        // Schedule reconnect FIRST so a slow probe doesn't delay
        // recovery. Probe runs concurrently and, if it returns 404,
        // sets ``permanentlyStopped`` + clears the pending timer.
        // See useCaptureEvents for the full rationale.
        scheduleReconnect();
        if (!streakProbed) {
          streakProbed = true;
          void probeSceneExists(sceneId).then((exists) => {
            if (cancelled || permanentlyStopped) return;
            if (exists !== false) return; // 2xx / 5xx / err → keep retrying
            permanentlyStopped = true;
            if (reconnectTimer) {
              clearTimeout(reconnectTimer);
              reconnectTimer = null;
            }
          });
        }
      };
    };

    connect();

    return () => {
      cancelled = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      if (ws) ws.close();
    };
  }, [sceneId]);

  return { scene, lastEvent, editProgress, lastEditResult, meshProgress };
}

function kindToStatus(kind: string, fallback: Scene["jobs"][number]["status"]) {
  if (kind === "job.running") return "running" as const;
  if (kind === "job.completed") return "completed" as const;
  if (kind === "job.failed") return "failed" as const;
  if (kind === "job.progress") return "running" as const;
  return fallback;
}

async function refreshScene(sceneId: string): Promise<Scene | null> {
  // Inline fetch instead of importing the api object to dodge a
  // circular hook → api → hook chain in some tooling configs. The
  // request shape is intentionally identical to api.getScene().
  const { api } = await import("@/lib/api");
  try {
    return await api.getScene(sceneId);
  } catch {
    return null;
  }
}

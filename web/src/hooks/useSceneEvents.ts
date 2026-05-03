"use client";
import { useEffect, useRef, useState } from "react";
import { wsUrl } from "@/lib/api";
import type { EditStatus, Scene, ServerEvent } from "@/lib/types";

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
} {
  const [scene, setScene] = useState<Scene | null>(null);
  const [lastEvent, setLastEvent] = useState<ServerEvent | null>(null);
  const [editProgress, setEditProgress] = useState<{
    progress: number;
    message: string | null;
  } | null>(null);
  const [lastEditResult, setLastEditResult] = useState<EditResult | null>(null);

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
    const url = wsUrl(`/api/scenes/${sceneId}/events`);
    const ws = new WebSocket(url);
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
            setScene((s) =>
              s
                ? {
                    ...s,
                    edit_status: "none" as EditStatus,
                    edit_recipe: null,
                    edit_error: null,
                    edited_ply_url: null,
                    edited_spz_url: null,
                  }
                : s,
            );
            setEditProgress(null);
            setLastEditResult(null);
          }
        }
      } catch {
        // ignore
      }
    };
    return () => ws.close();
  }, [sceneId]);

  return { scene, lastEvent, editProgress, lastEditResult };
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

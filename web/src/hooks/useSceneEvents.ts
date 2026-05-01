"use client";
import { useEffect, useState } from "react";
import { wsUrl } from "@/lib/api";
import type { Scene, ServerEvent } from "@/lib/types";

export function useSceneEvents(sceneId: string | null): {
  scene: Scene | null;
  lastEvent: ServerEvent | null;
} {
  const [scene, setScene] = useState<Scene | null>(null);
  const [lastEvent, setLastEvent] = useState<ServerEvent | null>(null);

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
          }
        }
      } catch {
        // ignore
      }
    };
    return () => ws.close();
  }, [sceneId]);

  return { scene, lastEvent };
}

function kindToStatus(kind: string, fallback: Scene["jobs"][number]["status"]) {
  if (kind === "job.running") return "running" as const;
  if (kind === "job.completed") return "completed" as const;
  if (kind === "job.failed") return "failed" as const;
  if (kind === "job.progress") return "running" as const;
  return fallback;
}

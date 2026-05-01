"use client";
import { useEffect, useState } from "react";
import { wsUrl } from "@/lib/api";
import type { Capture, ServerEvent } from "@/lib/types";

export function useCaptureEvents(captureId: string | null): {
  capture: Capture | null;
  lastEvent: ServerEvent | null;
} {
  const [capture, setCapture] = useState<Capture | null>(null);
  const [lastEvent, setLastEvent] = useState<ServerEvent | null>(null);

  useEffect(() => {
    if (!captureId) return;
    const url = wsUrl(`/api/captures/${captureId}/events`);
    const ws = new WebSocket(url);
    ws.onmessage = (e) => {
      try {
        const evt = JSON.parse(e.data) as ServerEvent;
        setLastEvent(evt);
        if (evt.kind === "snapshot") {
          setCapture(evt.data as unknown as Capture);
        } else if (evt.kind.startsWith("stream.frames")) {
          setCapture((c) =>
            c
              ? {
                  ...c,
                  frame_count: (evt.data.accepted as number) ?? c.frame_count,
                  dropped_count: (evt.data.dropped as number) ?? c.dropped_count,
                }
              : c,
          );
        }
      } catch {
        // ignore malformed payloads
      }
    };
    return () => ws.close();
  }, [captureId]);

  return { capture, lastEvent };
}

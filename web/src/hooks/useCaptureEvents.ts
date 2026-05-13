"use client";
import { useEffect, useState } from "react";
import { api, wsUrl } from "@/lib/api";
import type { Capture, ServerEvent } from "@/lib/types";

// Capped exponential backoff for WS reconnects. 1s → 2s → 4s → 8s → 15s.
// The first retry is intentionally short — most disconnects we care
// about (proxy idle timeouts, brief network blips) clear within
// seconds, and the page sits visibly frozen until we reconnect.
const RECONNECT_DELAYS_MS = [1_000, 2_000, 4_000, 8_000, 15_000];

// HTTP existence probe used to distinguish "resource was deleted"
// from "transient outage" when the WS keeps failing to open. The
// server rejects missing-resource subscriptions via ``ws.close(4404)``
// BEFORE ``ws.accept()`` (worker/app/api/captures.py +
// scenes.py), which the browser surfaces as a 1006 abnormal closure
// — indistinguishable from a transient network drop. Returns:
//   true  → resource is reachable, keep retrying the WS
//   false → server explicitly said 404, the resource is gone
//   null  → couldn't tell (5xx, CORS, network error) — keep retrying
async function probeCaptureExists(captureId: string): Promise<boolean | null> {
  try {
    const res = await fetch(`${api.base()}/api/captures/${captureId}`, {
      method: "GET",
    });
    if (res.status === 404) return false;
    if (res.ok) return true;
    return null;
  } catch {
    return null;
  }
}

export function useCaptureEvents(captureId: string | null): {
  capture: Capture | null;
  lastEvent: ServerEvent | null;
} {
  const [capture, setCapture] = useState<Capture | null>(null);
  const [lastEvent, setLastEvent] = useState<ServerEvent | null>(null);

  useEffect(() => {
    if (!captureId) return;
    let cancelled = false;
    let ws: WebSocket | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let attempt = 0;
    let everConnected = false;
    let probedAfterFirstFail = false;

    const scheduleReconnect = () => {
      if (cancelled) return;
      const delay =
        RECONNECT_DELAYS_MS[
          Math.min(attempt, RECONNECT_DELAYS_MS.length - 1)
        ];
      attempt += 1;
      reconnectTimer = setTimeout(connect, delay);
    };

    const connect = () => {
      if (cancelled) return;
      const url = wsUrl(`/api/captures/${captureId}/events`);
      ws = new WebSocket(url);
      ws.onopen = () => {
        attempt = 0;
        everConnected = true;
        // No REST snapshot refetch here. The server's WS handler
        // sends a ``snapshot`` event on every ``ws.accept()``, and
        // ``onmessage`` is attached synchronously before any frame
        // can arrive — so the snapshot WS event always reaches us.
        // A parallel REST fetch would race ``stream.frames.*``
        // events arriving concurrently on the socket and could roll
        // back frame_count after newer events bumped it.
      };
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
                    dropped_count:
                      (evt.data.dropped as number) ?? c.dropped_count,
                  }
                : c,
            );
          }
        } catch {
          // ignore malformed payloads
        }
      };
      ws.onclose = () => {
        if (cancelled) return;
        // First failure on a hook that's never seen onopen: do an
        // HTTP existence probe to discriminate "resource genuinely
        // deleted" (server returns 404 → stop, no point retrying)
        // from "transient outage" (anything else — keep retrying
        // with backoff so the page recovers when the API comes back).
        // The probe runs ONCE — for subsequent failures we just
        // keep retrying indefinitely on the assumption that whatever
        // the probe saw is still true. A capture deleted mid-session
        // after we already connected once flows through the normal
        // capture.deleted event on the WS.
        if (!everConnected && !probedAfterFirstFail) {
          probedAfterFirstFail = true;
          void probeCaptureExists(captureId).then((exists) => {
            if (cancelled) return;
            if (exists === false) return; // 404 → permanent stop
            scheduleReconnect();
          });
          return;
        }
        scheduleReconnect();
      };
      // onerror just precedes onclose for our purposes — let onclose
      // own the reconnect schedule so we don't double-fire.
    };

    connect();

    return () => {
      cancelled = true;
      if (reconnectTimer) clearTimeout(reconnectTimer);
      if (ws) ws.close();
    };
  }, [captureId]);

  return { capture, lastEvent };
}

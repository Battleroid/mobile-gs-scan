"use client";
import { useEffect, useRef, useState } from "react";
import { wsUrl } from "@/lib/api";
import type { Capture, ServerEvent } from "@/lib/types";

// Capped exponential backoff for WS reconnects. 1s → 2s → 4s → 8s → 15s.
// The first retry is intentionally short — most disconnects we care
// about (proxy idle timeouts, brief network blips) clear within
// seconds, and the page sits visibly frozen until we reconnect.
const RECONNECT_DELAYS_MS = [1_000, 2_000, 4_000, 8_000, 15_000];

// Close-codes the server uses to signal a TERMINAL rejection — the
// subscription is structurally invalid and retrying is pointless.
// The capture/scene WS endpoints close with 4404 when the resource
// doesn't exist (deleted out from under the tab, or a bad URL). The
// 1008/1011 codes are the WS-protocol-level "policy violation" /
// "internal error" markers we leave alone the same way — both are
// fail-permanent, not "wait and retry". Anything else (1006 abnormal,
// 1001 going away, etc.) keeps the backoff loop alive.
const TERMINAL_CLOSE_CODES: ReadonlySet<number> = new Set([4404, 1008, 1011]);

export function useCaptureEvents(captureId: string | null): {
  capture: Capture | null;
  lastEvent: ServerEvent | null;
} {
  const [capture, setCapture] = useState<Capture | null>(null);
  const [lastEvent, setLastEvent] = useState<ServerEvent | null>(null);
  // Monotonic counter so the snapshot-refetch fired from each
  // (re)connect's onopen only commits when it's still the latest
  // one in flight. Mirrors useSceneEvents — without it two close-
  // together reconnects can resolve out of order and let the
  // earlier snapshot stomp newer state.
  const refreshGen = useRef(0);

  useEffect(() => {
    if (!captureId) return;
    let cancelled = false;
    let ws: WebSocket | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let attempt = 0;

    const connect = () => {
      if (cancelled) return;
      const url = wsUrl(`/api/captures/${captureId}/events`);
      ws = new WebSocket(url);
      ws.onopen = () => {
        attempt = 0;
        // Re-fetch the canonical snapshot defensively — the server
        // sends one on connect, but if it raced our onmessage
        // attachment (vanishingly rare but possible) the page would
        // stay on pre-disconnect state. Idempotent on the happy path.
        const gen = ++refreshGen.current;
        void (async () => {
          try {
            const { api } = await import("@/lib/api");
            const fresh = await api.getCapture(captureId);
            if (!cancelled && gen === refreshGen.current) setCapture(fresh);
          } catch {
            // ignore — the snapshot event will populate us if it arrives.
          }
        })();
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
      ws.onclose = (event) => {
        if (cancelled) return;
        // Terminal codes mean the server explicitly told us not to
        // come back — don't reschedule. Anything else is treated as
        // a transient drop and the backoff loop runs.
        if (TERMINAL_CLOSE_CODES.has(event.code)) return;
        const delay =
          RECONNECT_DELAYS_MS[
            Math.min(attempt, RECONNECT_DELAYS_MS.length - 1)
          ];
        attempt += 1;
        reconnectTimer = setTimeout(connect, delay);
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

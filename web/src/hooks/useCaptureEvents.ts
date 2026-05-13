"use client";
import { useEffect, useState } from "react";
import { wsUrl } from "@/lib/api";
import type { Capture, ServerEvent } from "@/lib/types";

// Capped exponential backoff for WS reconnects. 1s → 2s → 4s → 8s → 15s.
// The first retry is intentionally short — most disconnects we care
// about (proxy idle timeouts, brief network blips) clear within
// seconds, and the page sits visibly frozen until we reconnect.
const RECONNECT_DELAYS_MS = [1_000, 2_000, 4_000, 8_000, 15_000];

// How many times we'll retry before declaring the subscription
// permanently failed (resource deleted, URL wrong, etc.). Only the
// "never connected" path is gated: once we've successfully opened
// at least once and the connection later drops, we treat it as a
// transient blip and keep retrying forever with backoff.
//
// We can't gate retries on CloseEvent.code for the missing-resource
// case — the server rejects the WS handshake via ``ws.close(4404)``
// BEFORE ``ws.accept()`` (see worker/app/api/captures.py +
// scenes.py), which the browser surfaces as a 1006 abnormal
// closure (the same code transient network blips produce). The
// attempt cap is the only durable signal here.
const MAX_INITIAL_ATTEMPTS = 5;

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
        // If we've never opened, cap the attempts so a deleted or
        // nonexistent capture doesn't loop forever. Once we've been
        // connected at least once, treat every close as a transient
        // drop and keep retrying — that's the actual reconnect goal.
        if (!everConnected && attempt + 1 >= MAX_INITIAL_ATTEMPTS) return;
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

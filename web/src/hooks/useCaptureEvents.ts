"use client";
import { useEffect, useState } from "react";
import { api, wsUrl } from "@/lib/api";
import type { Capture, ServerEvent } from "@/lib/types";

// Capped exponential backoff for WS reconnects. 1s → 2s → 4s → 8s → 15s.
// The first retry is intentionally short — most disconnects we care
// about (proxy idle timeouts, brief network blips) clear within
// seconds, and the page sits visibly frozen until we reconnect.
const RECONNECT_DELAYS_MS = [1_000, 2_000, 4_000, 8_000, 15_000];

// Floor between consecutive existence probes. The probe runs in
// parallel with reconnect attempts and should re-fire periodically
// during long outages so a capture deleted MID-outage (after the
// first probe returned ``true`` or ``null``) eventually surfaces as
// a 404 and stops the retry loop. 30 s strikes a balance between
// "fast enough to catch deletions in a few cycles" and "not
// hammering the server with probes during a long outage".
const PROBE_COOLDOWN_MS = 30_000;

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
    // Permanent stop signal set when the HTTP existence probe returns
    // 404. Separate from ``cancelled`` (which is owned by the effect
    // cleanup) so a 404 mid-session doesn't have to pretend the
    // component unmounted — it just stops scheduling reconnects.
    let permanentlyStopped = false;
    let ws: WebSocket | null = null;
    let reconnectTimer: ReturnType<typeof setTimeout> | null = null;
    let attempt = 0;
    // Wall-clock of the most recent existence probe. The probe fires
    // at most once every PROBE_COOLDOWN_MS so a long disconnect
    // window still gets periodic re-checks (the resource could be
    // deleted MID-outage, well after a first probe returned ``true``
    // or ``null``). Using a timestamp rather than a streak flag
    // means the re-probe cadence is decoupled from onopen / onclose
    // ordering and works the same whether we've ever connected or
    // not.
    let lastProbeAt = 0;

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
      const url = wsUrl(`/api/captures/${captureId}/events`);
      ws = new WebSocket(url);
      ws.onopen = () => {
        attempt = 0;
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
        if (cancelled || permanentlyStopped) return;
        // Schedule the reconnect FIRST so a slow / hung probe doesn't
        // delay recovery. The probe runs concurrently and, if it
        // returns 404, sets ``permanentlyStopped`` + clears the
        // pending timer so the retry doesn't fire.
        scheduleReconnect();
        // Cooldown-gated probe: re-fires at most once per
        // PROBE_COOLDOWN_MS so a long outage still gets periodic
        // re-checks (the resource could be deleted mid-outage long
        // after the first probe returned ``true`` or ``null``).
        const now = Date.now();
        if (now - lastProbeAt >= PROBE_COOLDOWN_MS) {
          lastProbeAt = now;
          void probeCaptureExists(captureId).then((exists) => {
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

"use client";
import { useEffect, useRef, useState } from "react";
import clsx from "clsx";
import { CaptureStreamer } from "@/lib/streaming";
import type { Capture } from "@/lib/types";

interface Props {
  capture: Capture;
  pairToken: string;
}

type Phase = "idle" | "requesting" | "streaming" | "finalizing" | "queued" | "error";

export function MobileCaptureUI({ capture, pairToken }: Props) {
  const videoRef = useRef<HTMLVideoElement>(null);
  const streamerRef = useRef<CaptureStreamer | null>(null);
  const [phase, setPhase] = useState<Phase>("idle");
  const [stats, setStats] = useState({ received: 0, dropped: 0 });
  const [err, setErr] = useState<string | null>(null);
  const [sceneId, setSceneId] = useState<string | null>(null);

  useEffect(() => {
    return () => {
      streamerRef.current?.stop("user").catch(() => {});
    };
  }, []);

  async function start() {
    if (!videoRef.current) return;
    setPhase("requesting");
    try {
      const stream = await navigator.mediaDevices.getUserMedia({
        audio: false,
        video: {
          facingMode: { ideal: "environment" },
          width: { ideal: 1280 },
          height: { ideal: 720 },
          frameRate: { ideal: 30, max: 30 },
        },
      });
      const streamer = new CaptureStreamer(
        videoRef.current,
        { captureId: capture.id, pairToken, fps: 5, jpegQuality: 0.85 },
        {
          onAck: (a) => setStats({ received: a.received, dropped: a.dropped }),
          onLimit: () => {
            setErr("server frame cap reached — additional frames will be dropped");
          },
          onQueued: (id) => {
            setSceneId(id);
            setPhase("queued");
          },
          onError: (e) => {
            setErr(e.message);
            setPhase("error");
          },
          onClose: () => {
            if (videoRef.current?.srcObject instanceof MediaStream) {
              videoRef.current.srcObject.getTracks().forEach((t) => t.stop());
            }
          },
        },
      );
      streamerRef.current = streamer;
      await streamer.start(stream);
      setPhase("streaming");
    } catch (e) {
      setErr((e as Error).message);
      setPhase("error");
    }
  }

  async function finish() {
    setPhase("finalizing");
    await streamerRef.current?.stop("user");
  }

  return (
    <div className="relative h-screen w-screen overflow-hidden bg-black text-white">
      <video
        ref={videoRef}
        playsInline
        muted
        autoPlay
        className="absolute inset-0 w-full h-full object-cover"
      />

      {/* Minimal AR overlay: bounding rect + frame counter. Full
          Scaniverse-style coverage cones land in PR #2. */}
      <div className="absolute inset-8 border border-white/40 pointer-events-none" />

      <div className="absolute top-4 left-4 right-4 flex justify-between text-xs font-mono">
        <span>{capture.name}</span>
        <span>
          {stats.received} frames
          {stats.dropped > 0 && ` (${stats.dropped} dropped)`}
        </span>
      </div>

      <div className="absolute bottom-8 left-0 right-0 flex flex-col items-center gap-3">
        {phase === "idle" && (
          <button
            onClick={start}
            className="px-6 py-3 bg-white text-black text-lg font-semibold"
          >
            start capture
          </button>
        )}
        {phase === "requesting" && <p className="text-sm">requesting camera…</p>}
        {phase === "streaming" && (
          <button
            onClick={finish}
            className="px-6 py-3 bg-red-500 text-white text-lg font-semibold"
          >
            finish
          </button>
        )}
        {phase === "finalizing" && <p className="text-sm">finalizing…</p>}
        {phase === "queued" && (
          <a
            href={sceneId ? `/captures/${capture.id}` : "/"}
            className="px-6 py-3 bg-white text-black text-lg font-semibold"
          >
            view progress
          </a>
        )}
        {err && (
          <p
            className={clsx(
              "text-xs px-3 py-1",
              phase === "error" ? "bg-red-500" : "bg-yellow-500 text-black",
            )}
          >
            {err}
          </p>
        )}
      </div>
    </div>
  );
}

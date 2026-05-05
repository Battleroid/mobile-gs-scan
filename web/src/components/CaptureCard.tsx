"use client";
import Link from "next/link";
import { useMemo } from "react";
import { useQuery } from "@tanstack/react-query";
import clsx from "clsx";
import { api } from "@/lib/api";
import type { Capture, Job, Scene } from "@/lib/types";

// Four chip palettes, picked deterministically from a capture id so
// each card stays the same colour across reloads. Until the
// JobKind.thumbnail step (PR-D) ships rendered PNGs, this gradient is
// the placeholder thumbnail. Index choice is content-derived, not
// random, so reorders / re-renders don't reshuffle the shelf.
const PALETTES: [string, string][] = [
  ["var(--chip-1)", "var(--accent)"],
  ["var(--chip-2)", "var(--accent-2)"],
  ["var(--chip-3)", "var(--accent-3)"],
  ["var(--chip-4)", "var(--accent)"],
];

function paletteFor(id: string): [string, string] {
  let h = 0;
  for (let i = 0; i < id.length; i++) h = (h * 31 + id.charCodeAt(i)) | 0;
  return PALETTES[Math.abs(h) % PALETTES.length];
}

// Map the rich CaptureStatus union onto the design's three labels —
// keeps the per-card status pill aligned with the home page filter
// chips (Ready / Training / Failed). Uploading + queued read as
// "training" from the user's perspective.
function statusLabel(c: Capture): { label: string; tone: string } {
  if (c.status === "completed") return { label: "ready", tone: "text-accent3" };
  if (c.status === "failed" || c.status === "canceled")
    return { label: c.status, tone: "text-danger" };
  if (c.status === "processing") return { label: "training", tone: "text-accent" };
  if (c.status === "uploading") return { label: "uploading", tone: "text-accent" };
  return { label: c.status, tone: "text-muted" };
}

function relTime(iso: string): string {
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return "";
  const diff = Date.now() - t;
  if (diff < 60_000) return "just now";
  if (diff < 3_600_000) return `${Math.round(diff / 60_000)}m ago`;
  if (diff < 86_400_000) return `${Math.round(diff / 3_600_000)}h ago`;
  if (diff < 604_800_000) return `${Math.round(diff / 86_400_000)}d ago`;
  return new Date(iso).toLocaleDateString();
}

function progressOf(jobs: Job[] | undefined): number | null {
  if (!jobs?.length) return null;
  // The training (splatfacto) job is what dominates wall time; if
  // it's running we surface its progress directly. Otherwise show
  // the most-recent running job's progress so cards animate during
  // sfm / export too.
  const running = jobs.find((j) => j.status === "running");
  return running ? running.progress : null;
}

export function CaptureCard({ capture }: { capture: Capture }) {
  const [from, to] = paletteFor(capture.id);
  const { label, tone } = statusLabel(capture);
  const isTraining = capture.status === "processing" || capture.status === "queued";

  // Pull the scene ONLY for training rows — the only consumer of
  // this query is the progress overlay (rendered when isTraining
  // && progress !== null). Polling completed / failed / uploading
  // shelves would otherwise fire an N+1 burst of getScene requests
  // on every grid mount (plus tanstack's default refetch-on-focus
  // / refetch-on-mount) for data the card never reads. On a busy
  // shelf this dwarfs the cost of the actual list query.
  const { data: scene } = useQuery<Scene | null>({
    queryKey: ["scene", capture.scene_id],
    queryFn: () => api.getScene(capture.scene_id!),
    enabled: !!capture.scene_id && isTraining,
    refetchInterval: isTraining ? 3_000 : false,
  });

  const progress = progressOf(scene?.jobs);

  return (
    <Link
      href={`/captures/${capture.id}`}
      className="group block overflow-hidden rounded-lg border border-rule bg-surface shadow-[0_1px_0_rgba(0,0,0,0.02)] transition-transform hover:-translate-y-[2px] hover:shadow-md"
    >
      {/* Thumbnail. Gradient placeholder keyed off id; trained scenes
       *  with a thumb_url (PR-D) will override the gradient with a
       *  real PNG. Training cards get a light overlay strip showing
       *  splatfacto progress + a bottom progress bar. */}
      <div
        className="relative h-[180px] overflow-hidden"
        style={{
          background: `linear-gradient(135deg, ${from} 0%, ${to} 100%)`,
        }}
      >
        <Particles seed={capture.id} />
        {isTraining && progress !== null && (
          <>
            <div className="absolute left-3 right-3 top-3 flex justify-between font-mono text-[10px] text-white drop-shadow-[0_1px_2px_rgba(0,0,0,0.4)]">
              <span>● training · splatfacto</span>
              <span>{Math.round(progress * 100)}%</span>
            </div>
            <div className="absolute bottom-0 left-0 right-0 h-[3px] bg-white/30">
              <div
                className="h-full bg-white transition-[width] duration-500"
                style={{ width: `${progress * 100}%` }}
              />
            </div>
          </>
        )}
      </div>

      {/* Footer — title + status pill, then mono frame count + age. */}
      <div className="px-4 pb-4 pt-[14px]">
        <div className="flex items-baseline justify-between gap-2">
          <div className="truncate text-[16px] font-semibold tracking-[-0.01em] group-hover:text-accent">
            {capture.name}
          </div>
          <span
            className={clsx(
              "shrink-0 font-mono text-[10px] uppercase tracking-[0.08em]",
              tone,
            )}
          >
            {label}
          </span>
        </div>
        <div className="mt-[6px] flex justify-between font-mono text-[11px] text-muted">
          <span>{capture.frame_count} frames</span>
          <span>{relTime(capture.updated_at || capture.created_at)}</span>
        </div>
      </div>
    </Link>
  );
}

// Sparse pseudo-splat texture for the gradient. Pure SVG, no Three —
// the design's SplatViz is a real-time point cloud renderer that's
// way too heavy to mount per card. This is the static stand-in until
// PR-D ships a server-rendered PNG via JobKind.thumbnail.
function Particles({ seed }: { seed: string }) {
  const dots = useMemo(() => dotsFor(seed), [seed]);
  return (
    <svg className="absolute inset-0 h-full w-full opacity-90" viewBox="0 0 300 180">
      {dots.map((d, i) => (
        <circle
          key={i}
          cx={d.x}
          cy={d.y}
          r={d.r}
          fill="white"
          fillOpacity={d.a}
        />
      ))}
    </svg>
  );
}

function dotsFor(seed: string) {
  // Deterministic pseudo-random so cards keep the same particle
  // pattern across renders. Pure helper so it's safe to memoize per
  // seed without tripping React 19's render-purity lint rule.
  let s = 0;
  for (let i = 0; i < seed.length; i++) s = (s * 31 + seed.charCodeAt(i)) | 0;
  const rand = () => {
    s = (s * 1664525 + 1013904223) | 0;
    return ((s >>> 0) % 10000) / 10000;
  };
  return Array.from({ length: 60 }, () => ({
    x: rand() * 300,
    y: rand() * 180,
    r: 0.8 + rand() * 1.6,
    a: 0.18 + rand() * 0.42,
  }));
}

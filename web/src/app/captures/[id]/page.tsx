"use client";
import { use } from "react";
import { useQuery } from "@tanstack/react-query";
import dynamic from "next/dynamic";
import clsx from "clsx";
import { api } from "@/lib/api";
import { useCaptureEvents } from "@/hooks/useCaptureEvents";
import { useSceneEvents } from "@/hooks/useSceneEvents";
import { CapturePairing } from "@/components/CapturePairing";
import type { Job } from "@/lib/types";

// Heavy three.js viewer — keep it out of the SSR bundle.
const SplatViewer = dynamic(
  () => import("@/components/SplatViewer").then((m) => m.SplatViewer),
  { ssr: false },
);

interface PageProps {
  params: Promise<{ id: string }>;
}

export default function CaptureDetailPage({ params }: PageProps) {
  const { id } = use(params);

  const { data: initial } = useQuery({
    queryKey: ["capture", id],
    queryFn: () => api.getCapture(id),
  });
  const { capture: live } = useCaptureEvents(id);
  const capture = live ?? initial ?? null;

  const sceneId = capture?.scene_id ?? null;
  const { scene } = useSceneEvents(sceneId);

  if (!capture) return <p className="text-muted">loading…</p>;

  return (
    <div className="space-y-6">
      <header>
        <h1 className="text-lg font-semibold">{capture.name}</h1>
        <p className="text-xs text-muted">
          {capture.id} · {capture.source} · {capture.status}
        </p>
      </header>

      {capture.status === "pairing" && capture.pair_url && (
        <section className="border border-rule p-4">
          <h2 className="text-sm mb-3 text-muted">scan from your phone</h2>
          <CapturePairing pairUrl={capture.pair_url} />
        </section>
      )}

      {(capture.status === "streaming" ||
        capture.status === "uploading" ||
        capture.status === "queued") && (
        <section className="border border-rule p-4 space-y-1">
          <h2 className="text-sm text-muted">capturing</h2>
          <p>
            {capture.frame_count} frames received
            {capture.dropped_count > 0 && `, ${capture.dropped_count} dropped`}
          </p>
        </section>
      )}

      {sceneId && scene && (
        <section className="space-y-3">
          <h2 className="text-sm text-muted">pipeline</h2>
          <ul className="space-y-2">
            {scene.jobs.map((j) => (
              <JobRow key={j.id} job={j} />
            ))}
          </ul>

          {scene.status === "completed" && (scene.ply_url || scene.spz_url) && (
            <div className="space-y-3">
              <h2 className="text-sm text-muted mt-6">scene</h2>
              <SplatViewer
                url={api.base() + (scene.spz_url ?? scene.ply_url!)}
              />
              <div className="flex gap-3 text-xs">
                {scene.ply_url && (
                  <a
                    href={api.base() + scene.ply_url}
                    download
                    className="underline hover:text-fg"
                  >
                    download .ply
                  </a>
                )}
                {scene.spz_url && (
                  <a
                    href={api.base() + scene.spz_url}
                    download
                    className="underline hover:text-fg"
                  >
                    download .spz
                  </a>
                )}
              </div>
            </div>
          )}

          {scene.status === "failed" && (
            <p className="text-danger text-sm">
              pipeline failed: {scene.error ?? "unknown error"}
            </p>
          )}
        </section>
      )}
    </div>
  );
}

function JobRow({ job }: { job: Job }) {
  return (
    <li className="border border-rule px-3 py-2">
      <div className="flex items-baseline justify-between text-sm">
        <span>{job.kind}</span>
        <span
          className={clsx(
            "text-xs",
            job.status === "completed" && "text-fg",
            job.status === "running" && "text-warn",
            job.status === "failed" && "text-danger",
            (job.status === "queued" || job.status === "claimed") && "text-muted",
          )}
        >
          {job.status}
        </span>
      </div>
      <div className="mt-1 h-1 bg-rule">
        <div
          className="h-full bg-accent transition-all"
          style={{ width: `${Math.round(job.progress * 100)}%` }}
        />
      </div>
      {job.progress_msg && (
        <p className="text-xs text-muted mt-1">{job.progress_msg}</p>
      )}
      {job.error && <p className="text-xs text-danger mt-1">{job.error}</p>}
    </li>
  );
}

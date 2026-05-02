"use client";
import { use, useState } from "react";
import { useRouter } from "next/navigation";
import { useQuery } from "@tanstack/react-query";
import dynamic from "next/dynamic";
import clsx from "clsx";
import { api } from "@/lib/api";
import { useCaptureEvents } from "@/hooks/useCaptureEvents";
import { useSceneEvents } from "@/hooks/useSceneEvents";
import { CapturePairing } from "@/components/CapturePairing";
import { JobLogPanel } from "@/components/JobLogPanel";
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
  const router = useRouter();

  const { data: initial } = useQuery({
    queryKey: ["capture", id],
    queryFn: () => api.getCapture(id),
  });
  const { capture: live } = useCaptureEvents(id);
  const capture = live ?? initial ?? null;

  const sceneId = capture?.scene_id ?? null;
  const { scene } = useSceneEvents(sceneId);

  const [deleting, setDeleting] = useState(false);

  const onDelete = async () => {
    if (!capture) return;
    if (
      !window.confirm(
        "Delete this capture? All frames and pipeline artifacts will be removed. In-flight jobs will be canceled.",
      )
    ) {
      return;
    }
    setDeleting(true);
    try {
      const res = await api.deleteCapture(capture.id);
      if (!res.ok) {
        throw new Error(`HTTP ${res.status}`);
      }
      router.push("/");
    } catch (err) {
      window.alert(`delete failed: ${(err as Error).message}`);
      setDeleting(false);
    }
  };

  if (!capture) return <p className="text-muted">loading…</p>;

  return (
    <div className="space-y-6">
      <header className="flex items-start justify-between gap-4">
        <div className="min-w-0">
          <h1 className="text-lg font-semibold truncate">{capture.name}</h1>
          <p className="text-xs text-muted">
            {capture.id} · {capture.source} · {capture.status}
          </p>
        </div>
        <button
          type="button"
          onClick={onDelete}
          disabled={deleting}
          className="text-xs text-danger underline hover:text-fg disabled:opacity-50"
        >
          {deleting ? "deleting…" : "delete"}
        </button>
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
  const cancelable =
    job.status === "queued" ||
    job.status === "claimed" ||
    job.status === "running";
  const [cancelling, setCancelling] = useState(false);

  const onCancel = async () => {
    if (!window.confirm(`Cancel ${job.kind} job?`)) return;
    setCancelling(true);
    try {
      await api.cancelJob(job.id);
    } catch (err) {
      window.alert(`cancel failed: ${(err as Error).message}`);
    } finally {
      setCancelling(false);
    }
  };

  return (
    <li className="border border-rule px-3 py-2">
      <div className="flex items-baseline justify-between text-sm">
        <span>{job.kind}</span>
        <div className="flex items-center gap-2">
          <span
            className={clsx(
              "text-xs",
              job.status === "completed" && "text-fg",
              job.status === "running" && "text-warn",
              job.status === "failed" && "text-danger",
              job.status === "canceled" && "text-muted",
              (job.status === "queued" || job.status === "claimed") && "text-muted",
            )}
          >
            {job.status}
          </span>
          {cancelable && (
            <button
              type="button"
              onClick={onCancel}
              disabled={cancelling}
              className="text-xs text-danger underline hover:text-fg disabled:opacity-50"
            >
              {cancelling ? "…" : "cancel"}
            </button>
          )}
        </div>
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
      <JobLogPanel job={job} />
    </li>
  );
}

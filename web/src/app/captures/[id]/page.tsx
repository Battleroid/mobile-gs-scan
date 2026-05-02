"use client";
import { use, useRef, useState } from "react";
import { useRouter } from "next/navigation";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import dynamic from "next/dynamic";
import clsx from "clsx";
import { api } from "@/lib/api";
import { useCaptureEvents } from "@/hooks/useCaptureEvents";
import { useSceneEvents } from "@/hooks/useSceneEvents";
import { CapturePairing } from "@/components/CapturePairing";
import { JobLogPanel } from "@/components/JobLogPanel";
import type { Capture, Job } from "@/lib/types";

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
        <div className="min-w-0 flex-1">
          <CaptureNameEditor capture={capture} />
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
                pointsUrl={
                  scene.ply_url ? api.base() + scene.ply_url : undefined
                }
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

/**
 * Inline rename: the name is normally rendered as a heading; click
 * "rename" to swap into edit mode. Enter submits, Escape cancels.
 * Keeps the rest of the page layout untouched while editing.
 */
function CaptureNameEditor({ capture }: { capture: Capture }) {
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(capture.name);
  const [saving, setSaving] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  // No need to sync `draft` to `capture.name` while not editing —
  // the heading renders capture.name directly when editing is
  // false, so a stale local draft is invisible. startEdit below
  // re-seeds draft from capture.name on entry to edit mode, which
  // covers the only case where the value matters.

  const startEdit = () => {
    setDraft(capture.name);
    setEditing(true);
    queueMicrotask(() => {
      inputRef.current?.focus();
      inputRef.current?.select();
    });
  };

  const cancel = () => {
    setEditing(false);
    setDraft(capture.name);
  };

  const save = async () => {
    const trimmed = draft.trim();
    if (!trimmed || trimmed === capture.name) {
      cancel();
      return;
    }
    setSaving(true);
    try {
      const updated = await api.renameCapture(capture.id, trimmed);
      // Push the new value into the per-id cache so the rest of the
      // app's useQuery({ queryKey: ["capture", id] }) consumers
      // refresh without waiting for the next poll. The websocket
      // capture.renamed event will also fire, but the cache write
      // is what makes the page feel instantaneous.
      queryClient.setQueryData(["capture", capture.id], updated);
      setEditing(false);
    } catch (err) {
      window.alert(`rename failed: ${(err as Error).message}`);
    } finally {
      setSaving(false);
    }
  };

  if (editing) {
    return (
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void save();
        }}
        className="flex items-center gap-2"
      >
        <input
          ref={inputRef}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Escape") {
              e.preventDefault();
              cancel();
            }
          }}
          maxLength={200}
          disabled={saving}
          className="flex-1 bg-transparent border-b border-rule text-lg font-semibold focus:outline-none focus:border-accent disabled:opacity-50"
        />
        <button
          type="submit"
          disabled={saving}
          className="text-xs underline hover:text-fg disabled:opacity-50"
        >
          {saving ? "saving…" : "save"}
        </button>
        <button
          type="button"
          onClick={cancel}
          disabled={saving}
          className="text-xs text-muted underline hover:text-fg disabled:opacity-50"
        >
          cancel
        </button>
      </form>
    );
  }

  return (
    <div className="flex items-center gap-2">
      <h1 className="text-lg font-semibold truncate">{capture.name}</h1>
      <button
        type="button"
        onClick={startEdit}
        className="text-xs text-muted underline hover:text-fg"
      >
        rename
      </button>
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

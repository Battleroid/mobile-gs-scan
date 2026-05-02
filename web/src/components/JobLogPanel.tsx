"use client";
import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { api } from "@/lib/api";
import type { Job } from "@/lib/types";

/**
 * Collapsible per-step log panel.
 *
 * Auto-opens for jobs that are currently running so the user gets
 * a live tail without having to interact. While open AND the job
 * is running, polls /api/jobs/{id}/log every 2s. Closes
 * automatically once the job leaves the running state, but can be
 * re-opened on demand for completed / failed jobs to inspect what
 * happened.
 *
 * The mesh job's log path resolves to None server-side (no
 * subprocess in PR #1's stub), so the panel renders a quiet "no
 * log" placeholder for it instead of an empty <pre>.
 */
export function JobLogPanel({ job }: { job: Job }) {
  const isRunning = job.status === "running" || job.status === "claimed";
  // Default-open for running jobs (live tail), default-closed for
  // completed / failed / queued (user opts in if they care).
  const [open, setOpen] = useState(isRunning);

  const { data, isFetching, error } = useQuery({
    queryKey: ["job-log", job.id],
    queryFn: () => api.getJobLog(job.id),
    enabled: open,
    // Only poll while the job is still moving. Once it's done the
    // log file stops growing, so a single fetch is enough.
    refetchInterval: open && isRunning ? 2_000 : false,
    // Don't refetch on focus / mount when we already have a fresh
    // snapshot — the polling interval handles freshness.
    refetchOnWindowFocus: false,
    staleTime: 1_000,
  });

  if (!open) {
    return (
      <button
        type="button"
        onClick={() => setOpen(true)}
        className="mt-2 text-xs text-muted underline hover:text-fg"
      >
        show log
      </button>
    );
  }

  return (
    <div className="mt-2">
      <div className="flex items-center gap-2">
        <button
          type="button"
          onClick={() => setOpen(false)}
          className="text-xs text-muted underline hover:text-fg"
        >
          hide log
        </button>
        {isFetching && (
          <span className="text-xs text-muted">refreshing…</span>
        )}
      </div>

      {error ? (
        <p className="mt-1 text-xs text-danger">
          could not load log: {(error as Error).message}
        </p>
      ) : data?.available ? (
        <pre className="mt-1 p-2 bg-rule text-fg text-[10px] font-mono whitespace-pre-wrap max-h-64 overflow-y-auto">
          {data.log || "(log file is empty)"}
        </pre>
      ) : (
        <p className="mt-1 text-xs text-muted">
          no log available for this step
        </p>
      )}
    </div>
  );
}

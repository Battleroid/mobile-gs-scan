"use client";
import { useEffect, useRef, useState } from "react";
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
 * Auto-scroll behaviour:
 *   - "follow log" toggle defaults ON while the job is in
 *     running / claimed state, OFF once it lands.
 *   - When ON, the <pre> scrolls to the bottom on every successful
 *     poll so the latest log line is visible without manual
 *     intervention.
 *   - Scrolling up by more than the small near-bottom tolerance
 *     (24 px) auto-disables follow so the user can read older
 *     output without being yanked back to the bottom on the next
 *     poll. Scrolling back to the bottom re-engages follow.
 *   - The "follow" toggle button can also be flipped manually for
 *     completed jobs whose log file is final but big enough to
 *     scroll.
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
  // Default-on for running jobs so new lines are visible without
  // scrolling. Off for finished jobs so the user can read from the
  // top of the captured tail without being scroll-jacked.
  const [follow, setFollow] = useState(isRunning);

  const preRef = useRef<HTMLPreElement | null>(null);

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

  // Push the scrollTop to the bottom whenever the log body changes
  // and the user wants follow. Runs after the new content is in the
  // DOM (effect, not render) so scrollHeight reflects the updated
  // size. Skipping when follow is off keeps the user's current view
  // anchored where they put it.
  useEffect(() => {
    if (!follow) return;
    const el = preRef.current;
    if (!el) return;
    el.scrollTop = el.scrollHeight;
  }, [data?.log, follow]);

  // Reset `follow` to false when the job transitions out of the
  // running/claimed state, so the user can browse the captured
  // tail freely without being scroll-jacked. React 19 forbids
  // setState() inside an effect for this kind of state-syncing
  // (react-hooks/set-state-in-effect); use the documented
  // "adjust state on prop change during render" pattern instead.
  // https://react.dev/learn/you-might-not-need-an-effect#adjusting-some-state-when-a-prop-changes
  const [prevIsRunning, setPrevIsRunning] = useState(isRunning);
  if (prevIsRunning !== isRunning) {
    setPrevIsRunning(isRunning);
    if (!isRunning) setFollow(false);
  }

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

  // True when the user's view is within the near-bottom tolerance.
  // Used by the onScroll handler to decide whether to disengage or
  // re-engage follow as the user scrolls.
  const isNearBottom = (el: HTMLPreElement) => {
    const distance = el.scrollHeight - el.scrollTop - el.clientHeight;
    return distance <= 24;
  };

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
        <label className="text-xs text-muted flex items-center gap-1 cursor-pointer">
          <input
            type="checkbox"
            checked={follow}
            onChange={(e) => setFollow(e.target.checked)}
            className="accent-accent"
          />
          follow
        </label>
        {isFetching && (
          <span className="text-xs text-muted">refreshing…</span>
        )}
      </div>

      {error ? (
        <p className="mt-1 text-xs text-danger">
          could not load log: {(error as Error).message}
        </p>
      ) : data?.available ? (
        <pre
          ref={preRef}
          onScroll={(e) => {
            const el = e.currentTarget;
            const near = isNearBottom(el);
            // Auto-disengage on scroll up; auto-re-engage when the
            // user scrolls back down to the bottom. Avoid setState
            // when the value hasn't actually changed so we don't
            // spam React's reconciler on every wheel tick.
            if (near && !follow) setFollow(true);
            if (!near && follow) setFollow(false);
          }}
          className="mt-1 p-2 bg-rule text-fg text-[10px] font-mono whitespace-pre-wrap max-h-64 overflow-y-auto"
        >
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

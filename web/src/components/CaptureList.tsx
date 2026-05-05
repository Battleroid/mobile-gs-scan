"use client";
import Link from "next/link";
import { useMemo, useState } from "react";
import { useQuery } from "@tanstack/react-query";
import clsx from "clsx";
import { api } from "@/lib/api";
import type { Capture } from "@/lib/types";
import { BigButton, FilterChip } from "./pebble";
import { CaptureCard } from "./CaptureCard";

type Filter = "all" | "ready" | "training" | "failed";

const FILTERS: { id: Filter; label: string }[] = [
  { id: "all", label: "All" },
  { id: "ready", label: "Ready" },
  { id: "training", label: "Training" },
  { id: "failed", label: "Failed" },
];

// Map our richer CaptureStatus union onto the design's three filter
// buckets. Anything in flight (uploading / queued / processing) is
// "training" from the user's perspective; failed includes canceled
// since neither produced a usable scene.
function bucketize(c: Capture): Filter {
  if (c.status === "completed") return "ready";
  if (c.status === "failed" || c.status === "canceled") return "failed";
  return "training";
}

export function CaptureList() {
  const [filter, setFilter] = useState<Filter>("all");
  const [query, setQuery] = useState("");

  const { data, isLoading, error } = useQuery({
    queryKey: ["captures"],
    queryFn: api.listCaptures,
    refetchInterval: 3_000,
  });

  const filtered = useMemo(() => {
    if (!data) return [];
    const q = query.trim().toLowerCase();
    return data.filter((c) => {
      if (filter !== "all" && bucketize(c) !== filter) return false;
      if (q && !c.name.toLowerCase().includes(q)) return false;
      return true;
    });
  }, [data, filter, query]);

  return (
    <div className="mx-auto w-full max-w-6xl px-9">
      {/* Hero — display "Captures" + mono kicker on the left, filter
       *  chips + search + new-scan CTA on the right. The design puts
       *  these on a single line on desktop; we wrap to a second row
       *  below `md` so the chips don't get cramped. */}
      <div className="flex flex-col gap-4 pb-2 pt-7 md:flex-row md:items-end md:justify-between">
        <div>
          <div className="mb-2 font-mono text-[11px] uppercase tracking-[0.12em] text-muted">
            your shelf · {data?.length ?? 0} {data?.length === 1 ? "scan" : "scans"}
          </div>
          <h1 className="m-0 text-[52px] font-bold leading-none tracking-[-0.02em]">
            Captures
          </h1>
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {FILTERS.map((f) => (
            <FilterChip
              key={f.id}
              active={filter === f.id}
              onClick={() => setFilter(f.id)}
            >
              {f.label}
            </FilterChip>
          ))}
          <span className="mx-1 h-[22px] w-px bg-rule" />
          <input
            type="search"
            value={query}
            onChange={(e) => setQuery(e.target.value)}
            placeholder="search…"
            className="w-32 rounded-md border border-rule bg-surface px-3 py-[6px] text-sm placeholder:text-muted focus:border-accent focus:outline-none"
          />
          <BigButton href="/captures/new">＋ New scan</BigButton>
        </div>
      </div>

      {/* Body — grid, loading, error, empty all share the same outer
       *  spacing so the page doesn't jolt as state changes. */}
      <div className="py-6">
        {isLoading && <p className="text-muted">loading…</p>}
        {error && (
          <p className="text-danger">{(error as Error).message}</p>
        )}
        {!isLoading && !error && filtered.length === 0 && (
          <EmptyState hasAny={(data?.length ?? 0) > 0} />
        )}
        {!isLoading && !error && filtered.length > 0 && (
          <div className="grid grid-cols-1 gap-[18px] sm:grid-cols-2 lg:grid-cols-3">
            {filtered.map((c) => (
              <CaptureCard key={c.id} capture={c} />
            ))}
          </div>
        )}
      </div>
    </div>
  );
}

function EmptyState({ hasAny }: { hasAny: boolean }) {
  if (hasAny) {
    return (
      <div className="rounded-lg border border-rule bg-surface p-10 text-center">
        <div className="font-mono text-[10px] uppercase tracking-[0.12em] text-muted">
          no matches
        </div>
        <div className="mt-2 text-fg">
          Try clearing the filter or search.
        </div>
      </div>
    );
  }
  return (
    <div
      className={clsx(
        "rounded-lg border border-rule bg-surface p-10 text-center",
      )}
    >
      <div className="font-mono text-[10px] uppercase tracking-[0.12em] text-muted">
        empty shelf
      </div>
      <div className="mt-2 text-[22px] font-bold tracking-[-0.02em]">
        Nothing here yet.
      </div>
      <p className="mx-auto mt-2 max-w-md text-[14px] text-inkSoft">
        Drop a folder of frames or a video file at{" "}
        <Link href="/captures/new" className="text-accent underline">
          new scan
        </Link>{" "}
        — or open the Pebble app on your phone and start recording. Captures
        appear here as they finish.
      </p>
    </div>
  );
}

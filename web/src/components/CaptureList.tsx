"use client";
import Link from "next/link";
import { useQuery } from "@tanstack/react-query";
import clsx from "clsx";
import { api } from "@/lib/api";
import type { Capture } from "@/lib/types";

const STATUS_TONE: Record<Capture["status"], string> = {
  created: "text-muted",
  pairing: "text-accent",
  streaming: "text-accent",
  uploading: "text-accent",
  queued: "text-muted",
  processing: "text-warn",
  completed: "text-fg",
  failed: "text-danger",
  canceled: "text-muted",
};

export function CaptureList() {
  const { data, isLoading, error } = useQuery({
    queryKey: ["captures"],
    queryFn: api.listCaptures,
    refetchInterval: 3_000,
  });

  if (isLoading) return <p className="text-muted">loading…</p>;
  if (error) return <p className="text-danger">{(error as Error).message}</p>;
  if (!data?.length)
    return (
      <p className="text-muted">
        no captures yet. <Link href="/captures/new" className="underline">start one</Link>.
      </p>
    );

  return (
    <ul className="divide-y divide-rule">
      {data.map((c) => (
        <li key={c.id} className="py-3">
          <Link
            href={`/captures/${c.id}`}
            className="flex items-baseline justify-between gap-4 hover:bg-rule/30 px-2 -mx-2 py-1"
          >
            <div className="flex-1 min-w-0">
              <div className="truncate">{c.name}</div>
              <div className="text-xs text-muted">
                {c.source} · {c.frame_count} frames
                {c.dropped_count > 0 && ` (${c.dropped_count} dropped)`}
              </div>
            </div>
            <span className={clsx("text-xs", STATUS_TONE[c.status])}>
              {c.status}
            </span>
          </Link>
        </li>
      ))}
    </ul>
  );
}

"use client";
import { useState } from "react";
import { useMutation } from "@tanstack/react-query";
import { api } from "@/lib/api";
import { CapturePairing } from "@/components/CapturePairing";
import { UploadDropzone } from "@/components/UploadDropzone";
import type { Capture } from "@/lib/types";

type Tab = "phone" | "upload";

export default function NewCapturePage() {
  const [tab, setTab] = useState<Tab>("phone");
  const [name, setName] = useState("");

  return (
    <div className="space-y-6">
      <h1 className="text-lg font-semibold">new capture</h1>

      <div className="flex border-b border-rule text-sm">
        <button
          onClick={() => setTab("phone")}
          className={tabClass(tab === "phone")}
        >
          phone capture
        </button>
        <button
          onClick={() => setTab("upload")}
          className={tabClass(tab === "upload")}
        >
          upload set
        </button>
      </div>

      <input
        value={name}
        onChange={(e) => setName(e.target.value)}
        placeholder="session name (optional — server picks a memorable random name if blank)"
        className="w-full bg-rule/30 border border-rule px-3 py-2 outline-none focus:border-accent"
      />

      {tab === "phone" ? (
        <PhonePairPanel name={name} />
      ) : (
        <div className="space-y-3">
          <p className="text-sm text-muted">
            drag-and-drop runs the server-side SfM (Glomap → splatfacto
            → export). good for drone / DSLR sets.
          </p>
          <UploadDropzone />
        </div>
      )}
    </div>
  );
}

function tabClass(active: boolean) {
  return [
    "px-3 py-2 -mb-px border-b",
    active ? "border-accent text-fg" : "border-transparent text-muted hover:text-fg",
  ].join(" ");
}

function PhonePairPanel({ name }: { name: string }) {
  const [capture, setCapture] = useState<Capture | null>(null);

  const m = useMutation({
    mutationFn: () =>
      api.createCapture({
        // Pass the user's text only if non-empty after trim; let
        // the server auto-generate a memorable name otherwise. The
        // capture detail page surfaces a rename UI for after-the-
        // fact corrections.
        name: name.trim() || undefined,
        source: "mobile_native",
        has_pose: true,
        meta: {},
      }),
    onSuccess: setCapture,
  });

  if (capture) {
    return (
      <div className="space-y-4">
        <CapturePairing pairUrl={capture.pair_url ?? "#"} />
        <p className="text-xs text-muted text-center">
          waiting for phone to connect…{" "}
          <a
            href={`/captures/${capture.id}`}
            className="underline hover:text-fg"
          >
            open progress page
          </a>
        </p>
      </div>
    );
  }

  return (
    <div className="space-y-3">
      <p className="text-sm text-muted">
        phone capture uses ARCore poses (Android app) or device-motion +
        SfM (PWA fallback). leave the name blank to let the server
        pick a memorable one for you, or type your own — you can
        rename later either way.
      </p>
      <button
        onClick={() => m.mutate()}
        disabled={m.isPending}
        className="px-4 py-2 border border-rule hover:bg-rule/30 disabled:opacity-60"
      >
        {m.isPending ? "starting…" : "start phone session"}
      </button>
      {m.error && (
        <p className="text-xs text-danger">{(m.error as Error).message}</p>
      )}
    </div>
  );
}

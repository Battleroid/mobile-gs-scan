"use client";
import { use, useEffect, useState } from "react";
import { api } from "@/lib/api";
import { MobileCaptureUI } from "@/components/MobileCaptureUI";
import type { Capture } from "@/lib/types";

interface Props {
  params: Promise<{ token: string }>;
}

export default function MobilePairPage({ params }: Props) {
  const { token } = use(params);
  const [capture, setCapture] = useState<Capture | null>(null);
  const [err, setErr] = useState<string | null>(null);

  useEffect(() => {
    api.resolvePairToken(token).then(setCapture).catch((e) => setErr(e.message));
  }, [token]);

  if (err) {
    return (
      <div className="h-screen flex flex-col items-center justify-center text-center p-6">
        <p className="text-danger">pair token invalid or expired</p>
        <p className="text-xs text-muted mt-2">{err}</p>
      </div>
    );
  }
  if (!capture) {
    return (
      <div className="h-screen flex items-center justify-center">
        <p className="text-muted">resolving session…</p>
      </div>
    );
  }
  return <MobileCaptureUI capture={capture} pairToken={token} />;
}

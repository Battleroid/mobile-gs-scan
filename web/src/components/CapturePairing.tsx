"use client";
import { useEffect, useState } from "react";
import { renderQrSvg } from "@/lib/qr";

interface Props {
  pairUrl: string; // path component, e.g. "/m/<token>"
  size?: number;
}

export function CapturePairing({ pairUrl, size = 256 }: Props) {
  const [state, setState] = useState<{ abs: string; svg: string }>({
    abs: "",
    svg: "",
  });

  useEffect(() => {
    // Browser-only: window.location.origin isn't available during SSR.
    // Set state inside the async callback rather than synchronously to
    // satisfy react-hooks/set-state-in-effect.
    if (typeof window === "undefined") return;
    const abs = window.location.origin + pairUrl;
    let cancelled = false;
    void renderQrSvg(abs, size).then((svg) => {
      if (!cancelled) setState({ abs, svg });
    });
    return () => {
      cancelled = true;
    };
  }, [pairUrl, size]);

  return (
    <div className="flex flex-col items-center gap-3">
      <div
        className="bg-white p-3"
        style={{ width: size + 24, height: size + 24 }}
        dangerouslySetInnerHTML={{ __html: state.svg }}
      />
      <code className="text-xs text-muted break-all max-w-xs text-center">
        {state.abs}
      </code>
      <p className="text-xs text-muted max-w-xs text-center">
        Scan with your phone camera. The PWA opens at this URL; the
        Android app intercepts it via deep link if installed.
      </p>
    </div>
  );
}

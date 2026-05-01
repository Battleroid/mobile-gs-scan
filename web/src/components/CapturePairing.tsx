"use client";
import { useEffect, useState } from "react";
import { renderQrSvg } from "@/lib/qr";

interface Props {
  pairUrl: string; // path component, e.g. "/m/<token>"
  size?: number;
}

export function CapturePairing({ pairUrl, size = 256 }: Props) {
  const [svg, setSvg] = useState<string>("");
  const [absoluteUrl, setAbsoluteUrl] = useState<string>("");

  useEffect(() => {
    if (typeof window === "undefined") return;
    const abs = window.location.origin + pairUrl;
    setAbsoluteUrl(abs);
    void renderQrSvg(abs, size).then(setSvg);
  }, [pairUrl, size]);

  return (
    <div className="flex flex-col items-center gap-3">
      <div
        className="bg-white p-3"
        style={{ width: size + 24, height: size + 24 }}
        dangerouslySetInnerHTML={{ __html: svg }}
      />
      <code className="text-xs text-muted break-all max-w-xs text-center">
        {absoluteUrl}
      </code>
      <p className="text-xs text-muted max-w-xs text-center">
        Scan with your phone camera. The PWA opens at this URL; the
        Android app intercepts it via deep link if installed.
      </p>
    </div>
  );
}

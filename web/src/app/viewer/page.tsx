"use client";
// Dedicated full-screen viewer route. Used by SplatViewer's
// "new tab" pop-out button so the user can blow up a splat to a
// dedicated browser tab without the rest of the studio chrome.
//
// Reads ``url`` (required) plus ``pointsUrl`` / ``meshGlbUrl`` /
// ``meshObjUrl`` (all optional) from query params; every value is
// an absolute, fully-qualified API URL the embedding caller
// already resolved via ``api.base() + ...``. The mesh URLs let
// pop-out preserve the 4th view-mode (mesh) option from the
// originating embed.
import { Suspense } from "react";
import { useSearchParams } from "next/navigation";
import dynamic from "next/dynamic";

const SplatViewer = dynamic(
  () => import("@/components/SplatViewer").then((m) => m.SplatViewer),
  { ssr: false },
);

export default function ViewerPage() {
  return (
    <Suspense fallback={null}>
      <ViewerInner />
    </Suspense>
  );
}

function ViewerInner() {
  const params = useSearchParams();
  const url = params.get("url");
  const pointsUrl = params.get("pointsUrl") ?? undefined;
  const meshGlbUrl = params.get("meshGlbUrl") ?? undefined;
  const meshObjUrl = params.get("meshObjUrl") ?? undefined;

  if (!url) {
    return (
      <p className="p-4 text-muted text-sm">
        missing ?url= query parameter
      </p>
    );
  }

  return (
    <SplatViewer
      url={url}
      pointsUrl={pointsUrl}
      meshGlbUrl={meshGlbUrl}
      meshObjUrl={meshObjUrl}
      fillScreen
    />
  );
}

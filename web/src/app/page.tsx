import { Suspense } from "react";
import { CaptureList } from "@/components/CaptureList";

// Home shell is a thin wrapper — the design's hero, filter chips,
// search, and grid all live inside CaptureList because they all
// depend on the same query state (filter + search + the captures
// data).
//
// Suspense boundary is required because CaptureList reads the
// filter chip state from `useSearchParams()` (so the URL drives
// `?filter=ready|training|failed` and the choice survives a
// navigation round-trip into a capture detail page). Next 16 then
// can't statically prerender the page; the boundary lets it bail
// to client-side rendering for the dynamic chunk while the rest
// of the layout (header, etc.) stays static.
export default function HomePage() {
  return (
    <Suspense fallback={null}>
      <CaptureList />
    </Suspense>
  );
}

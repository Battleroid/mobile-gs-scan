import { CaptureList } from "@/components/CaptureList";

// Home shell is a thin wrapper — the design's hero, filter chips,
// search, and grid all live inside CaptureList because they all
// depend on the same query state (filter + search + the captures
// data).
export default function HomePage() {
  return <CaptureList />;
}

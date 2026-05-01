import Link from "next/link";
import { CaptureList } from "@/components/CaptureList";

export default function HomePage() {
  return (
    <div className="space-y-6">
      <div className="flex items-baseline justify-between">
        <h1 className="text-lg font-semibold">captures</h1>
        <Link
          href="/captures/new"
          className="px-3 py-1 border border-rule text-sm hover:bg-rule/30"
        >
          new capture
        </Link>
      </div>
      <CaptureList />
    </div>
  );
}

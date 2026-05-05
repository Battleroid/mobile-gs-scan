"use client";
import { useState } from "react";
import { UploadDropzone } from "@/components/UploadDropzone";

export default function NewCapturePage() {
  const [name, setName] = useState("");

  return (
    <div className="space-y-6">
      <h1 className="text-lg font-semibold">new capture</h1>

      <input
        value={name}
        onChange={(e) => setName(e.target.value)}
        placeholder="session name (optional — server picks a memorable random name if blank)"
        className="w-full bg-rule/30 border border-rule px-3 py-2 outline-none focus:border-accent"
      />

      <div className="space-y-3">
        <p className="text-sm text-muted">
          drop a folder of images or a single video. the server runs
          ffmpeg → SfM (Glomap) → splatfacto → export.
        </p>
        <UploadDropzone name={name} />
      </div>
    </div>
  );
}

"use client";
import { useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import clsx from "clsx";
import { api } from "@/lib/api";

export function UploadDropzone() {
  const router = useRouter();
  const [over, setOver] = useState(false);
  const [pending, startTransition] = useTransition();
  const [progress, setProgress] = useState<string | null>(null);

  function onDrop(e: React.DragEvent) {
    e.preventDefault();
    setOver(false);
    const files = Array.from(e.dataTransfer.files).filter((f) =>
      f.type.startsWith("image/"),
    );
    if (!files.length) return;
    void handle(files);
  }

  async function onPick(e: React.ChangeEvent<HTMLInputElement>) {
    const files = Array.from(e.target.files ?? []);
    if (!files.length) return;
    await handle(files);
  }

  async function handle(files: File[]) {
    const name = files[0]?.webkitRelativePath?.split("/")[0] ?? "uploaded set";
    setProgress(`creating capture for ${files.length} files…`);
    const cap = await api.createCapture({
      name,
      source: "upload",
      meta: { count: files.length },
    });
    setProgress(`uploading ${files.length} files…`);
    await api.uploadFiles(cap.id, files);
    setProgress("finalizing…");
    await api.finalize(cap.id);
    startTransition(() => router.push(`/captures/${cap.id}`));
  }

  return (
    <div
      onDragOver={(e) => {
        e.preventDefault();
        setOver(true);
      }}
      onDragLeave={() => setOver(false)}
      onDrop={onDrop}
      className={clsx(
        "border border-dashed border-rule p-10 text-center transition-colors",
        over && "border-accent bg-rule/40",
        pending && "opacity-60 pointer-events-none",
      )}
    >
      <p className="text-muted text-sm">
        drop a folder of images here, or
      </p>
      <label className="inline-block mt-2 px-3 py-1 text-sm border border-rule cursor-pointer hover:bg-rule/30">
        pick files
        <input
          type="file"
          multiple
          accept="image/*"
          className="hidden"
          onChange={onPick}
        />
      </label>
      {progress && <p className="text-xs text-accent mt-3">{progress}</p>}
    </div>
  );
}

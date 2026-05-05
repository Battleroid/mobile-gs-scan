"use client";
import { useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import clsx from "clsx";
import { api } from "@/lib/api";

interface Props {
  /** User-typed capture name. Empty string = let the server pick a
   *  memorable random name (api.createCapture sees it as undefined). */
  name?: string;
}

// Mirrors the Android client's three preset training-iter values.
// The custom path is open-ended within the worker's accepted range;
// keep the upper bound generous (200k) but high enough that anyone
// who really needs more is probably misusing splatfacto.
const TRAIN_ITERS_PRESETS = [5_000, 15_000, 30_000] as const;
type TrainIterPreset = (typeof TRAIN_ITERS_PRESETS)[number];
const TRAIN_ITERS_MIN = 100;
const TRAIN_ITERS_MAX = 200_000;
const TRAIN_ITERS_DEFAULT: TrainIterPreset = 15_000;

const VIDEO_EXTS = [".mp4", ".mov", ".webm", ".mkv"];

const EXTRACT_FPS_DEFAULT = 8;
const JPEG_QUALITY_DEFAULT = 90;

type TrainIters =
  | { kind: "preset"; value: TrainIterPreset }
  | { kind: "custom"; value: number };

type DropKind = "images" | "video";

interface VideoMeta {
  /** Detected nominal fps from the browser's video element. 0 when
   *  the metadata didn't expose enough to compute it; in that case
   *  the UI doesn't show the source-fps ceiling and the server
   *  clamps for us. */
  fps: number;
}

export function UploadDropzone({ name }: Props) {
  const router = useRouter();
  const [over, setOver] = useState(false);
  const [pending, startTransition] = useTransition();
  const [progress, setProgress] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [trainIters, setTrainIters] = useState<TrainIters>({
    kind: "preset",
    value: TRAIN_ITERS_DEFAULT,
  });
  const [staged, setStaged] = useState<{
    files: File[];
    kind: DropKind;
    video?: VideoMeta;
  } | null>(null);
  const [extractFps, setExtractFps] = useState<number>(EXTRACT_FPS_DEFAULT);
  const [jpegQuality, setJpegQuality] = useState<number>(JPEG_QUALITY_DEFAULT);

  function onDrop(e: React.DragEvent) {
    e.preventDefault();
    setOver(false);
    void stage(Array.from(e.dataTransfer.files));
  }

  async function onPick(e: React.ChangeEvent<HTMLInputElement>) {
    await stage(Array.from(e.target.files ?? []));
  }

  async function stage(files: File[]) {
    if (!files.length) return;
    setError(null);
    const kinds = files.map(classify);
    if (kinds.some((k) => k === "image") && kinds.some((k) => k === "video")) {
      setError("drop either image files or one video — not both.");
      return;
    }
    if (kinds.filter((k) => k === "video").length > 1) {
      setError("only one video per capture is supported.");
      return;
    }
    if (kinds.every((k) => k === "image")) {
      setStaged({ files, kind: "images" });
      return;
    }
    // Single-video path: probe metadata for a source-fps ceiling.
    const video = files.find((_, i) => kinds[i] === "video")!;
    const meta = await probeVideo(video);
    setStaged({ files: [video], kind: "video", video: meta });
    if (meta.fps > 0) {
      // Clamp the requested fps to the source's nominal fps so the
      // form doesn't show a ceiling-busting value. Server clamps
      // again from its own ffprobe pass.
      setExtractFps((cur) => Math.min(cur, Math.floor(meta.fps)));
    }
  }

  async function submit() {
    if (!staged) return;
    setError(null);
    const iters = clampTrainIters(trainIters);
    // Prefer the user-supplied capture name when present. Fall back
    // to the dropped folder's name (webkitRelativePath's first
    // segment, when the browser populates it for folder picks);
    // empty string lets the server auto-generate.
    const captureName =
      (name ?? "").trim() ||
      staged.files[0]?.webkitRelativePath?.split("/")[0] ||
      undefined;
    const meta: Record<string, unknown> =
      staged.kind === "images"
        ? {
            source_kind: "images",
            count: staged.files.length,
            train_iters: iters,
          }
        : {
            source_kind: "video",
            train_iters: iters,
            extract_fps: extractFps,
            jpeg_quality: jpegQuality,
          };
    setProgress(
      staged.kind === "images"
        ? `creating capture for ${staged.files.length} files…`
        : "creating capture for video…",
    );
    try {
      const cap = await api.createCapture({
        name: captureName,
        source: "upload",
        meta,
      });
      setProgress(
        staged.kind === "images"
          ? `uploading ${staged.files.length} files…`
          : `uploading ${staged.files[0].name}…`,
      );
      await api.uploadFiles(cap.id, staged.files);
      setProgress("finalizing…");
      await api.finalize(cap.id);
      startTransition(() => router.push(`/captures/${cap.id}`));
    } catch (e) {
      setError((e as Error).message);
      setProgress(null);
    }
  }

  return (
    <div className="space-y-3">
      <TrainItersControl value={trainIters} onChange={setTrainIters} />
      {staged?.kind === "video" && (
        <VideoControls
          fps={extractFps}
          fpsCeiling={Math.floor(staged.video?.fps ?? 0)}
          onFpsChange={setExtractFps}
          quality={jpegQuality}
          onQualityChange={setJpegQuality}
        />
      )}
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
        {staged ? (
          <p className="text-sm">
            {staged.kind === "images"
              ? `staged: ${staged.files.length} image${staged.files.length === 1 ? "" : "s"}`
              : `staged: ${staged.files[0].name}`}
            {" · "}
            <button
              type="button"
              onClick={() => setStaged(null)}
              className="underline text-muted hover:text-fg"
            >
              clear
            </button>
          </p>
        ) : (
          <p className="text-muted text-sm">
            drop a folder of images or a single video here, or
          </p>
        )}
        {!staged && (
          <label className="inline-block mt-2 px-3 py-1 text-sm border border-rule cursor-pointer hover:bg-rule/30">
            pick files
            <input
              type="file"
              multiple
              accept={"image/*," + VIDEO_EXTS.join(",")}
              className="hidden"
              onChange={onPick}
            />
          </label>
        )}
        {staged && (
          <button
            type="button"
            onClick={submit}
            disabled={pending}
            className="mt-3 px-4 py-2 border border-rule hover:bg-rule/30 disabled:opacity-60"
          >
            {pending ? "uploading…" : "start"}
          </button>
        )}
        {progress && <p className="text-xs text-accent mt-3">{progress}</p>}
        {error && <p className="text-xs text-danger mt-3">{error}</p>}
      </div>
    </div>
  );
}

function TrainItersControl({
  value,
  onChange,
}: {
  value: TrainIters;
  onChange: (v: TrainIters) => void;
}) {
  return (
    <fieldset className="space-y-2 text-sm">
      <legend className="text-xs text-muted uppercase tracking-wide">
        training iterations
      </legend>
      <div className="flex flex-wrap items-center gap-3">
        {TRAIN_ITERS_PRESETS.map((p) => (
          <label key={p} className="flex items-center gap-1 cursor-pointer">
            <input
              type="radio"
              checked={value.kind === "preset" && value.value === p}
              onChange={() => onChange({ kind: "preset", value: p })}
              className="accent-accent"
            />
            {presetLabel(p)} ({p.toLocaleString()})
          </label>
        ))}
        <label className="flex items-center gap-1 cursor-pointer">
          <input
            type="radio"
            checked={value.kind === "custom"}
            onChange={() =>
              onChange({
                kind: "custom",
                value:
                  value.kind === "custom" ? value.value : TRAIN_ITERS_DEFAULT,
              })
            }
            className="accent-accent"
          />
          custom
        </label>
        {value.kind === "custom" && (
          <input
            type="number"
            min={TRAIN_ITERS_MIN}
            max={TRAIN_ITERS_MAX}
            step={1_000}
            value={value.value}
            onChange={(e) => {
              const n = parseInt(e.target.value, 10);
              if (Number.isFinite(n)) {
                onChange({ kind: "custom", value: n });
              }
            }}
            className="w-28 bg-transparent border-b border-rule px-1 focus:outline-none focus:border-accent"
          />
        )}
      </div>
    </fieldset>
  );
}

function VideoControls({
  fps,
  fpsCeiling,
  onFpsChange,
  quality,
  onQualityChange,
}: {
  fps: number;
  fpsCeiling: number;
  onFpsChange: (n: number) => void;
  quality: number;
  onQualityChange: (n: number) => void;
}) {
  return (
    <fieldset className="space-y-3 text-sm border border-rule p-3">
      <legend className="text-xs text-muted uppercase tracking-wide px-1">
        video extraction
      </legend>
      <label className="flex items-center gap-2">
        <span className="w-32">extract fps</span>
        <input
          type="number"
          min={1}
          max={fpsCeiling > 0 ? fpsCeiling : 60}
          step={1}
          value={fps}
          onChange={(e) => {
            const n = parseInt(e.target.value, 10);
            if (Number.isFinite(n) && n > 0) {
              onFpsChange(
                fpsCeiling > 0 ? Math.min(n, fpsCeiling) : n,
              );
            }
          }}
          className="w-20 bg-transparent border-b border-rule px-1 focus:outline-none focus:border-accent"
        />
        <span className="text-xs text-muted">
          {fpsCeiling > 0
            ? `(source: ${fpsCeiling} fps; cannot exceed)`
            : "(source fps unknown — server clamps)"}
        </span>
      </label>
      <label className="flex items-center gap-2">
        <span className="w-32">jpeg quality</span>
        <input
          type="range"
          min={1}
          max={100}
          step={1}
          value={quality}
          onChange={(e) => onQualityChange(parseInt(e.target.value, 10))}
          className="w-40 accent-accent"
        />
        <span className="text-xs text-muted">{quality}</span>
      </label>
    </fieldset>
  );
}

function classify(f: File): "image" | "video" | "other" {
  if (f.type.startsWith("image/")) return "image";
  if (f.type.startsWith("video/")) return "video";
  // Some browsers don't populate file.type for less-common video
  // extensions; fall back to extension match.
  const lower = (f.name || "").toLowerCase();
  if (VIDEO_EXTS.some((ext) => lower.endsWith(ext))) return "video";
  return "other";
}

async function probeVideo(file: File): Promise<VideoMeta> {
  return new Promise((resolve) => {
    const url = URL.createObjectURL(file);
    const v = document.createElement("video");
    v.preload = "metadata";
    v.muted = true;
    v.src = url;
    const cleanup = () => {
      URL.revokeObjectURL(url);
    };
    // The HTML5 spec doesn't expose nominal fps directly. We
    // use ``requestVideoFrameCallback`` (where supported) to time
    // a few frames and infer fps; otherwise fall back to fps=0
    // (server clamps from its ffprobe pass).
    const tryRvfc = () => {
      const anyV = v as unknown as {
        requestVideoFrameCallback?: (
          cb: (ts: number, info: { mediaTime: number }) => void,
        ) => number;
      };
      if (typeof anyV.requestVideoFrameCallback !== "function") {
        cleanup();
        resolve({ fps: 0 });
        return;
      }
      const samples: number[] = [];
      const tick = (_ts: number, info: { mediaTime: number }) => {
        samples.push(info.mediaTime);
        if (samples.length >= 6) {
          const deltas: number[] = [];
          for (let i = 1; i < samples.length; i++) {
            const d = samples[i] - samples[i - 1];
            if (d > 0) deltas.push(d);
          }
          const avg = deltas.length
            ? deltas.reduce((a, b) => a + b, 0) / deltas.length
            : 0;
          const fps = avg > 0 ? Math.round(1 / avg) : 0;
          cleanup();
          v.pause();
          resolve({ fps });
          return;
        }
        anyV.requestVideoFrameCallback!(tick);
      };
      v.addEventListener(
        "loadeddata",
        () => {
          v.play().catch(() => {});
          anyV.requestVideoFrameCallback!(tick);
        },
        { once: true },
      );
      v.addEventListener(
        "error",
        () => {
          cleanup();
          resolve({ fps: 0 });
        },
        { once: true },
      );
    };
    tryRvfc();
  });
}

function presetLabel(p: TrainIterPreset): string {
  if (p === 5_000) return "low";
  if (p === 15_000) return "standard";
  return "high";
}

function clampTrainIters(t: TrainIters): number {
  if (t.kind === "preset") return t.value;
  const n = Math.round(t.value);
  if (!Number.isFinite(n)) return TRAIN_ITERS_DEFAULT;
  return Math.max(TRAIN_ITERS_MIN, Math.min(TRAIN_ITERS_MAX, n));
}

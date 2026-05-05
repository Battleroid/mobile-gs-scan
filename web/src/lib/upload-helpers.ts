/**
 * Pure helpers extracted from the original UploadDropzone so the
 * `/captures/new` page can own the state machine inline without
 * dragging classification + video metadata code into a JSX file.
 */

export const VIDEO_EXTS = [".mp4", ".mov", ".webm", ".mkv"];
export const TRAIN_ITERS_PRESETS = [5_000, 15_000, 30_000] as const;
export type TrainIterPreset = (typeof TRAIN_ITERS_PRESETS)[number];
export const TRAIN_ITERS_MIN = 100;
export const TRAIN_ITERS_MAX = 200_000;
export const TRAIN_ITERS_DEFAULT: TrainIterPreset = 15_000;

export const EXTRACT_FPS_DEFAULT = 8;
export const JPEG_QUALITY_DEFAULT = 90;

export type DropKind = "images" | "video";

export interface VideoMeta {
  /** Detected nominal fps from the browser's video element. 0 when
   *  the metadata didn't expose enough to compute it; in that case
   *  the UI doesn't show the source-fps ceiling and the server
   *  clamps for us. */
  fps: number;
}

export type TrainIters =
  | { kind: "preset"; value: TrainIterPreset }
  | { kind: "custom"; value: number };

export function classify(f: File): "image" | "video" | "other" {
  if (f.type.startsWith("image/")) return "image";
  if (f.type.startsWith("video/")) return "video";
  // Some browsers don't populate file.type for less-common video
  // extensions; fall back to extension match.
  const lower = (f.name || "").toLowerCase();
  if (VIDEO_EXTS.some((ext) => lower.endsWith(ext))) return "video";
  return "other";
}

export function clampTrainIters(t: TrainIters): number {
  if (t.kind === "preset") return t.value;
  const n = Math.round(t.value);
  if (!Number.isFinite(n)) return TRAIN_ITERS_DEFAULT;
  return Math.max(TRAIN_ITERS_MIN, Math.min(TRAIN_ITERS_MAX, n));
}

export function probeVideo(file: File): Promise<VideoMeta> {
  return new Promise((resolve) => {
    const url = URL.createObjectURL(file);
    const v = document.createElement("video");
    v.preload = "metadata";
    v.muted = true;
    v.src = url;
    const cleanup = () => URL.revokeObjectURL(url);
    // The HTML5 spec doesn't expose nominal fps directly. We
    // use ``requestVideoFrameCallback`` (where supported) to time
    // a few frames and infer fps; otherwise fall back to fps=0
    // (server clamps from its ffprobe pass).
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
  });
}

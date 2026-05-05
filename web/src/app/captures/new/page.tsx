"use client";
import { useRef, useState, useTransition } from "react";
import { useRouter } from "next/navigation";
import clsx from "clsx";
import { api } from "@/lib/api";
import {
  BigButton,
  DisplayHeading,
  Eyebrow,
  Panel,
} from "@/components/pebble";
import {
  classify,
  clampTrainIters,
  EXTRACT_FPS_DEFAULT,
  JPEG_QUALITY_DEFAULT,
  probeVideo,
  TRAIN_ITERS_DEFAULT,
  TRAIN_ITERS_MAX,
  TRAIN_ITERS_MIN,
  TRAIN_ITERS_PRESETS,
  type TrainIterPreset,
  type TrainIters,
  type VideoMeta,
  VIDEO_EXTS,
} from "@/lib/upload-helpers";

type Staged = { files: File[]; kind: "images" | "video"; video?: VideoMeta };

const FIDELITY_STOPS: {
  value: TrainIterPreset;
  label: string;
  sub: string;
}[] = [
  { value: 5_000, label: "Low", sub: "5k · ~3 min" },
  { value: 15_000, label: "Standard", sub: "15k · ~10 min" },
  { value: 30_000, label: "High", sub: "30k · ~25 min" },
];

/**
 * `/captures/new` rebuilt to match StudioWebNew. Owns the full
 * upload state machine inline (file classification, video probe,
 * train-iters preset, create → upload → finalize chain) so the
 * design's three-column layout can wire form values directly without
 * a separate state holder. Copy + structure mirror studio.jsx lines
 * 244–311.
 */
export default function NewCapturePage() {
  const router = useRouter();
  // Empty by default so the label's promise ("or we'll pick one")
  // actually works — submit() falls back to the dropped folder name
  // first, then to undefined (server auto-generates a memorable
  // random name). Pre-filling here was a literal lift from the
  // design mock; in a real flow it would override every capture
  // with the same string.
  const [name, setName] = useState("");
  const [staged, setStaged] = useState<Staged | null>(null);
  const [trainIters, setTrainIters] = useState<TrainIters>({
    kind: "preset",
    value: TRAIN_ITERS_DEFAULT,
  });
  const [extractFps, setExtractFps] = useState<number>(EXTRACT_FPS_DEFAULT);
  const [jpegQuality, setJpegQuality] = useState<number>(JPEG_QUALITY_DEFAULT);
  const [over, setOver] = useState(false);
  const [pending, startTransition] = useTransition();
  const [submitting, setSubmitting] = useState(false);
  const [progress, setProgress] = useState<string | null>(null);
  const [error, setError] = useState<string | null>(null);

  async function stage(files: File[]) {
    if (!files.length) return;
    setError(null);
    const classified = files
      .map((f) => ({ file: f, kind: classify(f) }))
      .filter((c) => c.kind !== "other");
    if (classified.length === 0) {
      setError("nothing to upload — drop image files or a single video.");
      return;
    }
    const images = classified
      .filter((c) => c.kind === "image")
      .map((c) => c.file);
    const videos = classified
      .filter((c) => c.kind === "video")
      .map((c) => c.file);
    if (images.length > 0 && videos.length > 0) {
      setError("drop either image files or one video — not both.");
      return;
    }
    if (videos.length > 1) {
      setError("only one video per capture is supported.");
      return;
    }
    if (videos.length === 0) {
      setStaged({ files: images, kind: "images" });
      // Prefill the name from the dropped folder when blank, so the
      // name field reflects what we'd send if the user hits start.
      const folder = images[0]?.webkitRelativePath?.split("/")[0];
      if (folder && !name.trim()) setName(folder);
      return;
    }
    const video = videos[0];
    const meta = await probeVideo(video);
    setStaged({ files: [video], kind: "video", video: meta });
    if (meta.fps > 0) {
      setExtractFps((cur) => Math.min(cur, Math.floor(meta.fps)));
    }
  }

  async function submit() {
    if (!staged || submitting) return;
    setSubmitting(true);
    setError(null);
    const iters = clampTrainIters(trainIters);
    const captureName =
      name.trim() ||
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
      setSubmitting(false);
    }
  }

  const fidelityIdx = Math.max(
    0,
    FIDELITY_STOPS.findIndex(
      (s) => trainIters.kind === "preset" && s.value === trainIters.value,
    ),
  );
  const fidelityActive = FIDELITY_STOPS[fidelityIdx] ?? FIDELITY_STOPS[1];

  return (
    <div className="flex h-[calc(100vh-72px)] flex-col">
      {/* Body — main column + status / tip aside. Scrolls
       *  internally so the action footer stays anchored. The flex
       *  pair sits inside a centered max-width wrapper so the form
       *  isn't pinned to the left edge on wide displays. */}
      <div className="flex flex-1 justify-center overflow-auto px-9 pb-6 pt-9">
        <div className="flex w-full max-w-[1100px] gap-7">
        <div className="w-full max-w-[720px] flex-1">
          <Eyebrow>step 1 / 3</Eyebrow>
          <DisplayHeading className="mb-2 mt-2">
            Start a new scan.
          </DisplayHeading>
          <p className="m-0 mb-7 max-w-[540px] text-[15px] leading-[1.55] text-inkSoft">
            Drop a folder of frames or a video file. We&apos;ll run SfM, then
            train. It&apos;ll land on your shelf in a few minutes.
          </p>

          <Eyebrow className="mb-[6px]">name (optional)</Eyebrow>
          <div className="mb-7 flex items-center gap-3 rounded-md border border-rule bg-surface px-[18px] py-[14px]">
            <span className="font-mono text-[13px] text-muted">›</span>
            <input
              value={name}
              onChange={(e) => setName(e.target.value)}
              placeholder="oak side chair"
              className="flex-1 border-none bg-transparent text-[16px] font-medium text-fg outline-none placeholder:text-muted"
            />
            <span className="font-mono text-[11px] text-muted">
              or we&apos;ll pick one
            </span>
          </div>

          <DropZone
            staged={staged}
            over={over}
            disabled={submitting || pending}
            onDrop={(e) => {
              e.preventDefault();
              setOver(false);
              void stage(Array.from(e.dataTransfer.files));
            }}
            onDragOver={(e) => {
              e.preventDefault();
              setOver(true);
            }}
            onDragLeave={() => setOver(false)}
            onPick={(files) => void stage(files)}
            onClear={() => setStaged(null)}
          />

          {staged?.kind === "video" && (
            <VideoControls
              fps={extractFps}
              fpsCeiling={Math.floor(staged.video?.fps ?? 0)}
              onFpsChange={setExtractFps}
              quality={jpegQuality}
              onQualityChange={setJpegQuality}
            />
          )}

          <FidelityCard
            active={fidelityIdx}
            label={fidelityActive.label}
            sub={fidelityActive.sub}
            onChange={(idx) =>
              setTrainIters({
                kind: "preset",
                value: FIDELITY_STOPS[idx].value,
              })
            }
            customOpen={trainIters.kind === "custom"}
            customValue={
              trainIters.kind === "custom" ? trainIters.value : TRAIN_ITERS_DEFAULT
            }
            onCustomToggle={() =>
              setTrainIters((t) =>
                t.kind === "custom"
                  ? { kind: "preset", value: TRAIN_ITERS_DEFAULT }
                  : {
                      kind: "custom",
                      value: TRAIN_ITERS_DEFAULT,
                    },
              )
            }
            onCustomChange={(n) =>
              setTrainIters({ kind: "custom", value: n })
            }
          />
        </div>

        <aside className="hidden w-[320px] flex-shrink-0 lg:block">
          <Panel eyebrow="" title="" className="!p-5">
            <Eyebrow className="mb-[10px] !text-[10px] !tracking-[0.08em]">
              Studio status
            </Eyebrow>
            <div className="mb-[14px] flex items-center gap-[10px]">
              <span className="h-[10px] w-[10px] rounded-full bg-accent3 shadow-[0_0_0_4px_rgba(34,197,138,0.16)]" />
              <span className="text-[14px] font-semibold">
                Worker idle · ready
              </span>
            </div>
            <div className="space-y-[2px] font-mono text-[11px] leading-[1.7] text-inkSoft">
              <div className="flex justify-between">
                <span>gpu</span>
                <span>RTX 4090 · 22% mem</span>
              </div>
              <div className="flex justify-between">
                <span>queue</span>
                <span>0 jobs</span>
              </div>
              <div className="flex justify-between">
                <span>disk</span>
                <span>184 GB free</span>
              </div>
            </div>
          </Panel>

          <div className="mt-4 rounded-lg bg-chip4 p-5">
            <Eyebrow className="mb-2 !text-[10px] !tracking-[0.08em] !text-inkSoft">
              tip
            </Eyebrow>
            <p className="m-0 text-[13px] leading-[1.55]">
              Walk a slow circle around the subject. Aim for ~30s of footage
              and avoid changing exposure mid-loop.
            </p>
          </div>

          {progress && (
            <p className="mt-4 font-mono text-[11px] text-accent">{progress}</p>
          )}
          {error && (
            <p className="mt-4 font-mono text-[11px] text-danger">{error}</p>
          )}
        </aside>
        </div>
      </div>

      {/* Footer — sticky action row. Save-as-draft is decorative for
       *  now (no draft store on the web); the primary CTA fires the
       *  same submit() chain UploadDropzone used to. */}
      <div className="flex items-center justify-between border-t border-rule bg-surface px-9 py-4">
        <span className="font-mono text-[11px] text-muted">
          esc to cancel · ⌘↵ to start
        </span>
        <div className="flex gap-2">
          <BigButton
            variant="secondary"
            onClick={() => router.push("/")}
            disabled={submitting || pending}
          >
            Save as draft
          </BigButton>
          <BigButton
            onClick={submit}
            disabled={!staged || submitting || pending}
          >
            {submitting || pending ? "Uploading…" : "Start scanning →"}
          </BigButton>
        </div>
      </div>
    </div>
  );
}

function DropZone({
  staged,
  over,
  disabled,
  onDrop,
  onDragOver,
  onDragLeave,
  onPick,
  onClear,
}: {
  staged: Staged | null;
  over: boolean;
  disabled: boolean;
  onDrop: (e: React.DragEvent) => void;
  onDragOver: (e: React.DragEvent) => void;
  onDragLeave: () => void;
  onPick: (files: File[]) => void;
  onClear: () => void;
}) {
  const inputRef = useRef<HTMLInputElement | null>(null);
  return (
    <div className="mb-7">
      <div
        onDrop={onDrop}
        onDragOver={onDragOver}
        onDragLeave={onDragLeave}
        className={clsx(
          "relative flex items-center gap-5 rounded-lg border-[2px] border-dashed bg-surface px-6 py-8 transition-colors",
          over ? "border-accent" : "border-ruleStrong",
          disabled && "pointer-events-none opacity-60",
        )}
      >
        <div className="flex h-14 w-14 flex-shrink-0 items-center justify-center rounded-md bg-chip1 text-[26px]">
          📁
        </div>
        <div className="flex-1">
          {staged ? (
            <>
              <div className="text-[17px] font-semibold">
                {staged.kind === "images"
                  ? `staged: ${staged.files.length} image${staged.files.length === 1 ? "" : "s"}`
                  : `staged: ${staged.files[0].name}`}
              </div>
              <div className="text-[13px] text-inkSoft">
                Looks good. Hit{" "}
                <span className="font-semibold">Start scanning</span> to kick
                off training.
              </div>
              <button
                type="button"
                onClick={onClear}
                className="mt-3 font-mono text-[11px] text-muted underline hover:text-fg"
              >
                clear
              </button>
            </>
          ) : (
            <>
              <div className="text-[17px] font-semibold">Drop frames here</div>
              <div className="text-[13px] text-inkSoft">
                Folder of stills · video file (.mp4 / .mov / .webm / .mkv)
              </div>
              {/* Format chips advertise only what the picker `accept`
               *  + classify() actually ingest. The design mock also
               *  showed a zip chip; the worker doesn't unpack archives
               *  yet so we keep that out until pipeline support lands. */}
              <div className="mt-3 flex flex-wrap gap-[6px] font-mono text-[11px] text-inkSoft">
                <FormatChip tone="chip3">jpg</FormatChip>
                <FormatChip tone="chip3">png</FormatChip>
                <FormatChip tone="chip2">mp4</FormatChip>
                <FormatChip tone="chip2">mov</FormatChip>
              </div>
            </>
          )}
        </div>
        {!staged && (
          <button
            type="button"
            onClick={() => inputRef.current?.click()}
            className="flex-shrink-0 rounded-md bg-accent px-4 py-[10px] text-[13px] font-semibold text-white"
          >
            Browse…
          </button>
        )}
        <input
          ref={inputRef}
          type="file"
          multiple
          accept={"image/*," + VIDEO_EXTS.join(",")}
          className="hidden"
          onChange={(e) => onPick(Array.from(e.target.files ?? []))}
        />
      </div>
      <div className="mt-[10px] flex items-center gap-2 font-mono text-[11px] text-muted">
        <span>Recording on your phone instead?</span>
        <span className="font-semibold text-accent">
          captures from the app appear automatically →
        </span>
      </div>
    </div>
  );
}

function FormatChip({
  tone,
  children,
}: {
  tone: "chip2" | "chip3";
  children: React.ReactNode;
}) {
  const bg = { chip2: "bg-chip2", chip3: "bg-chip3" }[tone];
  return (
    <span className={`rounded-xs px-2 py-[3px] ${bg}`}>{children}</span>
  );
}

function FidelityCard({
  active,
  label,
  sub,
  onChange,
  customOpen,
  customValue,
  onCustomToggle,
  onCustomChange,
}: {
  active: number;
  label: string;
  sub: string;
  onChange: (idx: number) => void;
  customOpen: boolean;
  customValue: number;
  onCustomToggle: () => void;
  onCustomChange: (n: number) => void;
}) {
  return (
    <div className="rounded-lg border border-rule bg-surface p-6">
      <div className="mb-4 flex items-start justify-between gap-4">
        <div>
          <Eyebrow className="mb-1 !text-[10px] !tracking-[0.08em]">
            Training fidelity
          </Eyebrow>
          <div className="text-[22px] font-bold tracking-[-0.02em]">
            {customOpen ? `Custom · ${customValue.toLocaleString()} iter` : `${label} · ${sub.split(" · ")[0]} iter`}
          </div>
        </div>
        <div className="rounded-sm bg-chip4 px-[10px] py-[6px] font-mono text-[12px] text-inkSoft">
          {customOpen ? "≈ depends on iter count" : `${sub.split(" · ")[1] ?? ""} on RTX 4090`}
        </div>
      </div>

      {!customOpen && (
        <FidelityScale active={active} onChange={onChange} />
      )}
      {customOpen && (
        <div className="mt-2 flex items-center gap-3">
          <input
            type="number"
            min={TRAIN_ITERS_MIN}
            max={TRAIN_ITERS_MAX}
            step={1_000}
            value={customValue}
            onChange={(e) => {
              const n = parseInt(e.target.value, 10);
              if (Number.isFinite(n)) onCustomChange(n);
            }}
            className="w-32 rounded-sm border border-rule bg-bg px-3 py-2 font-mono text-sm text-fg focus:border-accent focus:outline-none"
          />
          <span className="font-mono text-[11px] text-inkSoft">
            iterations · {TRAIN_ITERS_MIN.toLocaleString()}–
            {TRAIN_ITERS_MAX.toLocaleString()}
          </span>
        </div>
      )}

      <button
        type="button"
        onClick={onCustomToggle}
        className="mt-4 font-mono text-[11px] text-muted underline hover:text-fg"
      >
        {customOpen ? "use a preset" : "custom iter count"}
      </button>
    </div>
  );
}

function FidelityScale({
  active,
  onChange,
}: {
  active: number;
  onChange: (idx: number) => void;
}) {
  // Visual stack with a hidden native range input layered on top.
  // The native input owns drag + keyboard accessibility for free
  // (pointer capture, ←/→ keys, screen-reader); we just paint the
  // dots and let the input dispatch the integer onChange. The
  // wrapper's relative position scopes the dots so they don't drift
  // out of the track when the slider stretches.
  return (
    <div className="select-none">
      <div className="relative mx-3 mb-3 mt-5 h-1 rounded-full bg-rule">
        {/* Filled portion behind the active dot. */}
        <div
          className="absolute left-0 top-0 h-full rounded-full bg-accent"
          style={{ width: `${active * 50}%` }}
        />
        {/* Three visual stops — purely cosmetic. The clickable hit
         *  area is the wider native input below. */}
        {[0, 1, 2].map((i) => (
          <span
            key={i}
            aria-hidden
            className={clsx(
              "pointer-events-none absolute top-1/2 -translate-x-1/2 -translate-y-1/2 rounded-full transition-all",
              i === active
                ? "h-[22px] w-[22px] border-[4px] border-white bg-accent shadow-[0_0_0_1px_var(--accent),0_4px_10px_rgba(255,90,54,0.3)]"
                : i < active
                  ? "h-3 w-3 border-2 border-rule bg-accent"
                  : "h-3 w-3 border-2 border-rule bg-surface",
            )}
            style={{ left: `${(i / 2) * 100}%` }}
          />
        ))}
        {/* Native range input layered transparently — owns the
         *  drag + keyboard interaction. step=1 snaps to the three
         *  preset stops; opacity-0 keeps it invisible while still
         *  receiving pointer events. The wider hit area lets users
         *  drag from anywhere on the track instead of having to
         *  land precisely on a 22-px dot. */}
        <input
          type="range"
          min={0}
          max={2}
          step={1}
          value={active}
          onChange={(e) => onChange(parseInt(e.target.value, 10))}
          aria-label={`Training fidelity: ${FIDELITY_STOPS[active]?.label ?? ""}`}
          className="absolute inset-x-0 top-1/2 z-10 h-7 w-full -translate-y-1/2 cursor-pointer appearance-none bg-transparent opacity-0"
        />
      </div>
      <div className="flex justify-between px-2">
        {FIDELITY_STOPS.map((stop, i) => (
          <button
            key={stop.value}
            type="button"
            onClick={() => onChange(i)}
            className={clsx(
              "flex-1 text-center transition-opacity",
              i === active ? "opacity-100" : "opacity-55",
            )}
          >
            <div className="text-[13px] font-semibold">{stop.label}</div>
            <div className="font-mono text-[10px] text-muted">{stop.sub}</div>
          </button>
        ))}
      </div>
    </div>
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
  // Used unchanged from the original UploadDropzone; reskinned to
  // match the surrounding cards. Only renders when a video is
  // staged so it doesn't clutter the image-set flow.
  return (
    <div className="mb-7 rounded-lg border border-rule bg-surface p-5">
      <Eyebrow className="mb-3 !text-[10px] !tracking-[0.08em]">
        video extraction
      </Eyebrow>
      <label className="mb-3 flex items-center gap-2 text-sm">
        <span className="w-32 text-inkSoft">extract fps</span>
        <input
          type="number"
          min={1}
          max={fpsCeiling > 0 ? fpsCeiling : 60}
          step={1}
          value={fps}
          onChange={(e) => {
            const n = parseInt(e.target.value, 10);
            if (Number.isFinite(n) && n > 0) {
              onFpsChange(fpsCeiling > 0 ? Math.min(n, fpsCeiling) : n);
            }
          }}
          className="w-20 rounded-sm border border-rule bg-bg px-2 py-1 font-mono text-sm focus:border-accent focus:outline-none"
        />
        <span className="font-mono text-[11px] text-muted">
          {fpsCeiling > 0
            ? `(source: ${fpsCeiling} fps; cannot exceed)`
            : "(source fps unknown — server clamps)"}
        </span>
      </label>
      <label className="flex items-center gap-2 text-sm">
        <span className="w-32 text-inkSoft">jpeg quality</span>
        <input
          type="range"
          min={1}
          max={100}
          step={1}
          value={quality}
          onChange={(e) => onQualityChange(parseInt(e.target.value, 10))}
          className="w-40 accent-accent"
        />
        <span className="font-mono text-[11px] text-muted">{quality}</span>
      </label>
    </div>
  );
}

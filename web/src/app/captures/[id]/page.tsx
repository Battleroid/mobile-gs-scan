"use client";
import { use, useRef, useState } from "react";
import Link from "next/link";
import { useRouter } from "next/navigation";
import { useQuery, useQueryClient } from "@tanstack/react-query";
import dynamic from "next/dynamic";
import clsx from "clsx";
import { api } from "@/lib/api";
import { useCaptureEvents } from "@/hooks/useCaptureEvents";
import { useSceneEvents } from "@/hooks/useSceneEvents";
import { JobLogPanel } from "@/components/JobLogPanel";
import { MeshPanel } from "@/components/MeshPanel";
import { SplatEditor, type SplatEditorHandle } from "@/components/SplatEditor";
import type { SelectionWidget } from "@/components/SplatViewer";
import type { Capture, Job, Scene } from "@/lib/types";
import {
  BigButton,
  DownloadMenu,
  Eyebrow,
  type DownloadOption,
} from "@/components/pebble";

// Heavy three.js viewer — keep it out of the SSR bundle.
const SplatViewer = dynamic(
  () => import("@/components/SplatViewer").then((m) => m.SplatViewer),
  { ssr: false },
);

interface PageProps {
  params: Promise<{ id: string }>;
}

/**
 * `/captures/[id]` rebuilt to match StudioWebDetail. Outer chrome
 * (breadcrumb, display title, action row, two-column grid) is new;
 * the inner SplatViewer / SplatEditor / MeshPanel / JobLogPanel
 * surfaces are preserved verbatim — they own real business logic
 * (training poll, edit recipe state, mesh trigger) that PR-A's plan
 * said to keep intact.
 */
export default function CaptureDetailPage({ params }: PageProps) {
  const { id } = use(params);
  const router = useRouter();

  const { data: initial } = useQuery({
    queryKey: ["capture", id],
    queryFn: () => api.getCapture(id),
  });
  const { capture: live } = useCaptureEvents(id);
  const capture = live ?? initial ?? null;

  const sceneId = capture?.scene_id ?? null;
  const { scene, editProgress, lastEditResult, meshProgress } =
    useSceneEvents(sceneId);

  const [deleting, setDeleting] = useState(false);
  // Which artifact the SplatViewer is showing. Auto-promotes to
  // "edited" when an edit lands and the user hasn't explicitly
  // overridden the choice; reverts to "original" on discard.
  const [viewing, setViewing] = useState<"original" | "edited">("original");
  // 3D widget state — null = no gizmo, "bbox"|"sphere" = render the
  // matching draggable wireframe in the viewer. Initial value is
  // snapshotted from the editor's current form state when activated.
  const editorRef = useRef<SplatEditorHandle | null>(null);
  const [activeWidget, setActiveWidget] = useState<
    "bbox" | "sphere" | null
  >(null);
  const [widgetSelection, setWidgetSelection] =
    useState<SelectionWidget | null>(null);
  const handleActivateWidget = (next: "bbox" | "sphere" | null) => {
    setActiveWidget(next);
    if (next === null) {
      setWidgetSelection(null);
      return;
    }
    setWidgetSelection(editorRef.current?.snapshotWidget(next) ?? null);
  };
  const handleWidgetCommit = (next: SelectionWidget) => {
    setWidgetSelection(next);
    editorRef.current?.commitWidget(next);
  };
  const hasEdit =
    !!scene && scene.edit_status === "completed" && !!scene.edited_ply_url;
  const [prevEditStatus, setPrevEditStatus] = useState(
    scene?.edit_status ?? null,
  );
  if (scene && prevEditStatus !== scene.edit_status) {
    setPrevEditStatus(scene.edit_status);
    if (scene.edit_status === "completed" && viewing === "original") {
      setViewing("edited");
    }
    if (scene.edit_status === "none") {
      setViewing("original");
    }
  }

  const onDelete = async () => {
    if (!capture) return;
    if (
      !window.confirm(
        "Delete this capture? All frames and pipeline artifacts will be removed. In-flight jobs will be canceled.",
      )
    ) {
      return;
    }
    setDeleting(true);
    try {
      const res = await api.deleteCapture(capture.id);
      if (!res.ok) throw new Error(`HTTP ${res.status}`);
      router.push("/");
    } catch (err) {
      window.alert(`delete failed: ${(err as Error).message}`);
      setDeleting(false);
    }
  };

  if (!capture) {
    return (
      <div className="mx-auto max-w-6xl px-9 py-12 text-muted">loading…</div>
    );
  }

  return (
    <div className="mx-auto w-full max-w-6xl px-9 pb-10 pt-6">
      {/* Breadcrumb */}
      <div className="flex items-center gap-3 font-mono text-[11px] text-muted">
        <Link href="/" className="hover:text-fg">
          ← captures
        </Link>
        <span className="h-1 w-1 rounded-full bg-muted" />
        <span>{capture.id.slice(0, 12)}</span>
      </div>

      {/* Title row */}
      <div className="mt-1 flex flex-col gap-4 pb-5 md:flex-row md:items-end md:justify-between">
        <div className="min-w-0 flex-1">
          <CaptureNameEditor capture={capture} />
          <MetaRow capture={capture} scene={scene} />
        </div>
        <div className="flex flex-wrap items-center gap-2">
          {scene && (
            <DownloadMenu {...buildDownloadOptions(scene, hasEdit)} />
          )}
          <BigButton
            variant="secondary"
            onClick={onDelete}
            disabled={deleting}
            className="!text-danger"
          >
            {deleting ? "Deleting…" : "Delete"}
          </BigButton>
        </div>
      </div>

      {/* Splat front and centre — full width up top so it stays the
       *  visual focus regardless of pipeline status. Pipeline + edit
       *  + mesh panels stack below. */}
      <ViewerPanel
        scene={scene}
        viewing={viewing}
        widgetSelection={widgetSelection}
        handleWidgetCommit={handleWidgetCommit}
        hasEdit={hasEdit}
      />

      <div className="mt-6">
        <PipelineCard capture={capture} scene={scene} />
      </div>

      {scene &&
        (scene.status === "queued" ||
          scene.status === "processing" ||
          scene.status === "uploading") && (
          // Tip is "while you wait" — only relevant while a scene
          // is actively in flight. Terminal states (failed,
          // canceled) get the failure banner below instead;
          // showing the tip in those states would directly
          // contradict the failure message and read as misleading
          // recovery guidance.
          <div className="mt-4">
            <TipCard scene={scene} />
          </div>
        )}

      {/* Editor + mesh — only when training is done. */}
      {scene && scene.status === "completed" && (
        <div className="mt-6 space-y-4">
          <SplatEditor
            ref={editorRef}
            scene={scene}
            editProgress={editProgress}
            lastEditResult={lastEditResult}
            viewing={viewing}
            onChangeView={setViewing}
            activeWidget={activeWidget}
            onActivateWidget={handleActivateWidget}
            onWidgetFormChange={setWidgetSelection}
          />
          <MeshPanel scene={scene} meshProgress={meshProgress} />
        </div>
      )}

      {scene?.status === "failed" && (
        <p className="mt-6 rounded-md border border-danger/30 bg-surface p-4 text-sm text-danger">
          pipeline failed: {scene.error ?? "unknown error"}
        </p>
      )}
    </div>
  );
}

// ─── Download menu builder ───────────────────────────────────────

/**
 * Compose primary + dropdown options for the action-row download
 * menu. When the scene has an edited variant the user is most likely
 * to want, the edited .ply leads; the original .ply slides into the
 * dropdown. Without an edit, the original .ply leads. Mesh artifacts
 * (.glb / .obj) and the .spz fast-loading variant always live in the
 * dropdown — pending entries when not yet ready.
 */
function buildDownloadOptions(
  scene: Scene,
  hasEdit: boolean,
): { primary: DownloadOption; options: DownloadOption[] } {
  const url = (rel: string | null) => (rel ? api.base() + rel : null);
  const editedPly: DownloadOption = {
    label: ".ply",
    sub: "edited",
    href: url(scene.edited_ply_url),
  };
  const editedSpz: DownloadOption = {
    label: ".spz",
    sub: "edited",
    href: url(scene.edited_spz_url),
  };
  const originalPly: DownloadOption = {
    label: ".ply",
    sub: "original",
    href: url(scene.ply_url),
  };
  const originalSpz: DownloadOption = {
    label: ".spz",
    sub: "original",
    href: url(scene.spz_url),
  };
  const meshGlb: DownloadOption = {
    label: ".glb",
    sub: "mesh",
    href: url(scene.mesh_glb_url),
  };
  const meshObj: DownloadOption = {
    label: ".obj",
    sub: "mesh",
    href: url(scene.mesh_obj_url),
  };

  if (hasEdit) {
    return {
      primary: editedPly,
      options: [editedSpz, originalPly, originalSpz, meshGlb, meshObj],
    };
  }
  return {
    primary: originalPly,
    options: [originalSpz, meshGlb, meshObj],
  };
}

// ─── Title meta row (mono pills) ─────────────────────────────────

function MetaRow({
  capture,
  scene,
}: {
  capture: Capture;
  scene: Scene | null;
}) {
  const status = scene?.status ?? capture.status;
  const dot =
    status === "completed"
      ? "bg-accent3"
      : status === "failed" || status === "canceled"
        ? "bg-danger"
        : "bg-accent";
  const startedAgo = relTime(capture.created_at);
  return (
    <div className="mt-2 flex flex-wrap gap-[14px] font-mono text-[12px] text-inkSoft">
      <span>{capture.source}</span>
      <span>{capture.frame_count} frames</span>
      {capture.dropped_count > 0 && (
        <span className="text-danger">{capture.dropped_count} dropped</span>
      )}
      {startedAgo && <span>started {startedAgo}</span>}
      <span className="inline-flex items-center gap-1">
        <span className={`h-[8px] w-[8px] rounded-full ${dot}`} />
        {status}
      </span>
    </div>
  );
}

// ─── Left column — viewer panel ──────────────────────────────────

function ViewerPanel({
  scene,
  viewing,
  widgetSelection,
  handleWidgetCommit,
  hasEdit: _hasEdit,
}: {
  scene: Scene | null;
  viewing: "original" | "edited";
  widgetSelection: SelectionWidget | null;
  handleWidgetCommit: (s: SelectionWidget) => void;
  hasEdit: boolean;
}) {
  // Mirror the original page's logic — pick the splat URL based on
  // edited vs original; remount the viewer when the URL changes so
  // Spark drops the prior buffer.
  if (!scene) {
    return (
      <div className="flex min-h-[560px] items-center justify-center rounded-lg border border-rule bg-surface text-muted">
        Waiting for scene…
      </div>
    );
  }
  if (scene.status !== "completed" || (!scene.ply_url && !scene.spz_url)) {
    return (
      <div className="relative flex min-h-[560px] items-center justify-center overflow-hidden rounded-lg border border-rule bg-gradient-to-br from-chip3 to-chip4 p-10 text-center">
        <div className="relative">
          <div className="font-mono text-[10px] uppercase tracking-[0.12em] text-inkSoft">
            preview
          </div>
          <div className="mt-2 text-[22px] font-bold tracking-[-0.02em]">
            Splat will appear here.
          </div>
          <p className="mx-auto mt-2 max-w-md text-[13px] text-inkSoft">
            We&apos;re training the model. The viewer lights up once
            splatfacto finishes — usually a few minutes.
          </p>
        </div>
      </div>
    );
  }
  const useEdited = viewing === "edited" && _hasEdit;
  const splatUrl = useEdited
    ? scene.edited_spz_url ?? scene.edited_ply_url!
    : scene.spz_url ?? scene.ply_url!;
  const pointsRel = useEdited
    ? scene.edited_ply_url ?? scene.ply_url
    : scene.ply_url;
  return (
    <div className="relative min-h-[560px] overflow-hidden rounded-lg border border-rule bg-surface">
      <SplatViewer
        key={splatUrl}
        url={api.base() + splatUrl}
        pointsUrl={pointsRel ? api.base() + pointsRel : undefined}
        meshGlbUrl={
          scene.mesh_glb_url ? api.base() + scene.mesh_glb_url : undefined
        }
        meshObjUrl={
          scene.mesh_obj_url ? api.base() + scene.mesh_obj_url : undefined
        }
        selection={widgetSelection}
        onSelectionCommit={handleWidgetCommit}
      />
    </div>
  );
}

// ─── Right column — pipeline / tip / exports ─────────────────────

function PipelineCard({
  capture: _capture,
  scene,
}: {
  capture: Capture;
  scene: Scene | null;
}) {
  const jobs = scene?.jobs ?? [];
  const completed = jobs.filter((j) => j.status === "completed").length;
  const total = jobs.length;
  return (
    <div className="rounded-lg border border-rule bg-surface p-5">
      <div className="mb-[14px] flex items-center justify-between">
        <Eyebrow className="!text-[10px] !tracking-[0.08em]">Pipeline</Eyebrow>
        <span className="font-mono text-[11px] text-accent">
          {total > 0 ? `${completed} / ${total}` : "—"}
        </span>
      </div>
      {jobs.length === 0 && (
        <p className="font-mono text-[11px] text-muted">
          waiting for the worker to pick this up…
        </p>
      )}
      {jobs.map((j) => (
        <PipelineJobRow key={j.id} job={j} />
      ))}
    </div>
  );
}

function PipelineJobRow({ job }: { job: Job }) {
  const cancelable =
    job.status === "queued" ||
    job.status === "claimed" ||
    job.status === "running";
  const [cancelling, setCancelling] = useState(false);
  const dot =
    job.status === "completed"
      ? "bg-accent3"
      : job.status === "running"
        ? "bg-accent"
        : job.status === "failed"
          ? "bg-danger"
          : "bg-muted";

  const onCancel = async () => {
    if (!window.confirm(`Cancel ${job.kind} job?`)) return;
    setCancelling(true);
    try {
      await api.cancelJob(job.id);
    } catch (err) {
      window.alert(`cancel failed: ${(err as Error).message}`);
    } finally {
      setCancelling(false);
    }
  };

  return (
    <div className="mb-[14px] last:mb-0">
      <div className="mb-[6px] flex items-baseline justify-between">
        <div className="flex items-center gap-2">
          <span
            className={clsx(
              "h-[8px] w-[8px] rounded-full",
              dot,
              job.status === "running" &&
                "shadow-[0_0_0_4px_rgba(255,90,54,0.18)]",
            )}
          />
          <span className="font-mono text-[13px] font-semibold">
            {job.kind}
          </span>
        </div>
        <div className="flex items-center gap-2">
          <span className="font-mono text-[11px] uppercase tracking-[0.05em] text-inkSoft">
            {job.status}
          </span>
          {cancelable && (
            <button
              type="button"
              onClick={onCancel}
              disabled={cancelling}
              className="font-mono text-[11px] text-danger underline hover:text-fg disabled:opacity-50"
            >
              {cancelling ? "…" : "cancel"}
            </button>
          )}
        </div>
      </div>
      <div className="h-[6px] overflow-hidden rounded-full bg-rule">
        <div
          className={clsx(
            "h-full transition-[width] duration-500",
            job.status === "completed" ? "bg-accent3" : "bg-accent",
          )}
          style={{ width: `${Math.round(job.progress * 100)}%` }}
        />
      </div>
      {job.progress_msg && (
        <p className="mt-1 font-mono text-[10px] text-muted">
          {job.progress_msg}
        </p>
      )}
      {job.error && (
        <p className="mt-1 font-mono text-[10px] text-danger">{job.error}</p>
      )}
      <JobLogPanel job={job} />
    </div>
  );
}

function TipCard({ scene }: { scene: Scene | null }) {
  const training = scene?.jobs.find((j) => j.kind === "train");
  if (!training) {
    return (
      <div className="rounded-lg bg-chip4 p-[18px]">
        <Eyebrow className="mb-2 !text-[10px] !tracking-[0.08em] !text-inkSoft">
          while you wait
        </Eyebrow>
        <p className="m-0 text-[13px] leading-[1.55]">
          We&apos;ll start training as soon as SfM lines up the cameras. You
          can close the tab — we&apos;ll keep going.
        </p>
      </div>
    );
  }
  return (
    <div className="rounded-lg bg-chip4 p-[18px]">
      <Eyebrow className="mb-2 !text-[10px] !tracking-[0.08em] !text-inkSoft">
        while you wait
      </Eyebrow>
      <p className="m-0 text-[13px] leading-[1.55]">
        Splatfacto is crunching this scene at{" "}
        <b>{Math.round(training.progress * 100)}%</b>. You can close the tab
        — we&apos;ll keep training and the result will be ready when you
        come back.
      </p>
    </div>
  );
}

// ─── Inline rename (preserved verbatim, only chrome reskinned) ───

function CaptureNameEditor({ capture }: { capture: Capture }) {
  const queryClient = useQueryClient();
  const [editing, setEditing] = useState(false);
  const [draft, setDraft] = useState(capture.name);
  const [saving, setSaving] = useState(false);
  const inputRef = useRef<HTMLInputElement>(null);

  const startEdit = () => {
    setDraft(capture.name);
    setEditing(true);
    queueMicrotask(() => {
      inputRef.current?.focus();
      inputRef.current?.select();
    });
  };

  const cancel = () => {
    setEditing(false);
    setDraft(capture.name);
  };

  const save = async () => {
    const trimmed = draft.trim();
    if (!trimmed || trimmed === capture.name) {
      cancel();
      return;
    }
    setSaving(true);
    try {
      const updated = await api.renameCapture(capture.id, trimmed);
      queryClient.setQueryData(["capture", capture.id], updated);
      setEditing(false);
    } catch (err) {
      window.alert(`rename failed: ${(err as Error).message}`);
    } finally {
      setSaving(false);
    }
  };

  if (editing) {
    return (
      <form
        onSubmit={(e) => {
          e.preventDefault();
          void save();
        }}
        className="flex items-center gap-2"
      >
        <input
          ref={inputRef}
          value={draft}
          onChange={(e) => setDraft(e.target.value)}
          onKeyDown={(e) => {
            if (e.key === "Escape") {
              e.preventDefault();
              cancel();
            }
          }}
          maxLength={200}
          disabled={saving}
          className="flex-1 border-b border-rule bg-transparent text-[44px] font-bold tracking-[-0.02em] focus:border-accent focus:outline-none disabled:opacity-50"
        />
        <button
          type="submit"
          disabled={saving}
          className="font-mono text-[11px] underline hover:text-fg disabled:opacity-50"
        >
          {saving ? "saving…" : "save"}
        </button>
        <button
          type="button"
          onClick={cancel}
          disabled={saving}
          className="font-mono text-[11px] text-muted underline hover:text-fg disabled:opacity-50"
        >
          cancel
        </button>
      </form>
    );
  }

  return (
    <div className="flex items-center gap-3">
      <h1 className="m-0 truncate text-[44px] font-bold leading-none tracking-[-0.02em]">
        {capture.name}
      </h1>
      <button
        type="button"
        onClick={startEdit}
        className="font-mono text-[11px] text-muted underline hover:text-fg"
      >
        rename
      </button>
    </div>
  );
}

// ─── Helpers ─────────────────────────────────────────────────────

function relTime(iso: string): string {
  const t = Date.parse(iso);
  if (!Number.isFinite(t)) return "";
  const diff = Date.now() - t;
  if (diff < 60_000) return "just now";
  if (diff < 3_600_000) return `${Math.round(diff / 60_000)} min ago`;
  if (diff < 86_400_000) return `${Math.round(diff / 3_600_000)}h ago`;
  return `${Math.round(diff / 86_400_000)}d ago`;
}

"""SQLAlchemy schema for captures, scenes, and jobs.

Single SQLite db at $DATA_DIR/studio.sqlite. Three tables:

  captures   one per drag-drop / video upload set.
  scenes     one per finalized capture, owns the gsplat artifacts.
  jobs       extract / sfm / train / export / mesh steps a worker claims.

Events (the in-memory pub/sub for live progress) are *not* persisted;
they're only useful to clients connected at the moment a step runs.

Datetime convention: every datetime in this codebase is a *naive*
UTC value. SQLite has no native TIMESTAMP-WITH-TIMEZONE support, and
SQLAlchemy's `DateTime(timezone=True)` emits a SAWarning + silently
strips the tzinfo on store on the SQLite dialect — so trying to
store tz-aware values gets you naive reads anyway. The simpler,
consistent move is to keep everything naive UTC end-to-end:
``_utcnow()`` returns ``datetime.utcnow()``-equivalent (the modern
spelling: ``datetime.now(timezone.utc).replace(tzinfo=None)``), and
every comparison site stays naive↔naive.
"""
from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    DateTime,
    Enum,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship


def _utcnow() -> datetime:
    # Naive UTC — see module docstring. ``datetime.utcnow()`` is
    # deprecated in 3.12+; this is the canonical replacement that
    # still produces a naive value.
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class CaptureStatus(str, enum.Enum):
    created = "created"          # session row exists, nothing else
    uploading = "uploading"      # drag-drop / video upload in progress
    queued = "queued"            # finalize done, waiting for worker
    processing = "processing"    # at least one job has started
    completed = "completed"      # all pipeline jobs ok, scene viewable
    failed = "failed"
    canceled = "canceled"


class CaptureSource(str, enum.Enum):
    # Single value today; kept as a one-value enum (rather than a
    # bool / dropped column) so future capture sources (Android-app
    # direct, drone-set, …) can land without a column-shape change.
    upload = "upload"


class JobStatus(str, enum.Enum):
    queued = "queued"
    claimed = "claimed"
    running = "running"
    completed = "completed"
    failed = "failed"
    canceled = "canceled"


class JobKind(str, enum.Enum):
    extract = "extract"  # ffmpeg video → frames; no-op for image-set uploads
    sfm = "sfm"
    train = "train"
    export = "export"
    mesh = "mesh"  # On-demand Poisson mesh extraction (Phase 3).
    filter = "filter"  # user-triggered post-processing of an existing splat
    # Render a single PNG thumbnail of the trained splat for the
    # web home grid. Runs after export. Failure is non-fatal —
    # the scene stays "completed" without a thumbnail and the
    # web CaptureCard falls back to a chip-tinted gradient.
    thumbnail = "thumbnail"


class EditStatus(str, enum.Enum):
    none = "none"           # no edit has been applied
    queued = "queued"       # filter job enqueued, worker hasn't picked up yet
    running = "running"     # worker is applying the recipe
    completed = "completed" # edit artifacts are on disk and downloadable
    failed = "failed"       # last apply attempt failed; see edit_error


class MeshStatus(str, enum.Enum):
    none = "none"           # no mesh has been extracted
    queued = "queued"       # mesh job enqueued, worker hasn't picked up yet
    running = "running"     # worker is running open3d poisson
    completed = "completed" # mesh artifacts are on disk and downloadable
    failed = "failed"       # last extraction attempt failed; see mesh_error


class Capture(Base):
    __tablename__ = "captures"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[CaptureStatus] = mapped_column(
        Enum(CaptureStatus, native_enum=False), default=CaptureStatus.created
    )
    source: Mapped[CaptureSource] = mapped_column(
        Enum(CaptureSource, native_enum=False),
        default=CaptureSource.upload,
        nullable=False,
    )

    # Tracked while frames are uploaded / extracted from a video.
    frame_count: Mapped[int] = mapped_column(Integer, default=0)
    dropped_count: Mapped[int] = mapped_column(Integer, default=0)
    has_pose: Mapped[bool] = mapped_column(default=False)

    # Free-form per-session config: device info, intrinsics, capture
    # bounding box, target frame rate, etc.
    meta: Mapped[dict] = mapped_column(JSON, default=dict)

    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow
    )

    scene: Mapped["Scene | None"] = relationship(
        back_populates="capture", uselist=False
    )


class Scene(Base):
    __tablename__ = "scenes"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    capture_id: Mapped[str] = mapped_column(
        ForeignKey("captures.id", ondelete="CASCADE"), unique=True, nullable=False
    )

    status: Mapped[CaptureStatus] = mapped_column(
        Enum(CaptureStatus, native_enum=False), default=CaptureStatus.queued
    )
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Artifacts written by the export job. Relative to data_dir.
    ply_path: Mapped[str | None] = mapped_column(String, nullable=True)
    spz_path: Mapped[str | None] = mapped_column(String, nullable=True)
    thumbnail_path: Mapped[str | None] = mapped_column(String, nullable=True)

    # Edited artifacts written by the filter job (single replaceable
    # edit per scene; re-apply overwrites). The original ply_path /
    # spz_path above are never touched by edits.
    edited_ply_path: Mapped[str | None] = mapped_column(String, nullable=True)
    edited_spz_path: Mapped[str | None] = mapped_column(String, nullable=True)
    edit_recipe: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    edit_status: Mapped[EditStatus] = mapped_column(
        Enum(EditStatus, native_enum=False), default=EditStatus.none, nullable=False,
    )
    edit_error: Mapped[str | None] = mapped_column(Text, nullable=True)

    # Mesh artifacts written by the on-demand mesh job. The reconstruction
    # source is the trained Gaussian-splatting .ply (Open3D Poisson),
    # so it doesn't depend on the edit pipeline; mesh and edit are
    # independent siblings of the original splat.
    mesh_obj_path: Mapped[str | None] = mapped_column(String, nullable=True)
    mesh_glb_path: Mapped[str | None] = mapped_column(String, nullable=True)
    mesh_status: Mapped[MeshStatus] = mapped_column(
        Enum(MeshStatus, native_enum=False), default=MeshStatus.none, nullable=False,
    )
    mesh_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    # User-tunable mesh extraction params, persisted so a re-run uses
    # the same settings unless the user overrides them.
    mesh_params: Mapped[dict | None] = mapped_column(JSON, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    capture: Mapped[Capture] = relationship(back_populates="scene")
    jobs: Mapped[list["Job"]] = relationship(back_populates="scene")


class Job(Base):
    __tablename__ = "jobs"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    scene_id: Mapped[str] = mapped_column(
        ForeignKey("scenes.id", ondelete="CASCADE"), nullable=False
    )
    kind: Mapped[JobKind] = mapped_column(Enum(JobKind, native_enum=False), nullable=False)
    status: Mapped[JobStatus] = mapped_column(
        Enum(JobStatus, native_enum=False), default=JobStatus.queued
    )

    # Workers claim by writing their identity here + a heartbeat. A
    # claim with a heartbeat older than 60s is considered stale and
    # gets reaped back to `queued` so another worker can pick it up.
    claimed_by: Mapped[str | None] = mapped_column(String, nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    progress: Mapped[float] = mapped_column(default=0.0)  # 0..1
    progress_msg: Mapped[str | None] = mapped_column(String, nullable=True)
    log: Mapped[str | None] = mapped_column(Text, nullable=True)

    payload: Mapped[dict] = mapped_column(JSON, default=dict)
    result: Mapped[dict] = mapped_column(JSON, default=dict)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=_utcnow)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime, default=_utcnow, onupdate=_utcnow
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    scene: Mapped[Scene] = relationship(back_populates="jobs")


# Indexes — the worker poll path hits jobs.status frequently.
Index("ix_jobs_status_kind", Job.status, Job.kind)
Index("ix_jobs_scene", Job.scene_id)
Index("ix_captures_status", Capture.status)

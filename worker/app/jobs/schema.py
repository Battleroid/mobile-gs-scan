"""SQLAlchemy schema for captures, scenes, and jobs.

Single SQLite db at $DATA_DIR/studio.sqlite. Three tables:

  captures   one per phone session or drag-drop set.
  scenes     one per finalized capture, owns the gsplat artifacts.
  jobs       sfm / train / export / mesh steps a worker claims.

Events (the in-memory pub/sub for live progress) are *not* persisted;
they're only useful to clients connected at the moment a step runs.
"""
from __future__ import annotations

import enum
from datetime import datetime, timezone

from sqlalchemy import (
    JSON,
    Column,
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
    return datetime.now(timezone.utc)


class Base(DeclarativeBase):
    pass


class CaptureStatus(str, enum.Enum):
    created = "created"          # session row exists, nothing else
    pairing = "pairing"          # phone has not connected yet, token live
    streaming = "streaming"      # phone WS connected, frames coming in
    uploading = "uploading"      # drag-drop / video upload in progress
    queued = "queued"            # finalize done, waiting for worker
    processing = "processing"    # at least one job has started
    completed = "completed"      # all pipeline jobs ok, scene viewable
    failed = "failed"
    canceled = "canceled"


class CaptureSource(str, enum.Enum):
    mobile_native = "mobile_native"  # Android app via WS
    mobile_web = "mobile_web"        # PWA via WS
    upload = "upload"                # drag-drop image/video


class JobStatus(str, enum.Enum):
    queued = "queued"
    claimed = "claimed"
    running = "running"
    completed = "completed"
    failed = "failed"
    canceled = "canceled"


class JobKind(str, enum.Enum):
    sfm = "sfm"
    train = "train"
    export = "export"
    mesh = "mesh"  # PR #2; always no-op in PR #1


class Capture(Base):
    __tablename__ = "captures"

    id: Mapped[str] = mapped_column(String, primary_key=True)
    name: Mapped[str] = mapped_column(String, nullable=False)
    status: Mapped[CaptureStatus] = mapped_column(
        Enum(CaptureStatus, native_enum=False), default=CaptureStatus.created
    )
    source: Mapped[CaptureSource] = mapped_column(
        Enum(CaptureSource, native_enum=False), nullable=False
    )

    # Random opaque token the phone presents to claim the WS. Cleared
    # after the WS connects so it's a strict one-shot.
    pair_token: Mapped[str | None] = mapped_column(String, nullable=True)
    pair_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime, nullable=True)

    # Tracked while frames are streaming.
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
Index("ix_captures_pair_token", Capture.pair_token)

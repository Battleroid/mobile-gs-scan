"""Async SQLite store for captures, scenes, and jobs.

Mirrors the lingbot-map-studio pattern: one engine, one connection
pool, both api and worker-gs read/write the same db file via the
shared bind-mount under /data.

Concurrency note: SQLite serializes writes. The volume of writes
here is low (a row per claim, a row per heartbeat every few seconds,
a row update per export step) — well below the point where you'd
need anything fancier.

Datetime convention: see the schema module's docstring. Every
datetime in this codebase is a naive UTC value (“wall clock at the
Greenwich meridian”). ``_utcnow()`` returns naive UTC; every column
stores naive UTC; every comparison stays naive↔naive.
"""
from __future__ import annotations

import secrets
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator

from sqlalchemy import delete, select, update
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.config import Settings, get_settings
from app.jobs.schema import (
    Base,
    Capture,
    CaptureSource,
    CaptureStatus,
    Job,
    JobKind,
    JobStatus,
    Scene,
)

_engine: AsyncEngine | None = None
_sessionmaker: async_sessionmaker[AsyncSession] | None = None


def _make_id() -> str:
    return uuid.uuid4().hex[:16]


def _make_token() -> str:
    return secrets.token_urlsafe(24)


def _utcnow() -> datetime:
    # Naive UTC — must match the column type. See module docstring.
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def init_store(settings: Settings | None = None) -> None:
    global _engine, _sessionmaker
    settings = settings or get_settings()
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    settings.captures_dir().mkdir(parents=True, exist_ok=True)
    settings.scenes_dir().mkdir(parents=True, exist_ok=True)

    _engine = create_async_engine(
        settings.db_url,
        future=True,
        connect_args={"timeout": 30},
    )
    _sessionmaker = async_sessionmaker(_engine, expire_on_commit=False)

    async with _engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)
        await conn.exec_driver_sql("PRAGMA journal_mode=WAL")
        await conn.exec_driver_sql("PRAGMA synchronous=NORMAL")
        # SQLAlchemy's create_all() is no-op for existing tables, so
        # newly-added columns on long-lived rows (Scene's edit_*
        # fields, etc.) need an explicit ALTER. SQLite supports
        # ADD COLUMN and tolerates the IF NOT EXISTS-style retry
        # below: we just attempt each add and ignore the duplicate-
        # column error so the boot path is idempotent.
        await _apply_lightweight_migrations(conn)


async def _apply_lightweight_migrations(conn) -> None:
    """Add new columns to existing tables on boot.

    SQLite's ``ALTER TABLE ADD COLUMN`` is cheap (metadata-only) and
    safe on a live db. We attempt each add and swallow the duplicate-
    column error so re-runs are no-ops.
    """
    statements = [
        "ALTER TABLE scenes ADD COLUMN edited_ply_path VARCHAR",
        "ALTER TABLE scenes ADD COLUMN edited_spz_path VARCHAR",
        "ALTER TABLE scenes ADD COLUMN edit_recipe JSON",
        "ALTER TABLE scenes ADD COLUMN edit_status VARCHAR DEFAULT 'none' NOT NULL",
        "ALTER TABLE scenes ADD COLUMN edit_error TEXT",
    ]
    for stmt in statements:
        try:
            await conn.exec_driver_sql(stmt)
        except Exception:
            # Already present — fine. Anything else (corrupt schema,
            # missing table) will surface on the next read.
            pass


async def shutdown_store() -> None:
    global _engine, _sessionmaker
    if _engine is not None:
        await _engine.dispose()
    _engine = None
    _sessionmaker = None


@asynccontextmanager
async def session() -> AsyncIterator[AsyncSession]:
    if _sessionmaker is None:
        raise RuntimeError("store not initialized — call init_store() first")
    async with _sessionmaker() as s:
        yield s


# ─── captures ─────────────────────────────────────


async def create_capture(
    *,
    name: str,
    source: CaptureSource,
    has_pose: bool = False,
    meta: dict | None = None,
    settings: Settings | None = None,
) -> Capture:
    settings = settings or get_settings()
    cap = Capture(
        id=_make_id(),
        name=name,
        source=source,
        status=CaptureStatus.pairing if source != CaptureSource.upload else CaptureStatus.created,
        pair_token=_make_token() if source != CaptureSource.upload else None,
        pair_token_expires_at=(
            _utcnow() + timedelta(seconds=settings.pair_token_ttl_seconds)
            if source != CaptureSource.upload
            else None
        ),
        has_pose=has_pose,
        meta=meta or {},
    )
    async with session() as s:
        s.add(cap)
        await s.commit()
        await s.refresh(cap)
    return cap


async def list_captures(limit: int = 100) -> list[Capture]:
    async with session() as s:
        rows = await s.execute(
            select(Capture).order_by(Capture.created_at.desc()).limit(limit)
        )
        return list(rows.scalars())


async def get_capture(capture_id: str) -> Capture | None:
    async with session() as s:
        return await s.get(Capture, capture_id)


async def get_capture_by_pair_token(token: str) -> Capture | None:
    async with session() as s:
        rows = await s.execute(
            select(Capture).where(Capture.pair_token == token).limit(1)
        )
        return rows.scalar_one_or_none()


async def claim_pair_token(token: str) -> Capture | None:
    """Atomically consume a pair token + flip the capture into streaming."""
    async with session() as s:
        rows = await s.execute(
            select(Capture).where(Capture.pair_token == token).limit(1)
        )
        cap = rows.scalar_one_or_none()
        if cap is None:
            return None
        if cap.pair_token_expires_at and cap.pair_token_expires_at < _utcnow():
            return None
        cap.pair_token = None
        cap.pair_token_expires_at = None
        cap.status = CaptureStatus.streaming
        await s.commit()
        await s.refresh(cap)
        return cap


async def bump_capture_frames(capture_id: str, *, accepted: int, dropped: int) -> None:
    async with session() as s:
        await s.execute(
            update(Capture)
            .where(Capture.id == capture_id)
            .values(
                frame_count=Capture.frame_count + accepted,
                dropped_count=Capture.dropped_count + dropped,
                updated_at=_utcnow(),
            )
        )
        await s.commit()


async def set_capture_status(
    capture_id: str, status: CaptureStatus, *, error: str | None = None
) -> None:
    async with session() as s:
        await s.execute(
            update(Capture)
            .where(Capture.id == capture_id)
            .values(status=status, error=error, updated_at=_utcnow())
        )
        await s.commit()


async def set_capture_name(capture_id: str, name: str) -> None:
    """Update the user-facing name on a capture row.

    The caller is expected to have already trimmed + validated
    [name] (non-empty, length-bounded). The id and status are
    untouched.
    """
    async with session() as s:
        await s.execute(
            update(Capture)
            .where(Capture.id == capture_id)
            .values(name=name, updated_at=_utcnow())
        )
        await s.commit()


# ─── scenes ──────────────────────────────────────


async def create_scene(capture_id: str) -> Scene:
    scene = Scene(id=_make_id(), capture_id=capture_id, status=CaptureStatus.queued)
    async with session() as s:
        s.add(scene)
        await s.commit()
        await s.refresh(scene)
    return scene


async def get_scene(scene_id: str) -> Scene | None:
    async with session() as s:
        return await s.get(Scene, scene_id)


async def get_scene_for_capture(capture_id: str) -> Scene | None:
    async with session() as s:
        rows = await s.execute(
            select(Scene).where(Scene.capture_id == capture_id).limit(1)
        )
        return rows.scalar_one_or_none()


async def update_scene(scene_id: str, **fields) -> None:
    if not fields:
        return
    async with session() as s:
        await s.execute(update(Scene).where(Scene.id == scene_id).values(**fields))
        await s.commit()


# ─── jobs ────────────────────────────────────────


async def enqueue_job(scene_id: str, kind: JobKind, payload: dict | None = None) -> Job:
    job = Job(
        id=_make_id(),
        scene_id=scene_id,
        kind=kind,
        payload=payload or {},
    )
    async with session() as s:
        s.add(job)
        await s.commit()
        await s.refresh(job)
    return job


async def list_jobs_for_scene(scene_id: str) -> list[Job]:
    async with session() as s:
        rows = await s.execute(
            select(Job).where(Job.scene_id == scene_id).order_by(Job.created_at)
        )
        return list(rows.scalars())


async def get_job(job_id: str) -> Job | None:
    async with session() as s:
        return await s.get(Job, job_id)


async def claim_next_job(*, worker_id: str, kinds: list[JobKind]) -> Job | None:
    """Best-effort claim of the oldest queued job in `kinds`.

    Two workers racing on the same row is still safe because of the
    `WHERE status='queued'` condition in the UPDATE — only the first
    flip wins, the second sees rowcount 0 and tries again.
    """
    async with session() as s:
        rows = await s.execute(
            select(Job)
            .where(Job.status == JobStatus.queued, Job.kind.in_(kinds))
            .order_by(Job.created_at)
            .limit(1)
        )
        candidate = rows.scalar_one_or_none()
        if candidate is None:
            return None
        result = await s.execute(
            update(Job)
            .where(Job.id == candidate.id, Job.status == JobStatus.queued)
            .values(
                status=JobStatus.claimed,
                claimed_by=worker_id,
                heartbeat_at=_utcnow(),
                updated_at=_utcnow(),
            )
        )
        await s.commit()
        if result.rowcount == 0:
            return None
        return await s.get(Job, candidate.id)


async def update_job(
    job_id: str,
    *,
    status: JobStatus | None = None,
    progress: float | None = None,
    progress_msg: str | None = None,
    heartbeat: bool = False,
    started: bool = False,
    completed: bool = False,
    error: str | None = None,
    result: dict | None = None,
) -> None:
    values: dict = {"updated_at": _utcnow()}
    if status is not None:
        values["status"] = status
    if progress is not None:
        values["progress"] = progress
    if progress_msg is not None:
        values["progress_msg"] = progress_msg
    if heartbeat:
        values["heartbeat_at"] = _utcnow()
    if started:
        values["started_at"] = _utcnow()
    if completed:
        values["completed_at"] = _utcnow()
    if error is not None:
        values["error"] = error
    if result is not None:
        values["result"] = result
    async with session() as s:
        await s.execute(update(Job).where(Job.id == job_id).values(**values))
        await s.commit()


async def cancel_job(job_id: str) -> bool:
    """Set a job to status=canceled if it's still in flight.

    Returns True if a row was updated, False if the job was already
    in a terminal state (completed / failed / canceled) or doesn't
    exist. Used by both the explicit POST /api/jobs/{id}/cancel
    endpoint and DELETE /api/captures/{id} (which cancels every
    in-flight job for the capture's scene before tearing down).
    """
    async with session() as s:
        result = await s.execute(
            update(Job)
            .where(
                Job.id == job_id,
                Job.status.in_([
                    JobStatus.queued,
                    JobStatus.claimed,
                    JobStatus.running,
                ]),
            )
            .values(status=JobStatus.canceled, updated_at=_utcnow())
        )
        await s.commit()
        return int(result.rowcount or 0) > 0


async def delete_terminal_jobs_of_kind(scene_id: str, kind: JobKind) -> int:
    """Drop completed / failed rows of a given kind for a scene.

    Used by the filter/edit upsert path so re-applying a recipe leaves
    a single canonical filter-job row in the scene's job list rather
    than one per apply. We DELIBERATELY don't touch:
      * in-flight rows (queued / claimed / running) — the caller
        cancels those first.
      * canceled rows whose worker hasn't acked yet — the runner's
        heartbeat loop and ``_run_filter``'s pre-commit re-fetch
        both look up the row by id; if it's deleted first, the
        canceled subprocess's late completion happily overwrites
        the new recipe's artifacts. Leaving canceled rows around
        is the load-bearing protection against that race.
    """
    async with session() as s:
        result = await s.execute(
            delete(Job).where(
                Job.scene_id == scene_id,
                Job.kind == kind,
                Job.status.in_([
                    JobStatus.completed,
                    JobStatus.failed,
                ]),
            )
        )
        await s.commit()
        return int(result.rowcount or 0)


async def reap_stale_jobs(*, stale_after_seconds: int = 60) -> int:
    """Re-queue claims with a heartbeat older than `stale_after_seconds`."""
    cutoff = _utcnow() - timedelta(seconds=stale_after_seconds)
    async with session() as s:
        result = await s.execute(
            update(Job)
            .where(
                Job.status.in_([JobStatus.claimed, JobStatus.running]),
                Job.heartbeat_at < cutoff,
            )
            .values(
                status=JobStatus.queued,
                claimed_by=None,
                heartbeat_at=None,
                updated_at=_utcnow(),
            )
        )
        await s.commit()
        return int(result.rowcount or 0)

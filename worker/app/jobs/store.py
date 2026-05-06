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

import json
import uuid
from contextlib import asynccontextmanager
from datetime import datetime, timedelta, timezone
from typing import AsyncIterator

from sqlalchemy import and_, delete, or_, select, text, update
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
        "ALTER TABLE scenes ADD COLUMN mesh_obj_path VARCHAR",
        "ALTER TABLE scenes ADD COLUMN mesh_glb_path VARCHAR",
        "ALTER TABLE scenes ADD COLUMN mesh_status VARCHAR DEFAULT 'none' NOT NULL",
        "ALTER TABLE scenes ADD COLUMN mesh_error TEXT",
        "ALTER TABLE scenes ADD COLUMN mesh_params JSON",
        # Added with PR-D's JobKind.thumbnail step. Pre-existing
        # dbs without this column would fail every ORM read of
        # Scene with `no such column` once the runtime ships
        # this version, so the migration is required even though
        # the column itself was technically declared on the
        # schema before the rendering pipeline arrived.
        "ALTER TABLE scenes ADD COLUMN thumbnail_path VARCHAR",
        # One-shot post-pairing-removal repair: rows that were stuck
        # in pairing/streaming when the WS endpoint was retired
        # would otherwise fail to decode their status enum on read.
        # Flip them to canceled so the row stays addressable. Safe
        # to leave in indefinitely (idempotent no-op once no such
        # rows exist).
        "UPDATE captures SET status='canceled' "
        "WHERE status IN ('pairing','streaming')",
        # mobile_native / mobile_web are no longer valid sources;
        # collapse legacy rows to 'upload' for the same reason.
        "UPDATE captures SET source='upload' "
        "WHERE source IN ('mobile_native','mobile_web')",
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
    source: CaptureSource = CaptureSource.upload,
    has_pose: bool = False,
    meta: dict | None = None,
) -> Capture:
    cap = Capture(
        id=_make_id(),
        name=name,
        source=source,
        status=CaptureStatus.created,
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


async def delete_capture(capture_id: str) -> bool:
    """Hard-delete a capture row + its scenes + the scenes' jobs.

    The schema's ``ondelete="CASCADE"`` declarations are no-ops on
    SQLite without a per-connection ``PRAGMA foreign_keys=ON``; we
    keep FK enforcement off (turning it on globally has historically
    surfaced latent FK issues elsewhere in the pipeline) and cascade
    manually. Returns True if the row existed and was removed;
    False if no capture had this id (caller can decide whether
    that's a 404 or an idempotent no-op).

    Concurrency: cascades run as bulk DELETE statements (no SELECT-
    then-iterate) so each WHERE clause sees the live state at the
    moment that statement runs, not a snapshot. SQLite's first
    write in a session also acquires the database-level write lock,
    so a concurrent ``finalize_capture`` trying to ``create_scene``
    for this capture id blocks behind us until commit. That closes
    the race window where a finalize landing between a SELECT-
    scenes and DELETE-capture would leave an orphan scene pointing
    at a deleted capture.
    """
    async with session() as s:
        # Delete jobs whose scene belongs to this capture. Subselect
        # by capture_id (not by a precomputed scene id list) so any
        # scene a concurrent finalize() raced in is also cleaned up —
        # same shape as the next two deletes.
        await s.execute(
            delete(Job).where(
                Job.scene_id.in_(
                    select(Scene.id).where(Scene.capture_id == capture_id)
                )
            )
        )
        await s.execute(
            delete(Scene).where(Scene.capture_id == capture_id)
        )
        result = await s.execute(
            delete(Capture).where(Capture.id == capture_id)
        )
        await s.commit()
        return result.rowcount > 0


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


async def create_scene(capture_id: str) -> Scene | None:
    """Create a Scene row for ``capture_id``. Returns None if the
    capture doesn't exist (the caller treats that as 404 / race-loss).

    The parent-capture existence check and the INSERT happen in one
    ``INSERT ... SELECT ... WHERE EXISTS`` statement so a concurrent
    ``delete_capture`` can't slip the parent row away between the
    check and the insert. Without this guard, a finalize() call that
    passed its initial ``get_capture`` check could race past a
    ``delete_capture`` commit and INSERT a Scene with a now-dangling
    ``capture_id`` — which would then enqueue jobs that the worker
    later picks up and fails on with ``capture vanished``.

    SQLite's write-lock serialization handles the timing: either
    this INSERT acquires the write lock first (rowcount=1, scene
    created cleanly) or delete_capture's first DELETE wins
    (rowcount=0, no orphan, return None).
    """
    scene_id = _make_id()
    now = _utcnow()
    async with session() as s:
        result = await s.execute(
            text(
                """
                INSERT INTO scenes (
                    id, capture_id, status,
                    edit_status, mesh_status, created_at
                )
                SELECT
                    :sid, :cid, 'queued',
                    'none', 'none', :now
                WHERE EXISTS (SELECT 1 FROM captures WHERE id = :cid)
                """
            ),
            {"sid": scene_id, "cid": capture_id, "now": now},
        )
        await s.commit()
        if result.rowcount == 0:
            return None
        return await s.get(Scene, scene_id)


async def get_scene(scene_id: str) -> Scene | None:
    async with session() as s:
        return await s.get(Scene, scene_id)


async def get_scene_for_capture(capture_id: str) -> Scene | None:
    async with session() as s:
        rows = await s.execute(
            select(Scene).where(Scene.capture_id == capture_id).limit(1)
        )
        return rows.scalar_one_or_none()


async def list_scenes_needing_thumbnail() -> list[Scene]:
    """Find scenes that have a trained .ply but no thumbnail PNG
    yet AND no in-flight thumbnail job.

    Used by the worker's boot-time backfill to render thumbnails
    for captures that completed before PR-D shipped (or whose
    earlier render failed back when ``ns-render`` wasn't on PATH).
    Excluding scenes with an active thumbnail job avoids
    double-enqueuing when the worker restarts mid-pipeline.
    A previously-failed thumbnail (job in ``failed`` /
    ``canceled`` / ``completed`` with no ``thumbnail_path``) is
    re-tried on each boot — cheap, idempotent, and lets a one-time
    install of ns-render fix every previously-skipped scene.
    """
    async with session() as s:
        active = select(Job.scene_id).where(
            Job.kind == JobKind.thumbnail,
            Job.status.in_(
                [JobStatus.queued, JobStatus.claimed, JobStatus.running]
            ),
        )
        rows = await s.execute(
            select(Scene).where(
                Scene.ply_path.is_not(None),
                Scene.thumbnail_path.is_(None),
                Scene.id.not_in(active),
            )
        )
        return list(rows.scalars())


async def update_scene(scene_id: str, **fields) -> None:
    if not fields:
        return
    async with session() as s:
        await s.execute(update(Scene).where(Scene.id == scene_id).values(**fields))
        await s.commit()


# ─── jobs ────────────────────────────────────────


async def enqueue_job(
    scene_id: str, kind: JobKind, payload: dict | None = None
) -> Job | None:
    """Enqueue a job for a scene. Returns None if the scene doesn't
    exist (caller treats as race-loss and surfaces a 404 / bails the
    pipeline).

    Atomic via ``INSERT ... SELECT ... WHERE EXISTS`` so a concurrent
    ``delete_capture`` cascading the scene away can't race past
    ``finalize`` between ``create_scene`` and ``enqueue_pipeline``.
    Without this guard we'd insert orphan jobs that workers later
    fail with ``scene vanished``.

    Same SQLite single-writer-lock semantics as ``create_scene``:
    either we win the race against ``delete_capture``'s scene/job
    cascade (rowcount=1, job returned) or delete wins (rowcount=0,
    no orphan, return None).
    """
    job_id = _make_id()
    now = _utcnow()
    payload_json = json.dumps(payload or {})
    async with session() as s:
        result = await s.execute(
            text(
                """
                INSERT INTO jobs (
                    id, scene_id, kind, status, progress,
                    payload, result, created_at, updated_at
                )
                SELECT
                    :jid, :sid, :kind, 'queued', 0.0,
                    :payload, '{}', :now, :now
                WHERE EXISTS (SELECT 1 FROM scenes WHERE id = :sid)
                """
            ),
            {
                "jid": job_id,
                "sid": scene_id,
                "kind": kind.value,
                "payload": payload_json,
                "now": now,
            },
        )
        await s.commit()
        if result.rowcount == 0:
            return None
        return await s.get(Job, job_id)


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
    """Drop fully-acked terminal rows of a given kind for a scene.

    Used by the filter/edit upsert path so re-applying a recipe leaves
    a single canonical filter-job row in the scene's job list rather
    than one per apply. We delete:
      * completed / failed rows unconditionally (the worker is done
        with them by the time their status flips).
      * canceled rows where ``completed_at IS NOT NULL`` — that's
        the marker ``_ack_user_cancel`` sets after the runner has
        observed the cancel, killed any subprocess, and finished
        the post-cancel bookkeeping. Earlier canceled rows are
        load-bearing for the runner's heartbeat + ``_run_filter``
        pre-commit re-fetch (deleting them out from under an
        in-flight worker would let the late completion overwrite
        the new recipe's artifacts), but once the worker has acked
        them they're safe to GC.
    We DELIBERATELY don't touch in-flight rows (queued / claimed /
    running) — the caller cancels those first.
    """
    async with session() as s:
        result = await s.execute(
            delete(Job).where(
                Job.scene_id == scene_id,
                Job.kind == kind,
                or_(
                    Job.status.in_([JobStatus.completed, JobStatus.failed]),
                    and_(
                        Job.status == JobStatus.canceled,
                        Job.completed_at.is_not(None),
                    ),
                ),
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

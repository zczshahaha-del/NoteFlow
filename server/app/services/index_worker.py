from __future__ import annotations

import asyncio
import logging
import os
import socket
import uuid
from contextlib import suppress
from datetime import datetime, timedelta

from sqlalchemy import or_, select, update

from app.config import cfg
from app.database import AsyncSessionLocal
from app.models.db import Note, NoteEmbedding, NoteIndexJob
from app.rag.pipeline import indexer as rag_index
from app.services import markdown_index
from app.services.embedding_index import IndexEmbedding, prepare_embeddings, reusable_embedding
from app.services.embeddings import content_hash
from app.services.observability import record_metric

logger = logging.getLogger(__name__)

_worker_task: asyncio.Task | None = None
_wake_event: asyncio.Event | None = None
_stop_event: asyncio.Event | None = None
_worker_id = f"{socket.gethostname()}:{os.getpid()}:{uuid.uuid4().hex[:8]}"


def notify_index_worker() -> None:
    if _wake_event is not None:
        _wake_event.set()


async def _claim_one() -> str | None:
    now = datetime.utcnow()
    stale_at = now - timedelta(seconds=max(1, cfg.INDEX_JOB_STALE_SECONDS))
    async with AsyncSessionLocal() as session:
        async with session.begin():
            result = await session.execute(
                select(NoteIndexJob)
                .where(
                    or_(
                        (NoteIndexJob.status == "pending")
                        & or_(NoteIndexJob.next_attempt_at.is_(None), NoteIndexJob.next_attempt_at <= now),
                        (NoteIndexJob.status == "running")
                        & or_(NoteIndexJob.heartbeat_at.is_(None), NoteIndexJob.heartbeat_at < stale_at),
                    ),
                )
                .order_by(NoteIndexJob.created_at)
                .with_for_update(skip_locked=True)
                .limit(1)
            )
            job = result.scalar_one_or_none()
            if job is None:
                return None
            job.status = "running"
            job.claim_owner = _worker_id
            job.claimed_at = now
            job.heartbeat_at = now
            job.started_at = job.started_at or now
            return job.id


async def _load_claim(job_id: str) -> tuple[NoteIndexJob | None, Note | None]:
    async with AsyncSessionLocal() as session:
        job = await session.get(NoteIndexJob, job_id)
        if job is None or job.status != "running" or job.claim_owner != _worker_id:
            return None, None
        note = await session.scalar(
            select(Note).where(Note.id == job.note_id, Note.user_id == job.user_id, Note.deleted_at.is_(None))
        )
        return job, note


async def _prepare_index(note: Note, *, rag: bool):
    if rag:
        nodes = rag_index.parse_index_nodes(note)
        inputs = rag_index.rag_embedding_inputs(note, nodes)
        async with AsyncSessionLocal() as session:
            rows = await rag_index.load_rag_embeddings(session, note, list(inputs))
            reused = {key: IndexEmbedding.from_row(row) for key, row in rows.items() if reusable_embedding(row)}
        return nodes, await prepare_embeddings(inputs, reused)

    sections = markdown_index.parse_markdown_sections(note.content or "")
    inputs = markdown_index.legacy_embedding_inputs(note, sections)
    async with AsyncSessionLocal() as session:
        rows = await session.scalars(
            select(NoteEmbedding).where(
                NoteEmbedding.note_id == note.id,
                NoteEmbedding.user_id == note.user_id,
                NoteEmbedding.content_hash.in_(inputs),
                NoteEmbedding.provider == cfg.EMBEDDING_PROVIDER,
                NoteEmbedding.embedding_model == cfg.EMBEDDING_MODEL,
                NoteEmbedding.embedding_dim == cfg.EMBEDDING_DIMENSIONS,
            ).order_by(NoteEmbedding.indexed_at.desc(), NoteEmbedding.id)
        )
        reused = {}
        for row in rows:
            if row.content_hash in inputs and reusable_embedding(row):
                reused.setdefault(row.content_hash, IndexEmbedding.from_row(row))
    return sections, await prepare_embeddings(inputs, reused)


async def _keep_claim_alive(claim: NoteIndexJob) -> None:
    interval = max(1, cfg.INDEX_JOB_STALE_SECONDS) / 3
    while True:
        await asyncio.sleep(interval)
        try:
            async with AsyncSessionLocal() as session, session.begin():
                active_id = await session.scalar(
                    update(NoteIndexJob).where(
                        NoteIndexJob.id == claim.id,
                        NoteIndexJob.status == "running",
                        NoteIndexJob.claim_owner == _worker_id,
                        NoteIndexJob.claimed_at == claim.claimed_at,
                    ).values(heartbeat_at=datetime.utcnow()).returning(NoteIndexJob.id)
                )
                if active_id is None:
                    return
        except Exception:
            logger.exception("could not renew index claim %s", claim.id)


def _clear_claim(job: NoteIndexJob) -> None:
    job.claim_owner = None
    job.claimed_at = None
    job.heartbeat_at = None


def _cancel_index_job(job: NoteIndexJob, code: str, reason: str) -> None:
    job.status = "cancelled"
    job.error_code = code
    job.error_message = reason
    job.finished_at = datetime.utcnow()
    job.next_attempt_at = None
    _clear_claim(job)


async def _publish_index(
    claim: NoteIndexJob, snapshot: Note | None, prepared=None, error: Exception | None = None,
) -> bool:
    # Use the note-first lock order of the note write path. Check the claim and
    # note again before publishing results calculated outside this transaction.
    async with AsyncSessionLocal() as session, session.begin():
        note = await session.scalar(
            select(Note).where(
                Note.id == claim.note_id, Note.user_id == claim.user_id, Note.deleted_at.is_(None)
            ).with_for_update()
        )
        job = await session.get(NoteIndexJob, claim.id, with_for_update=True)
        if (
            job is None or job.status != "running" or job.claim_owner != _worker_id
            or job.claimed_at != claim.claimed_at
        ):
            return False
        if note is None:
            _cancel_index_job(job, "NOTE_NOT_FOUND", "note no longer exists")
            return True

        source_version = content_hash(note.content or "")
        if claim.source_version and source_version != claim.source_version:
            _cancel_index_job(job, "STALE_SOURCE_VERSION", "note changed while indexing")
            return True
        if (
            snapshot is None or source_version != content_hash(snapshot.content or "")
            or note.title != snapshot.title or note.tags != snapshot.tags
        ):
            # The job still matches the note, but the snapshot must be refreshed.
            job.status = "pending"
            job.next_attempt_at = None
            _clear_claim(job)
            return True

        rag = claim.graph_version == rag_index.GRAPH_VERSION
        if error is not None:
            if rag:
                await rag_index.schedule_rag_index_retry(session, note, job, error)
            else:
                markdown_index.schedule_index_retry(note, job, error)
            record_metric("rag_v2_index" if rag else "index", "note", status="failed")
        elif prepared is None:
            raise RuntimeError("index results must be prepared before publishing")
        elif rag:
            await rag_index.run_rag_index_job(session, note, job, prepared=prepared)
        else:
            await markdown_index.run_index_job(session, note, job, prepared=prepared)
        return True


async def _run_claimed(job_id: str) -> bool:
    claim, note = await _load_claim(job_id)
    if claim is None:
        return False
    if note is None or (claim.source_version and claim.source_version != content_hash(note.content or "")):
        return await _publish_index(claim, note)

    heartbeat = asyncio.create_task(_keep_claim_alive(claim))
    try:
        prepared = await _prepare_index(note, rag=claim.graph_version == rag_index.GRAPH_VERSION)
    except Exception as exc:
        return await _publish_index(claim, note, error=exc)
    finally:
        heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat

    try:
        return await _publish_index(claim, note, prepared)
    except Exception as exc:
        # The publishing transaction was rolled back. Record a retry separately,
        # leaving the previous visible index intact.
        logger.exception("could not publish index job %s", job_id)
        return await _publish_index(claim, note, error=exc)


async def _claim_and_run_one() -> bool:
    job_id = await _claim_one()
    if job_id is None:
        return False
    return await _run_claimed(job_id)


async def _worker_loop() -> None:
    assert _wake_event is not None and _stop_event is not None
    logger.info("note index worker started")
    while not _stop_event.is_set():
        try:
            processed = await _claim_and_run_one()
            if processed:
                continue
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("note index worker iteration failed")

        _wake_event.clear()
        try:
            await asyncio.wait_for(_wake_event.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            pass
    logger.info("note index worker stopped")


async def start_index_worker() -> None:
    global _worker_task, _wake_event, _stop_event
    if _worker_task and not _worker_task.done():
        return
    _wake_event = asyncio.Event()
    _stop_event = asyncio.Event()
    _worker_task = asyncio.create_task(_worker_loop(), name="noteflow-index-worker")


async def stop_index_worker() -> None:
    global _worker_task
    if _stop_event is not None:
        _stop_event.set()
    if _wake_event is not None:
        _wake_event.set()
    if _worker_task is not None:
        try:
            await asyncio.wait_for(_worker_task, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            _worker_task.cancel()
        _worker_task = None

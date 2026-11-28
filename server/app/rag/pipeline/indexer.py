from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.config import cfg
from app.models.db import (
    Note,
    NoteIndexJob,
    RagEmbedding,
    RagIndexState,
    RagNode,
)
from app.rag.pipeline.parser import CHUNKER_VERSION, PARSER_VERSION, StructuredNode, parse_markdown_nodes
from app.services.embedding_index import IndexEmbedding, prepare_embeddings, reusable_embedding
from app.services.embeddings import chunk_embedding_text, content_hash
from app.services.observability import record_metric
from app.utils import random_id


GRAPH_VERSION = "rag-v2"


@dataclass
class V2IndexStats:
    nodes: int = 0
    created: int = 0
    reused: int = 0
    embedded: int = 0
    embedding_reused: int = 0
    skipped: int = 0
    failed: int = 0


def embedding_version() -> str:
    return f"{cfg.EMBEDDING_PROVIDER}:{cfg.EMBEDDING_MODEL}:{cfg.EMBEDDING_DIMENSIONS}"


async def create_rag_index_job(
    session: AsyncSession,
    note: Note,
    *,
    force: bool = False,
) -> NoteIndexJob:
    source_version = content_hash(note.content or "")
    existing = await session.scalar(
        select(NoteIndexJob)
        .where(
            NoteIndexJob.note_id == note.id,
            NoteIndexJob.user_id == note.user_id,
            NoteIndexJob.graph_version == GRAPH_VERSION,
            NoteIndexJob.source_version == source_version,
        )
        .order_by(NoteIndexJob.created_at.desc())
        .limit(1)
    )
    if existing is not None and not force:
        if existing.status in {"pending", "running", "success"}:
            if existing.status == "pending" and existing.next_attempt_at is not None:
                # Leave a job that a worker is claiming to that worker.
                pending_job = await session.scalar(
                    select(NoteIndexJob)
                    .where(NoteIndexJob.id == existing.id, NoteIndexJob.status == "pending")
                    .with_for_update(skip_locked=True)
                    .execution_options(populate_existing=True)
                )
                if pending_job is not None:
                    pending_job.next_attempt_at = None
            return existing

    job = NoteIndexJob(
        id=random_id(),
        note_id=note.id,
        user_id=note.user_id,
        status="pending",
        retry_count=0,
        max_retries=3,
        source_version=source_version,
        parser_version=PARSER_VERSION,
        chunker_version=CHUNKER_VERSION,
        embedding_version=embedding_version(),
        graph_version=GRAPH_VERSION,
        idempotency_key=f"rag-v2:{note.id}:{source_version}" if not force else None,
    )
    session.add(job)
    await session.flush()
    return job


def _node_embedding_text(note: Note, node: StructuredNode) -> str:
    return chunk_embedding_text(
        note_title=note.title,
        section_title=" / ".join(node.section_path),
        chunk_content=node.content,
        tags=note.tags or [],
    )


def parse_index_nodes(note: Note) -> list[StructuredNode]:
    return parse_markdown_nodes(
        note.content or "",
        note_id=note.id,
        note_title=note.title,
        source_version=content_hash(note.content or ""),
        target_tokens=max(400, min(cfg.RAG_CHUNK_SIZE, 800)),
        max_tokens=1000,
        overlap_tokens=max(60, min(cfg.RAG_CHUNK_OVERLAP, 120)),
    )


def rag_embedding_inputs(note: Note, nodes: list[StructuredNode]) -> dict[str, str]:
    return {node.id: _node_embedding_text(note, node) for node in nodes}


async def load_rag_embeddings(
    session: AsyncSession, note: Note, node_ids: list[str],
) -> dict[str, RagEmbedding]:
    if not node_ids:
        return {}
    rows = await session.scalars(
        select(RagEmbedding).where(
            RagEmbedding.note_id == note.id,
            RagEmbedding.user_id == note.user_id,
            RagEmbedding.node_id.in_(node_ids),
            RagEmbedding.provider == cfg.EMBEDDING_PROVIDER,
            RagEmbedding.embedding_model == cfg.EMBEDDING_MODEL,
            RagEmbedding.embedding_dim == cfg.EMBEDDING_DIMENSIONS,
        )
    )
    return {row.node_id: row for row in rows}


async def _index_embeddings(
    session: AsyncSession, note: Note, nodes: list[StructuredNode], stats: V2IndexStats,
    prepared: dict[str, IndexEmbedding] | None,
) -> None:
    existing = await load_rag_embeddings(session, note, [node.id for node in nodes])
    reusable_ids = {key for key, row in existing.items() if reusable_embedding(row)}
    stats.embedding_reused = len(reusable_ids)
    missing = [node for node in nodes if node.id not in reusable_ids]
    if not missing:
        return
    if prepared is None:
        prepared = await prepare_embeddings(rag_embedding_inputs(note, missing))

    for node in missing:
        value = prepared[node.id]
        row = existing.get(node.id)
        if row is None:
            row = RagEmbedding(id=random_id(), node_id=node.id, note_id=note.id, user_id=note.user_id)
        row.content_hash = node.content_hash
        for key, field in value.fields().items():
            setattr(row, key, field)
        session.add(row)
        if value.status == "indexed":
            stats.embedded += 1
        elif value.status == "skipped":
            stats.skipped += 1
        else:
            stats.failed += 1
    await session.flush()


async def _current_note_source_version(session: AsyncSession, note_id: str) -> str | None:
    content = await session.scalar(select(Note.content).where(Note.id == note_id, Note.deleted_at.is_(None)))
    return content_hash(content or "") if content is not None else None


def _mark_v2_retry(job: NoteIndexJob, error: Exception, now: datetime) -> None:
    job.error_code = type(error).__name__[:80]
    job.error_message = str(error)[:2000]
    job.claim_owner = None
    job.claimed_at = None
    job.heartbeat_at = None
    job.retry_count = (job.retry_count or 0) + 1
    if job.retry_count <= (job.max_retries or 3):
        job.status = "pending"
        job.next_attempt_at = now + timedelta(seconds=min(60, 2 ** job.retry_count))
        job.finished_at = None
    else:
        job.status = "failed"
        job.next_attempt_at = None
        job.finished_at = now


async def schedule_rag_index_retry(session: AsyncSession, note: Note, job: NoteIndexJob, error: Exception) -> None:
    _mark_v2_retry(job, error, datetime.utcnow())
    state = await session.get(RagIndexState, note.id)
    if state is None:
        state = RagIndexState(note_id=note.id, user_id=note.user_id, source_version=job.source_version or "")
    if state.status != "indexed" and state.source_version in {"", job.source_version}:
        state.status = "failed" if job.status == "failed" else "pending"
        state.error_code = job.error_code
        state.error_message = job.error_message
        state.updated_at = datetime.utcnow()
        session.add(state)


async def run_rag_index_job(
    session: AsyncSession, note: Note, job: NoteIndexJob, *,
    prepared: tuple[list[StructuredNode], dict[str, IndexEmbedding]] | None = None,
) -> NoteIndexJob:
    started = datetime.utcnow()
    job.status = "running"
    job.started_at = job.started_at or started
    job.error_code = None
    job.error_message = None
    await session.flush()
    try:
        source_version = content_hash(note.content or "")
        if source_version != job.source_version:
            job.status = "cancelled"
            job.error_code = "STALE_SOURCE_VERSION"
            job.error_message = "note changed before v2 indexing started"
            job.finished_at = datetime.utcnow()
            job.claim_owner = None
            job.heartbeat_at = None
            return job

        nodes, prepared_embeddings = prepared if prepared is not None else (parse_index_nodes(note), None)
        stats = V2IndexStats(nodes=len(nodes))
        existing_nodes = {
            row.id: row
            for row in (
                await session.execute(select(RagNode).where(RagNode.id.in_([node.id for node in nodes])))
            ).scalars().all()
        } if nodes else {}

        for node in nodes:
            row = existing_nodes.get(node.id)
            if row is None:
                row = RagNode(id=node.id)
                stats.created += 1
            else:
                stats.reused += 1
            row.note_id = note.id
            row.user_id = note.user_id
            row.section_key = node.section_key
            row.section_path = node.section_path
            row.node_type = node.node_type
            row.node_index = node.node_index
            row.content = node.content
            row.token_count = node.token_count
            row.content_hash = node.content_hash
            row.source_version = source_version
            row.parser_version = PARSER_VERSION
            row.chunker_version = CHUNKER_VERSION
            row.block_types = node.block_types
            row.node_metadata = node.metadata
            if row.id not in existing_nodes:
                row.active = False
            session.add(row)
        await session.flush()
        await _index_embeddings(session, note, nodes, stats, prepared_embeddings)

        # Inline callers can still prepare inside their own transaction.
        if await _current_note_source_version(session, note.id) != source_version:
            job.status = "cancelled"
            job.error_code = "STALE_SOURCE_VERSION"
            job.error_message = "note changed while v2 indexing was running"
            job.finished_at = datetime.utcnow()
            job.claim_owner = None
            job.heartbeat_at = None
            await session.flush()
            return job

        await session.execute(
            update(RagNode)
            .where(RagNode.note_id == note.id, RagNode.user_id == note.user_id)
            .values(active=False)
        )
        if nodes:
            await session.execute(
                update(RagNode).where(RagNode.id.in_([node.id for node in nodes])).values(active=True)
            )

        state = await session.get(RagIndexState, note.id)
        if state is None:
            state = RagIndexState(note_id=note.id, user_id=note.user_id)
        state.user_id = note.user_id
        state.source_version = source_version
        state.parser_version = PARSER_VERSION
        state.chunker_version = CHUNKER_VERSION
        state.embedding_version = embedding_version()
        state.status = "indexed"
        state.node_count = len(nodes)
        state.error_code = None
        state.error_message = None
        state.indexed_at = datetime.utcnow()
        state.updated_at = datetime.utcnow()
        session.add(state)

        job.status = "success"
        job.error_code = None
        job.error_message = None
        job.stats = {
            "indexVersion": GRAPH_VERSION,
            "nodes": stats.nodes,
            "created": stats.created,
            "reused": stats.reused,
            "embedded": stats.embedded,
            "embeddingReused": stats.embedding_reused,
            "skipped": stats.skipped,
            "failed": stats.failed,
        }
        job.finished_at = datetime.utcnow()
        job.next_attempt_at = None
        job.claim_owner = None
        job.claimed_at = None
        job.heartbeat_at = None
        record_metric("rag_v2_index", "note", duration_ms=(datetime.utcnow() - started).total_seconds() * 1000)
        await session.flush()
        return job
    except Exception as exc:
        if prepared is not None:
            raise
        await schedule_rag_index_retry(session, note, job, exc)
        record_metric("rag_v2_index", "note", status="failed", duration_ms=(datetime.utcnow() - started).total_seconds() * 1000)
        await session.flush()
        return job


async def rag_index_diagnostics(session: AsyncSession, user_id: str, note_id: str | None = None) -> dict:
    conditions = [RagIndexState.user_id == user_id]
    if note_id:
        conditions.append(RagIndexState.note_id == note_id)
    states = (
        await session.execute(select(RagIndexState).where(*conditions).order_by(RagIndexState.updated_at.desc()))
    ).scalars().all()
    return {
        "provider": "llamaindex",
        "graphVersion": GRAPH_VERSION,
        "parserVersion": PARSER_VERSION,
        "chunkerVersion": CHUNKER_VERSION,
        "embeddingVersion": embedding_version(),
        "enabled": True,
        "states": [
            {
                "noteId": state.note_id,
                "status": state.status,
                "sourceVersion": state.source_version,
                "nodeCount": state.node_count,
                "errorCode": state.error_code,
                "errorMessage": state.error_message,
                "indexedAt": state.indexed_at.isoformat() if state.indexed_at else None,
                "updatedAt": state.updated_at.isoformat() if state.updated_at else None,
            }
            for state in states
        ],
    }


async def enqueue_rag_rebuild(
    session: AsyncSession,
    user_id: str,
    *,
    note_id: str | None = None,
    force: bool = False,
    limit: int = 500,
) -> list[NoteIndexJob]:
    stmt = select(Note).where(Note.user_id == user_id, Note.deleted_at.is_(None))
    if note_id:
        stmt = stmt.where(Note.id == note_id)
    notes = (
        await session.execute(stmt.order_by(Note.updated_at.desc()).limit(max(1, min(limit, 2000))))
    ).scalars().all()
    return [await create_rag_index_job(session, note, force=force) for note in notes]

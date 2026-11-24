from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlalchemy.ext.asyncio import AsyncSession

from app.config import cfg
from app.models.db import Note, NoteChunk, NoteEmbedding, NoteSection, RagEmbedding
from app.services.embeddings import chunk_embedding_text, content_hash, embed_texts, embedding_enabled
from app.utils import random_id


@dataclass
class EmbeddingIndexStats:
    indexed: int = 0
    reused: int = 0
    skipped: int = 0
    failed: int = 0


@dataclass(frozen=True)
class IndexEmbedding:
    status: str
    embedding: list[float] | None = None
    error_message: str | None = None
    indexed_at: datetime | None = None

    @classmethod
    def from_row(cls, row: NoteEmbedding | RagEmbedding) -> IndexEmbedding:
        return cls(row.status, row.embedding, row.error_message, row.indexed_at)

    def fields(self) -> dict:
        return {
            "provider": cfg.EMBEDDING_PROVIDER,
            "embedding_model": cfg.EMBEDDING_MODEL,
            "embedding_dim": cfg.EMBEDDING_DIMENSIONS,
            "status": self.status,
            "embedding": self.embedding,
            "error_message": self.error_message,
            "indexed_at": self.indexed_at,
        }


def reusable_embedding(row: NoteEmbedding | RagEmbedding) -> bool:
    return (row.status == "indexed" and row.embedding is not None) or (
        row.status == "skipped" and not embedding_enabled()
    )


async def prepare_embeddings(
    inputs: dict[str, str], existing: dict[str, IndexEmbedding] | None = None,
) -> dict[str, IndexEmbedding]:
    results = dict(existing or {})
    missing = [(key, value) for key, value in inputs.items() if key not in results]
    if not embedding_enabled():
        skipped = IndexEmbedding("skipped", error_message="embedding provider is not configured")
        results.update((key, skipped) for key, _ in missing)
        return results

    batch_size = max(1, min(cfg.EMBEDDING_BATCH_SIZE, 20))
    for start in range(0, len(missing), batch_size):
        batch = missing[start : start + batch_size]
        try:
            result = await embed_texts([value for _, value in batch])
            if (
                len(result.embeddings) != len(batch)
                or result.dimensions != cfg.EMBEDDING_DIMENSIONS
                or any(len(vector) != cfg.EMBEDDING_DIMENSIONS for vector in result.embeddings)
            ):
                raise RuntimeError("embedding provider returned an invalid vector size")
            now = datetime.utcnow()
            for (key, _), vector in zip(batch, result.embeddings):
                results[key] = IndexEmbedding("indexed", vector, indexed_at=now)
        except Exception as exc:
            if cfg.EMBEDDING_FAIL_INDEX_ON_ERROR:
                raise
            failed = IndexEmbedding("failed", error_message=str(exc)[:1000])
            results.update((key, failed) for key, _ in batch)
    return results


async def index_note_chunk_embeddings(
    session: AsyncSession,
    note: Note,
    chunks: list[NoteChunk],
    sections_by_id: dict[str, NoteSection],
    reused_count: int = 0,
    *,
    prepared: dict[str, IndexEmbedding] | None = None,
) -> EmbeddingIndexStats:
    stats = EmbeddingIndexStats(reused=reused_count)
    if prepared is None:
        texts = [
            chunk_embedding_text(
                note_title=note.title,
                section_title=sections_by_id[chunk.section_id].title if chunk.section_id in sections_by_id else "",
                chunk_content=chunk.content or "",
                tags=note.tags or [],
            )
            for chunk in chunks
        ]
        prepared = await prepare_embeddings({content_hash(text): text for text in texts})

    for chunk in chunks:
        key = chunk.content_hash
        value = prepared[key]
        session.add(
            NoteEmbedding(
                id=random_id(),
                chunk_id=chunk.id,
                note_id=chunk.note_id,
                section_id=chunk.section_id,
                user_id=chunk.user_id,
                content_hash=key,
                **value.fields(),
            )
        )
        if value.status == "indexed":
            stats.indexed += 1
        elif value.status == "skipped":
            stats.skipped += 1
        else:
            stats.failed += 1

    await session.flush()
    return stats

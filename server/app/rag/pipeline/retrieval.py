from __future__ import annotations

import asyncio
import math
import re
import time
from collections import OrderedDict
from dataclasses import dataclass, field, replace
from typing import Any, Awaitable, Callable

from sqlalchemy import select, text

from app.config import cfg
from app.database import AsyncSessionLocal
from app.models.db import Note, RagIndexState, RagNode
from app.services.embeddings import embed_texts, embedding_enabled
from app.services.note_library import QueryUnderstanding, understand_note_query
from app.services.pgvector import vector_literal



@dataclass
class RetrievalCandidate:
    node_id: str
    note_id: str
    note_title: str
    section_key: str
    section_path: list[str]
    content: str
    content_hash: str
    source_version: str
    start_line: int | None = None
    end_line: int | None = None
    score: float = 0.0
    channels: list[str] = field(default_factory=list)
    scores: dict[str, float] = field(default_factory=dict)


@dataclass
class RetrievalResult:
    query_plan: QueryUnderstanding
    candidates: list[RetrievalCandidate]
    trace: dict[str, Any]


def candidate_to_library_source(candidate: RetrievalCandidate):
    from app.services.note_library import LibrarySource

    return LibrarySource(
        note_id=candidate.note_id,
        note_title=candidate.note_title,
        section_id=candidate.section_key,
        section_title=" / ".join(candidate.section_path) or "正文",
        chunk_id=candidate.node_id,
        source_type="rag_v2_node",
        snippet=candidate.content[:500],
        score=candidate.score,
        score_components=dict(candidate.scores),
        retrieval_channels=list(candidate.channels),
        query_intent=None,
    )


def _line_number(metadata: dict[str, Any] | None, key: str) -> int | None:
    value = (metadata or {}).get(key)
    return int(value) if isinstance(value, int) and value >= 0 else None


def _normalize(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip()).casefold()


def tokenize(value: str) -> list[str]:
    normalized = _normalize(value)
    if not normalized:
        return []
    if cfg.RAG_BM25_TOKENIZER.startswith("jieba"):
        try:
            import jieba

            cut = jieba.cut_for_search(normalized) if cfg.RAG_BM25_TOKENIZER == "jieba_search" else jieba.cut(normalized)
            return [item.strip() for item in cut if item.strip()]
        except ImportError:
            pass
    latin = re.findall(r"[a-z0-9_+#.:-]+", normalized)
    cjk_runs = re.findall(r"[\u3400-\u9fff]+", normalized)
    cjk: list[str] = []
    for run in cjk_runs:
        cjk.extend(list(run))
        cjk.extend(run[index : index + 2] for index in range(max(0, len(run) - 1)))
    return list(dict.fromkeys([*latin, *cjk]))


async def _visible_nodes(user_id: str, note_id: str | None = None) -> list[RetrievalCandidate]:
    async with AsyncSessionLocal() as session:
        stmt = (
            select(RagNode, Note.title)
            .join(Note, Note.id == RagNode.note_id)
            .join(RagIndexState, RagIndexState.note_id == RagNode.note_id)
            .where(
                RagNode.user_id == user_id,
                RagNode.active.is_(True),
                RagIndexState.user_id == user_id,
                RagIndexState.status == "indexed",
                RagNode.source_version == RagIndexState.source_version,
                Note.user_id == user_id,
                Note.deleted_at.is_(None),
            )
        )
        if note_id:
            stmt = stmt.where(RagNode.note_id == note_id)
        rows = (await session.execute(stmt.order_by(RagNode.note_id, RagNode.node_index))).all()
    return [
        RetrievalCandidate(
            node_id=node.id,
            note_id=node.note_id,
            note_title=title,
            section_key=node.section_key,
            section_path=list(node.section_path or []),
            content=node.content,
            content_hash=node.content_hash,
            source_version=node.source_version,
            start_line=_line_number(node.node_metadata, "start_line"),
            end_line=_line_number(node.node_metadata, "end_line"),
        )
        for node, title in rows
    ]


async def title_retrieve(
    user_id: str,
    query_plan: QueryUnderstanding,
    *,
    note_id: str | None = None,
    limit: int = 30,
) -> list[RetrievalCandidate]:
    terms = [term for term in query_plan.terms if term][:8]
    if not terms:
        return []
    conditions: list[str] = []
    params: dict[str, Any] = {"user_id": user_id, "limit": limit}
    for index, term in enumerate(terms):
        key = f"term_{index}"
        params[key] = f"%{term.casefold()}%"
        conditions.append(
            f"(lower(n.title) LIKE :{key} OR lower(coalesce(node.section_path::text, '')) LIKE :{key} "
            f"OR lower(coalesce(n.tags::text, '')) LIKE :{key})"
        )
    note_filter = "AND n.id = :note_id" if note_id else ""
    if note_id:
        params["note_id"] = note_id
    sql = f"""
        SELECT node.id, node.note_id, n.title, node.section_key, node.section_path,
               node.content, node.content_hash, node.source_version, node.node_metadata,
               ({' + '.join(f'CASE WHEN {condition} THEN 1 ELSE 0 END' for condition in conditions)})::float AS raw_score
        FROM rag_v2_nodes node
        JOIN rag_v2_index_states state ON state.note_id = node.note_id
        JOIN notes n ON n.id = node.note_id
        WHERE node.user_id = :user_id
          AND state.user_id = :user_id
          AND n.user_id = :user_id
          AND node.active = true
          AND state.status = 'indexed'
          AND node.source_version = state.source_version
          AND n.deleted_at IS NULL
          {note_filter}
          AND ({' OR '.join(conditions)})
        ORDER BY raw_score DESC, n.updated_at DESC, node.node_index ASC
        LIMIT :limit
    """
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(text(sql), params)).mappings().all()
    return [
        RetrievalCandidate(
            node_id=row["id"],
            note_id=row["note_id"],
            note_title=row["title"],
            section_key=row["section_key"],
            section_path=list(row["section_path"] or []),
            content=row["content"],
            content_hash=row["content_hash"],
            source_version=row["source_version"],
            start_line=_line_number(row["node_metadata"], "start_line"),
            end_line=_line_number(row["node_metadata"], "end_line"),
            score=float(row["raw_score"]),
            channels=["title"],
            scores={"title": float(row["raw_score"])},
        )
        for row in rows
    ]


def _fallback_bm25(query_tokens: list[str], documents: list[list[str]]) -> list[float]:
    if not query_tokens or not documents:
        return [0.0] * len(documents)
    doc_freq: dict[str, int] = {}
    for document in documents:
        for token in set(document):
            doc_freq[token] = doc_freq.get(token, 0) + 1
    avg_length = sum(len(document) for document in documents) / max(1, len(documents))
    scores: list[float] = []
    for document in documents:
        frequencies: dict[str, int] = {}
        for token in document:
            frequencies[token] = frequencies.get(token, 0) + 1
        score = 0.0
        for token in query_tokens:
            frequency = frequencies.get(token, 0)
            if not frequency:
                continue
            inverse = math.log(1 + (len(documents) - doc_freq.get(token, 0) + 0.5) / (doc_freq.get(token, 0) + 0.5))
            denominator = frequency + 1.5 * (1 - 0.75 + 0.75 * len(document) / max(1.0, avg_length))
            score += inverse * frequency * 2.5 / denominator
        scores.append(score)
    return scores


@dataclass
class _Bm25Corpus:
    revision: tuple
    expires_at: float
    nodes: list[RetrievalCandidate]
    tokens: list[list[str]]
    index: Any


_BM25_CORPORA: OrderedDict[tuple[str, str | None], _Bm25Corpus] = OrderedDict()


async def _corpus_revision(user_id: str, note_id: str | None) -> tuple:
    # Check source versions and visible membership without reading chunks.
    query = (
        select(Note.id, Note.title, RagIndexState.source_version, RagIndexState.updated_at)
        .join(RagIndexState, RagIndexState.note_id == Note.id)
        .where(Note.user_id == user_id, Note.deleted_at.is_(None), RagIndexState.user_id == user_id,
               RagIndexState.status == "indexed")
        .order_by(Note.id)
    )
    if note_id:
        query = query.where(Note.id == note_id)
    async with AsyncSessionLocal() as session:
        return tuple((await session.execute(query)).all())


def _build_corpus(nodes: list[RetrievalCandidate], revision: tuple, now: float) -> _Bm25Corpus:
    tokens = [tokenize("\n".join([node.note_title, " / ".join(node.section_path), node.content])) for node in nodes]
    index = None
    if nodes:
        try:
            import bm25s

            text = [" ".join(document) for document in tokens]
            retriever = bm25s.BM25()
            retriever.index(bm25s.tokenize(text, stopwords=[], show_progress=False), show_progress=False)
            index = retriever
        except (ImportError, AttributeError, TypeError, ValueError, IndexError):
            pass
    return _Bm25Corpus(revision, now + cfg.RAG_QUERY_CACHE_TTL_SECONDS, nodes, tokens, index)


def _score_corpus(corpus: _Bm25Corpus, query_tokens: list[str]) -> list[float]:
    if corpus.index is not None and query_tokens:
        try:
            import bm25s

            query = bm25s.tokenize(" ".join(query_tokens), stopwords=[], show_progress=False)
            indexes, values = corpus.index.retrieve(query, k=len(corpus.nodes), show_progress=False)
            scores = [0.0] * len(corpus.nodes)
            for index, value in zip(indexes[0], values[0]):
                scores[int(index)] = float(value)
            return scores
        except (AttributeError, TypeError, ValueError, IndexError):
            pass
    return _fallback_bm25(query_tokens, corpus.tokens)


async def _get_corpus(user_id: str, note_id: str | None) -> _Bm25Corpus:
    key = (user_id, note_id)
    now = time.monotonic()
    enabled = cfg.RAG_QUERY_CACHE_TTL_SECONDS > 0
    revision = await _corpus_revision(user_id, note_id) if enabled else ()
    cached = _BM25_CORPORA.get(key)
    if enabled and cached is not None and cached.expires_at > now and cached.revision == revision:
        _BM25_CORPORA.move_to_end(key)
        return cached
    _BM25_CORPORA.pop(key, None)
    nodes = await _visible_nodes(user_id, note_id=note_id)
    corpus = await asyncio.to_thread(_build_corpus, nodes, revision, now)
    # Keep a bounded amount of document text per process.
    if enabled and len(nodes) <= 6000 and sum(len(node.content) for node in nodes) <= 4_000_000:
        _BM25_CORPORA[key] = corpus
        while len(_BM25_CORPORA) > 4:
            _BM25_CORPORA.popitem(last=False)
    return corpus


async def bm25_retrieve(
    user_id: str,
    query_plan: QueryUnderstanding,
    *,
    note_id: str | None = None,
    limit: int = 40,
) -> list[RetrievalCandidate]:
    corpus = await _get_corpus(user_id, note_id)
    query_tokens = tokenize(" ".join(query_plan.rewritten_queries if cfg.RAG_REWRITE_ENABLED else [query_plan.search_query]))
    scores = await asyncio.to_thread(_score_corpus, corpus, query_tokens)
    informative_query = {token for token in query_tokens if len(token) >= 2}
    ranked = sorted(zip(corpus.nodes, corpus.tokens, scores), key=lambda item: item[2], reverse=True)
    results: list[RetrievalCandidate] = []
    for node, tokens, score in ranked:
        if score <= 0 or (informative_query and not informative_query.intersection(tokens)):
            continue
        results.append(replace(node, score=float(score), channels=["bm25"], scores={"bm25": round(float(score), 6)}))
        if len(results) >= limit:
            break
    return results


async def vector_retrieve(
    user_id: str,
    query_plan: QueryUnderstanding,
    *,
    note_id: str | None = None,
    limit: int = 40,
) -> list[RetrievalCandidate]:
    if not embedding_enabled():
        return []
    result = await embed_texts([query_plan.search_query])
    if not result.embeddings or not result.embeddings[0]:
        return []
    params: dict[str, Any] = {
        "user_id": user_id,
        "query_vector": vector_literal(result.embeddings[0]),
        "provider": result.provider,
        "model": result.model,
        "dimensions": result.dimensions,
        "limit": limit,
    }
    note_filter = "AND node.note_id = :note_id" if note_id else ""
    if note_id:
        params["note_id"] = note_id
    sql = f"""
        SELECT node.id, node.note_id, n.title, node.section_key, node.section_path,
               node.content, node.content_hash, node.source_version, node.node_metadata,
               1 - (embedding.embedding <=> CAST(:query_vector AS vector)) AS similarity
        FROM rag_v2_embeddings embedding
        JOIN rag_v2_nodes node ON node.id = embedding.node_id
        JOIN rag_v2_index_states state ON state.note_id = node.note_id
        JOIN notes n ON n.id = node.note_id
        WHERE embedding.user_id = :user_id
          AND node.user_id = :user_id
          AND state.user_id = :user_id
          AND n.user_id = :user_id
          AND embedding.provider = :provider
          AND embedding.embedding_model = :model
          AND embedding.embedding_dim = :dimensions
          AND embedding.status = 'indexed'
          AND embedding.embedding IS NOT NULL
          AND node.active = true
          AND state.status = 'indexed'
          AND node.source_version = state.source_version
          AND n.deleted_at IS NULL
          {note_filter}
        ORDER BY embedding.embedding <=> CAST(:query_vector AS vector)
        LIMIT :limit
    """
    async with AsyncSessionLocal() as session:
        rows = (await session.execute(text(sql), params)).mappings().all()
    return [
        RetrievalCandidate(
            node_id=row["id"],
            note_id=row["note_id"],
            note_title=row["title"],
            section_key=row["section_key"],
            section_path=list(row["section_path"] or []),
            content=row["content"],
            content_hash=row["content_hash"],
            source_version=row["source_version"],
            start_line=_line_number(row["node_metadata"], "start_line"),
            end_line=_line_number(row["node_metadata"], "end_line"),
            score=float(row["similarity"]),
            channels=["vector"],
            scores={"vector": round(float(row["similarity"]), 6)},
        )
        for row in rows
        if row["similarity"] is not None and float(row["similarity"]) >= cfg.RAG_VECTOR_MIN_SIMILARITY
    ]


def weighted_rrf(
    channels: dict[str, list[RetrievalCandidate]],
    *,
    limit: int,
    k: int | None = None,
) -> list[RetrievalCandidate]:
    weights = {
        "title": cfg.RAG_TITLE_RRF_WEIGHT,
        "bm25": cfg.RAG_BM25_RRF_WEIGHT,
        "vector": cfg.RAG_VECTOR_RRF_WEIGHT,
    }
    rrf_k = max(1, k or cfg.RAG_RRF_K)
    fused: dict[tuple[str, str, str], RetrievalCandidate] = {}
    for channel, candidates in channels.items():
        for rank, candidate in enumerate(candidates, start=1):
            identity = (candidate.note_id, candidate.section_key, candidate.content_hash)
            contribution = weights.get(channel, 1.0) / (rrf_k + rank)
            current = fused.get(identity)
            if current is None:
                current = RetrievalCandidate(**{**candidate.__dict__})
                current.score = 0.0
                current.channels = []
                current.scores = dict(candidate.scores)
                fused[identity] = current
            current.channels = sorted(set([*current.channels, channel]))
            current.scores[f"{channel}Rank"] = rank
            current.scores[f"{channel}Rrf"] = round(contribution, 8)
            current.score += contribution
    result = sorted(fused.values(), key=lambda candidate: candidate.score, reverse=True)
    return result[:limit]


async def _run_channel(
    name: str,
    callback: Callable[[], Awaitable[list[RetrievalCandidate]]],
) -> tuple[str, list[RetrievalCandidate], dict[str, Any]]:
    started = time.perf_counter()
    try:
        result = await asyncio.wait_for(callback(), timeout=cfg.RAG_CHANNEL_TIMEOUT_MS / 1000)
        return name, result, {"status": "ok", "count": len(result), "durationMs": round((time.perf_counter() - started) * 1000, 2)}
    except asyncio.TimeoutError:
        return name, [], {"status": "timeout", "count": 0, "durationMs": round((time.perf_counter() - started) * 1000, 2)}
    except Exception as exc:
        return name, [], {
            "status": "failed",
            "count": 0,
            "durationMs": round((time.perf_counter() - started) * 1000, 2),
            "error": type(exc).__name__,
        }


async def retrieve_candidates(
    user_id: str,
    query: str,
    *,
    note_id: str | None = None,
    limit: int | None = None,
) -> RetrievalResult:
    query_plan = understand_note_query(query, note_id=note_id)
    candidate_limit = max(1, limit or cfg.RAG_CANDIDATE_K)
    callbacks: list[tuple[str, Callable[[], Awaitable[list[RetrievalCandidate]]]]] = [
        ("title", lambda: title_retrieve(user_id, query_plan, note_id=note_id, limit=candidate_limit)),
    ]
    if cfg.RAG_BM25_ENABLED:
        callbacks.append(("bm25", lambda: bm25_retrieve(user_id, query_plan, note_id=note_id, limit=candidate_limit)))
    if cfg.RAG_VECTOR_ENABLED:
        callbacks.append(("vector", lambda: vector_retrieve(user_id, query_plan, note_id=note_id, limit=candidate_limit)))
    results = await asyncio.gather(*[_run_channel(name, callback) for name, callback in callbacks])
    channels = {name: candidates for name, candidates, _trace in results}
    channel_trace = {name: trace for name, _candidates, trace in results}
    fused = weighted_rrf(channels, limit=candidate_limit)
    return RetrievalResult(
        query_plan=query_plan,
        candidates=fused,
        trace={
            "provider": "llamaindex",
            "query": {
                "normalized": query_plan.normalized_query,
                "search": query_plan.search_query,
                "intent": query_plan.intent,
                "scope": query_plan.scope_hint,
                "terms": query_plan.terms,
                "rewrites": query_plan.rewritten_queries if cfg.RAG_REWRITE_ENABLED else [query_plan.search_query],
            },
            "permissionFilter": {"userIdApplied": True, "deletedApplied": True, "sourceVersionApplied": True, "noteIdApplied": bool(note_id)},
            "channels": channel_trace,
            "rrf": {"k": cfg.RAG_RRF_K, "candidateCount": len(fused)},
            "cache": {"bm25Corpus": True, "ttlSeconds": cfg.RAG_QUERY_CACHE_TTL_SECONDS},
        },
    )

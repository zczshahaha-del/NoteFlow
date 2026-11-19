"""Local PostgreSQL benchmark with isolated, automatically removed synthetic notes."""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import math
import statistics
import time
import uuid
from datetime import datetime, timezone
from unittest.mock import patch

import httpx
from sqlalchemy import delete, func, insert, select, update

from app.config import cfg
from app.database import AsyncSessionLocal, engine
from app.deps import CurrentUser, get_current_user
from app.main import app
from app.models.db import Note, RagIndexState, RagNode, User
from app.rag.pipeline import retrieval
from app.services.note_library import understand_note_query


def _stats(samples: list[float]) -> dict:
    return {
        "samples": len(samples),
        "p50Ms": round(statistics.median(samples), 2),
        "p95Ms": round(sorted(samples)[math.ceil(len(samples) * 0.95) - 1], 2),
    }


async def benchmark(count: int, repeats: int) -> dict:
    if cfg.ENVIRONMENT == "production" or cfg.DB_HOST not in {"localhost", "127.0.0.1", "::1"}:
        raise RuntimeError("Run only against local development PostgreSQL")
    owner_id = f"bench-library-{uuid.uuid4().hex}"
    user = CurrentUser(id=owner_id, email=f"{owner_id}@local.test", display_name="合成知识库压测")
    target_id = f"{owner_id}-{count - 1}"
    marker = "NFBENCHZEUS24391"
    fixture_created = False
    result = {"notes": count, "ragChunks": count * 3}
    try:
        async with AsyncSessionLocal.begin() as session:
            session.add(User(id=user.id, email=user.email, display_name=user.display_name))
            await session.flush()
            for start in range(0, count, 100):
                notes, states, chunks = [], [], []
                for index in range(start, min(start + 100, count)):
                    note_id = f"{owner_id}-{index}"
                    title = f"合成笔记 {index:04d}"
                    source = f"source-{index}"
                    body_parts = [
                        f"## Section {part}\n\n" + ("ordinary archive notes about plans, measurements and reports. " * 20)
                        for part in range(3)
                    ]
                    if note_id == target_id:
                        body_parts[-1] += f"\n\n{marker} belongs to the last note."
                    notes.append(dict(id=note_id, user_id=user.id, title=title, content="\n\n".join(body_parts)))
                    states.append(dict(note_id=note_id, user_id=user.id, source_version=source, status="indexed", node_count=3))
                    chunks.extend(dict(
                        id=f"{note_id}-{part}", note_id=note_id, user_id=user.id, section_key=f"section-{part}",
                        node_index=part, section_path=[f"Section {part}"], content=body, content_hash=f"hash-{index}-{part}",
                        source_version=source, parser_version="benchmark", chunker_version="benchmark", active=True,
                        node_metadata={"start_line": part * 4 + 1, "end_line": part * 4 + 4},
                    ) for part, body in enumerate(body_parts))
                await session.execute(insert(Note), notes)
                await session.execute(insert(RagIndexState), states)
                await session.execute(insert(RagNode), chunks)
        fixture_created = True
        with patch.dict(app.dependency_overrides, {get_current_user: lambda: user}):
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://bench") as client:
                for name, url, includes_content in (
                    ("metadataList", "/api/notes?includeDeleted=true&includeContent=false", False),
                    ("legacyFullList", "/api/notes?includeDeleted=true", True),
                ):
                    latencies = []
                    for _ in range(repeats):
                        start = time.perf_counter()
                        response = await client.get(url)
                        latencies.append((time.perf_counter() - start) * 1000)
                        response.raise_for_status()
                    rows = response.json()["notes"]
                    assert len(rows) == count and all(("content" in r) == includes_content for r in rows)
                    result[name] = {**_stats(latencies), "responseBytes": len(response.content)}
                detail = await client.get(f"/api/notes/{target_id}")
                assert marker in detail.json()["note"]["content"]
                literal = []
                for _ in range(repeats):
                    start = time.perf_counter()
                    response = await client.post("/api/notes/search", json={"query": marker, "mode": "literal", "limit": 10})
                    literal.append((time.perf_counter() - start) * 1000)
                    response.raise_for_status()
                    assert {item["noteId"] for item in response.json()["results"]} == {target_id}
                result["literalSearch"] = _stats(literal)

        with patch.object(cfg, "RAG_QUERY_CACHE_TTL_SECONDS", 120), patch.object(retrieval, "_visible_nodes", wraps=retrieval._visible_nodes) as reads:
            warm = []
            for index in range(repeats + 1):
                plan = understand_note_query(f"{marker} probe{index}")
                start = time.perf_counter()
                matches = await retrieval.bm25_retrieve(user.id, plan, limit=8)
                elapsed = (time.perf_counter() - start) * 1000
                assert matches and matches[0].note_id == target_id
                if index:
                    warm.append(elapsed)
                else:
                    result["bm25ColdMs"] = round(elapsed, 2)
            assert reads.await_count == 1, "Warm queries should not reload the full corpus"
            result["bm25WarmSearch"] = _stats(warm)
            async with AsyncSessionLocal.begin() as session:
                await session.execute(update(Note).where(Note.id == target_id, Note.user_id == owner_id).values(
                    deleted_at=datetime.now(timezone.utc).replace(tzinfo=None),
                ))
            assert all(item.note_id != target_id for item in await retrieval.bm25_retrieve(user.id, understand_note_query(marker)))
            result["deletedNoteExcludedAfterWarmSearch"] = True
    finally:
        retrieval._BM25_CORPORA.pop((owner_id, None), None)
        if fixture_created:
            async with AsyncSessionLocal.begin() as session:
                await session.execute(delete(User).where(User.id == owner_id))
                assert not await session.scalar(select(func.count()).select_from(Note).where(Note.user_id == owner_id))
    return result


async def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--notes", type=int, default=1000, choices=range(10, 2001), metavar="10..2000")
    parser.add_argument("--repeats", type=int, default=15, choices=range(3, 101), metavar="3..100")
    args = parser.parse_args()
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("app.services.observability").setLevel(logging.WARNING)
    try:
        print(json.dumps(await benchmark(args.notes, args.repeats), ensure_ascii=False, indent=2))
    finally:
        await engine.dispose()


if __name__ == "__main__":
    asyncio.run(main())

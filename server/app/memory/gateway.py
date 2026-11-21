"""Authenticated memory boundary: source lifecycle, policy and engine coordination."""
from __future__ import annotations

import hashlib
import asyncio
from datetime import datetime, timedelta

from sqlalchemy import select

from app.database import AsyncSessionLocal
from app.memory.control import control, effective_mission, uses_engine, lock_owner, save_control
from app.memory.engine import MemoryChanges, MemoryEngine, MemoryFact, MemoryRecord
from app.providers.memory_registry import configured_engine
from app.memory.policy import classify_memory_content
from app.models.db import MemorySource, IntegrationOutbox, UserSettings
from app.observability.context import current_trace
from app.repositories.outbox import OutboxRepository
from app.utils import random_id


def record(fact: MemoryFact, source, *, provider: str):
    metadata = source.metadata_json or {}
    claim = metadata.get("claim") or {}
    return MemoryRecord(
        id=fact.id, content=claim.get("content", fact.content), source_ref=metadata.get("group", source.id),
        value=claim.get("value"),
        status="pending" if fact.id in metadata.get("pendingIds", []) else fact.status,
        memory_type=metadata.get("memoryType", "personal_info"),
        source=metadata.get("source", "user_message"), scope=metadata.get("scope", "global"),
        created_at=source.created_at, updated_at=fact.updated_at or source.updated_at,
        tags=list(fact.tags), canonical_key=metadata.get("canonicalKey", ""), provider=provider,
        memory_layer=metadata.get("layer", "semantic"),
    )


def public_record(memory):
    from dataclasses import asdict
    item = asdict(memory)
    names = {"memory_type": "memoryType", "source_ref": "sourceRef", "memory_layer": "layer",
        "canonical_key": "canonicalKey", "created_at": "createdAt", "updated_at": "updatedAt",
        "deleted_at": "deletedAt", "last_used_at": "lastUsedAt", "access_count": "accessCount"}
    return {names.get(k, k): v.isoformat() if isinstance(v, datetime) else v for k, v in item.items() if k != "expires_at"} | {
        "value": memory.value if memory.value is not None else memory.content,
    }


class MemoryGateway:
    def __init__(self, engine: MemoryEngine | None = None):
        self.engine = engine if engine is not None else configured_engine()

    async def preview(self, user_id, *, mission, text):
        if not classify_memory_content(text).allowed:
            raise ValueError("该示例包含不适合保存的信息。")
        test_owner = f"preview:{user_id}:{random_id()}"
        try:
            await self.engine.ensure_bank(test_owner, mission.strip())
            await self.engine.retain(test_owner, content=text, source_id="preview", metadata={"source": "synthetic_preview"})
            return {"facts": [fact.content for fact in await self.engine.list(test_owner)
                              if fact.status == "active" and not fact.derived]}
        finally:
            await self.engine.delete_bank(test_owner)

    async def wait(self, user_id, source_id, *, seconds=20):
        deadline = asyncio.get_running_loop().time() + seconds
        while True:
            async with AsyncSessionLocal() as session:
                source = await session.get(MemorySource, source_id)
                if source is None or source.user_id != user_id:
                    raise LookupError("source not found")
                if source.status in {"organized", "stored"}:
                    ids = set((source.metadata_json or {}).get("children", [source_id]))
                    return [r for r in await self.read(user_id, limit=None) if r.source_ref in ids and r.status == "active"]
                dead = await session.scalar(select(IntegrationOutbox.id).where(
                    IntegrationOutbox.user_id == user_id, IntegrationOutbox.aggregate_id == source_id,
                    IntegrationOutbox.status == "dead"))
                if dead:
                    from app.memory.engine import MemoryUnavailable
                    raise MemoryUnavailable("记忆整理失败，尚未确认保存；请在记忆管理重试。")
                if source.status != "queued" or asyncio.get_running_loop().time() >= deadline:
                    return []
            await asyncio.sleep(0.3)

    async def sources(self, session, user_id):
        return {s.id: s for s in (await session.scalars(select(MemorySource).where(
            MemorySource.user_id == user_id, MemorySource.status == "stored").execution_options(populate_existing=True))).all()}

    async def read(self, user_id, *, query=None, memory_types=(), scopes=(), canonical_keys=(), layers=(),
                   limit=50, include_archived=True, historical=False, direct_read=False):
        async with AsyncSessionLocal() as session:
            settings, initial_state = await control(session, user_id)
            if query is not None and not settings.memory_enabled:
                return []
            sources = await self.sources(session, user_id)
            if not sources:
                return []
            # An index hit only nominates a source. Read governed facts from a
            # fresh SQL snapshot after the external calls (including concurrent deletion).
            has_legacy = any("claim" not in (s.metadata_json or {}) for s in sources.values())
            if query is not None:
                hits = await self.engine.recall(user_id, query)
            else:
                hits = await self.engine.list(user_id) if has_legacy else []
            legacy, ranked = [], []
            for hit in hits:
                if hit.derived:
                    continue
                source = sources.get(hit.source_id)
                if query is not None and (source is None or "claim" not in (source.metadata_json or {})):
                    try:
                        hit = await self.engine.get(user_id, hit.id)
                    except LookupError:
                        continue
                ranked.append(hit.source_id)
                if hit.source_id in sources and not (sources[hit.source_id].metadata_json or {}).get("claim"):
                    legacy.append(hit)

            sources = await self.sources(session, user_id)
            if query is not None:
                current = (await session.execute(select(UserSettings.memory_enabled, UserSettings.preferences).where(
                    UserSettings.user_id == user_id))).first()
                if current is None or not current[0] or (current[1] or {}).get("memoryControl", {}).get("generation", 0) != initial_state.get("generation", 0):
                    return []
            order = dict.fromkeys(ranked)
            for source in sources.values():
                meta = source.metadata_json or {}
                if query is None or meta.get("canonicalKey") in canonical_keys:
                    order[source.id] = None
            if historical:
                # Follow the revision chain back from relevant hits even when
                # the index only returns the latest city, project, or preference.
                successors = set(order)
                while successors:
                    prior = {s.id for s in sources.values()
                             if successors.intersection((s.metadata_json or {}).get("historical", {}).values())}
                    successors = prior - order.keys()
                    order.update(dict.fromkeys(successors))
            rows = []
            for source_id in order:
                source = sources.get(source_id)
                claim = (source.metadata_json or {}).get("claim") if source else None
                if claim:
                    rows.append(MemoryFact(claim["id"], claim["content"], source_id,
                                           "active" if claim.get("active", True) else "archived"))
            exact = {s.id for s in sources.values() if (s.metadata_json or {}).get("canonicalKey") in canonical_keys}
            result = []
            for row in [*rows, *legacy]:
                source = sources.get(row.source_id)
                if not source or row.derived or row.status == "unavailable":
                    continue
                meta = source.metadata_json or {}
                is_past = row.id in meta.get("hiddenIds", [])
                if is_past and not (historical and meta.get("historical", {}).get(row.id) in sources):
                    continue
                item = record(row, source, provider=self.engine.name)
                if not classify_memory_content(item.content).allowed:
                    continue
                if (query is not None or not include_archived) and item.status != "active":
                    continue
                # A request to inspect one's own preferences may span task scopes.
                # Applying a preference to a task still enforces that task's scope.
                inspecting = direct_read and query is not None and bool(meta.get("claim"))
                if scopes and not (inspecting and set(scopes) == {"global"}) and item.scope not in {"global", *scopes}:
                    continue
                if not inspecting and memory_types and (query is None or meta.get("memoryType")) and item.memory_type not in memory_types:
                    continue
                if not inspecting and layers and meta.get("layer") and item.memory_layer not in layers:
                    continue
                if canonical_keys and item.canonical_key and not any(
                    item.canonical_key == key or item.canonical_key.startswith(key + ".")
                    or key.startswith(item.canonical_key + ".") for key in canonical_keys
                ):
                    # An open-ended planner key can differ from the stored vocabulary.
                    # A ranked claim remains eligible when no such field exists.
                    if not inspecting or (exact and not is_past):
                        continue
                if is_past:
                    item.content = "曾经有效（后来已更改）：" + item.content
                result.append(item)
            return result if limit is None else result[:max(1, min(limit, 500))]

    async def submit_turn(self, user_id, content, *, automatic):
        trace = current_trace()
        return await self.submit(user_id, content, source_key=trace.run_id or None,
            metadata={"dialogue": True, "automatic": automatic, "runId": trace.run_id,
                      "sessionId": trace.session_id, "source": "user_message" if automatic else "user_explicit"})

    async def submit(self, user_id, content, *, source_key=None, metadata=None):
        text = content.strip()
        if len(text) > 8000:
            raise ValueError("记忆输入过长，请缩短到 8000 字以内。")
        decision = classify_memory_content(text)
        mixed_turn = (metadata or {}).get("dialogue") and decision.reason == "temporary_instruction"
        if not decision.allowed and not mixed_turn:
            return None
        trace = current_trace()
        source_key = source_key or trace.run_id or trace.request_id or random_id()
        source_key = hashlib.sha256(source_key.encode()).hexdigest()
        async with AsyncSessionLocal() as session:
            await lock_owner(session, user_id)
            settings, state = await control(session, user_id)
            if not settings.memory_enabled:
                return None
            existing = await session.scalar(select(MemorySource).where(
                MemorySource.user_id == user_id, MemorySource.source_key == source_key))
            if existing:
                return None if existing.status in {"deleted", "skipped"} else existing.id
            source = MemorySource(id=random_id(), user_id=user_id, source_key=source_key,
                engine=self.engine.name,
                generation=state.get("generation", 0), rule_version=state.get("ruleVersion", 1),
                metadata_json=metadata or {}, status="queued")
            session.add(source)
            await session.flush()
            job = await OutboxRepository(session).enqueue(topic="memory.retain.requested", aggregate_type="memory_source",
                aggregate_id=source.id, user_id=user_id, idempotency_key=f"retain:{user_id}:{source_key}",
                payload={"content": text}, max_attempts=3, trace_id=trace.trace_id or None)
            if (metadata or {}).get("automatic"):
                job.available_at = datetime.utcnow() + timedelta(seconds=15)
            await session.commit()
        from app.workers.outbox_worker import notify_outbox_worker
        notify_outbox_worker()
        return source.id

    async def process(self, session, item):
        await lock_owner(session, item.user_id)
        settings, state = await control(session, item.user_id)
        source = await session.get(MemorySource, item.aggregate_id)
        if not source or source.user_id != item.user_id or source.status != "queued":
            item.payload = {}
            return
        if (not settings.memory_enabled or not await uses_engine(item.user_id)
            or source.generation != state.get("generation", 0) or source.rule_version != state.get("ruleVersion", 1)):
            source.status = "skipped"
            item.payload = {}
            return
        if (source.metadata_json or {}).get("dialogue"):
            from app.memory.resolution import organize
            # The worker records failure and retries in its outer transaction.
            async with session.begin_nested():
                await organize(self, session, item, source, state)
            return
        if state.get("contextSince") and source.created_at.isoformat() <= state["contextSince"]:
            source.status = "skipped"
            item.payload = {}
            return
        mission = effective_mission(state)
        await self.engine.ensure_bank(item.user_id, mission)
        # Retry of a noncommitted source can replace only its own document.
        # Published/curated sources are never reprocessed by this worker.
        await self.engine.delete_source(item.user_id, source.id)
        await self.engine.retain(item.user_id, content=item.payload["content"], source_id=source.id,
                                 metadata={"source": "user_message", "rule_version": source.rule_version})
        rows = [fact for fact in await self.engine.list(item.user_id, source_id=source.id)
                if fact.source_id == source.id and not fact.derived and fact.status != "unavailable"]
        published = await self.sources(session, item.user_id)
        previous = await self.engine.list(item.user_id, status="active") if published else []
        normalize = lambda text: "".join(text.split()).casefold()
        known = {normalize(fact.content) for fact in previous if fact.source_id in published and not fact.derived}
        hidden, pending = [], []
        for row in rows:
            duplicate = normalize(row.content) in known
            if duplicate or (state.get("reviewNewFacts", False) and (source.metadata_json or {}).get("source") != "user_explicit"):
                await self.engine.curate(item.user_id, row.id, MemoryChanges(status="archived",
                    reason="duplicate" if duplicate else "pending user review: protect manual curation"))
                (hidden if duplicate else pending).append(row.id)
        source.metadata_json = {**(source.metadata_json or {}), "hiddenIds": hidden, "pendingIds": pending}
        source.status = "stored" if rows else "empty"
        item.payload = {}

    async def curated(self, user_id, memory_id, *, content=None, status=None):
        if content is None and status is None:
            raise ValueError("请选择要修改的内容或状态。")
        async with AsyncSessionLocal() as session:
            await lock_owner(session, user_id)
            row = await self.engine.get(user_id, memory_id)
            sources = await self.sources(session, user_id)
            source = sources.get(row.source_id)
            if source is None:
                raise LookupError("memory not found")
            if content is not None:
                if not content.strip() or len(content) > 2000 or not classify_memory_content(content).allowed:
                    raise ValueError("记忆内容为空、过长或包含不适合保存的信息。")
                settings, state = await control(session, user_id)
                # Subsequent automatic facts require confirmation after a manual
                # correction, rather than silently introducing a contradictory copy.
                if not (source.metadata_json or {}).get("claim"):
                    save_control(settings, {**state, "reviewNewFacts": True})
            if status is not None:
                if status not in {"active", "archived"}:
                    raise ValueError("这里只支持启用或停用；彻底删除请使用删除来源。")
            changes = MemoryChanges(content=content.strip() if content is not None else None,
                                    status=status, reason="NoteFlow user curation")
            meta = dict(source.metadata_json or {})
            if memory_id in meta.get("hiddenIds", []):
                raise LookupError("memory not found")
            row = await self.engine.curate(user_id, memory_id, changes)
            if claim := meta.get("claim"):
                meta["claim"] = {**claim, "value": content.strip() if content is not None else claim.get("value"),
                                 "content": content.strip() if content is not None else claim["content"],
                                 "active": status == "active" if status else claim.get("active", True)}
                meta["effectiveAt"] = datetime.utcnow().isoformat()
                source.metadata_json = meta
            if status is not None:
                source.metadata_json = {**(source.metadata_json or {}), "pendingIds": [
                    key for key in (source.metadata_json or {}).get("pendingIds", []) if key != memory_id]}
            await session.commit()
            return record(row, source, provider=self.engine.name)

    async def _deletion_group(self, session, user_id, memory_id):
        row = await self.engine.get(user_id, memory_id)
        sources = await self.sources(session, user_id)
        chosen = sources.get(row.source_id)
        if chosen is None or row.id in (chosen.metadata_json or {}).get("hiddenIds", []):
            raise LookupError("memory not found")
        group = (chosen.metadata_json or {}).get("group", chosen.id)
        return row, chosen, [s for s in sources.values() if (s.metadata_json or {}).get("group", s.id) == group]

    async def delete_impact(self, user_id, memory_id):
        async with AsyncSessionLocal() as session:
            _, chosen, group = await self._deletion_group(session, user_id, memory_id)
            records = []
            for source in group:
                hidden = (source.metadata_json or {}).get("hiddenIds", [])
                rows = await self.engine.list(user_id, source_id=source.id)
                records.extend(public_record(record(r, source, provider=self.engine.name)) for r in rows
                               if r.source_id == source.id and not r.derived and r.status != "unavailable"
                               and r.id not in hidden)
            return {"sourceId": (chosen.metadata_json or {}).get("group", chosen.id), "memories": records}

    async def delete(self, user_id, memory_id):
        from app.memory.portability import clear_portable_source, copied_source_memories

        async with AsyncSessionLocal() as session:
            await lock_owner(session, user_id)
            row, source, group = await self._deletion_group(session, user_id, memory_id)
            result = record(row, source, provider=self.engine.name)
            # Also erase dedicated documents for superseded versions. A legacy
            # document may own other facts, so its per-fact exclusion stays in force.
            sources = await self.sources(session, user_id)
            ids = {s.id for s in group}
            ancestors = list(ids)
            while ancestors:
                child = sources[ancestors.pop()]
                for prior_id in (child.metadata_json or {}).get("predecessors", []):
                    if prior_id in sources and prior_id not in ids:
                        ids.add(prior_id)
                        ancestors.append(prior_id)
            # Old records lack message provenance, so use a conservative time
            # barrier for those. New records only fence their own evidence/field.
            if any(not sources[sid].metadata_json.get("turn") for sid in ids):
                settings, state = await control(session, user_id)
                save_control(settings, {**state, "contextSince": datetime.utcnow().isoformat()})
            for source_id in ids:
                source = sources[source_id]
                for copy in await copied_source_memories(session, user_id, source_id):
                    copy.status = "deleted"
                    copy.deleted_at = datetime.utcnow()
                source.status = "deleted"
                clear_portable_source(source)
                meta = source.metadata_json
                turn = await session.get(MemorySource, meta["turn"]) if meta.get("turn") else None
                meta = {**meta, "evidenceIds": meta.get("contextIds", []),
                        "evidenceRuns": [turn.metadata_json["runId"]] if turn and turn.metadata_json.get("runId") else []}
                source.metadata_json = {k: v for k, v in meta.items() if k not in {"claim", "evidence", "contextIds"}}
                await OutboxRepository(session).enqueue(topic="memory.source.delete", aggregate_type="memory_source",
                    aggregate_id=source_id, user_id=user_id, idempotency_key=f"delete-source:{source_id}", payload={}, max_attempts=3)
            await session.commit()
        for source_id in ids:
            await self.engine.delete_source(user_id, source_id)
        result.status = "deleted"
        return result

    async def graph(self, user_id):
        records = await self.read(user_id, limit=500, include_archived=False)
        allowed = {r.id: r for r in records}
        graph = await self.engine.graph(user_id)
        nodes = [{"id": node.id, "content": allowed[node.id].content, "sourceRef": allowed[node.id].source_ref}
                 for node in graph.nodes if node.id in allowed]
        visible = {node["id"] for node in nodes}
        edges = [{"source": edge.source, "target": edge.target, "relation": edge.relation}
                 for edge in graph.edges if edge.source in visible and edge.target in visible]
        return {"nodes": nodes, "edges": edges}

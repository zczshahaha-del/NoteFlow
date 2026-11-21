"""Resolve a committed chat turn against its conversation and governed prior facts."""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timedelta
from typing import Literal

from pydantic import BaseModel, Field, ValidationError
from sqlalchemy import select

from app.memory.control import effective_mission
from app.memory.domain import MEMORY_SCOPES, MEMORY_TYPES, is_single_value_key
from app.memory.engine import MemoryUnavailable
from app.memory.policy import classify_memory_content
from app.models.db import ChatMessage, MemorySource
from app.services.ai import complete_chat
from app.services.prompt_templates import render_prompt


class Change(BaseModel):
    content: str = Field(max_length=1200)
    anchorId: str = ""
    label: str = Field(min_length=1, max_length=80)
    value: str = Field(max_length=700)
    key: str = Field(pattern=r"^[a-z][a-z0-9_]*(\.[a-z0-9_]+)+$", max_length=100)
    type: str
    scope: str = "global"
    evidence: str = Field(min_length=1, max_length=1000)
    contextIds: list[str] = Field(default_factory=list, max_length=20)
    replaces: list[str] = Field(default_factory=list, max_length=32)
    cardinality: Literal["single", "multiple"] = "multiple"
    previous: Literal["none", "historical", "mistaken"] = "none"


class Resolution(BaseModel):
    changes: list[Change] = Field(max_length=12)


async def resolve(message, context, known, mission):
    data = {"message": message, "context": context, "known": known}
    messages = [
        {"role": "system", "content": render_prompt("memory/resolve", mission=mission,
            schema=json.dumps(Resolution.model_json_schema(), ensure_ascii=False))},
        {"role": "user", "content": json.dumps(data, ensure_ascii=False)},
    ]
    for attempt in range(2):
        answer = await complete_chat(messages, temperature=0, max_tokens=3000,
                                     response_format={"type": "json_object"})
        try:
            return Resolution.model_validate_json(answer).changes
        except ValidationError as error:
            if attempt:
                raise MemoryUnavailable("记忆整理结果格式异常。") from None
            fields = [{"field": e["loc"], "error": e["type"]} for e in error.errors()]
            messages.extend([{"role": "assistant", "content": answer[:7000]},
                             {"role": "user", "content": "按给定 JSON Schema 修正字段：" + json.dumps(fields)}])


async def conversation(session, source, text, cutoff, excluded_runs, excluded_ids):
    """Only server-owned messages can supply context for a personal fact."""
    meta = source.metadata_json
    if not meta.get("runId"):
        return text, [], source.created_at
    anchor = await session.scalar(select(ChatMessage).where(
        ChatMessage.user_id == source.user_id, ChatMessage.run_id == meta["runId"],
        ChatMessage.session_id == meta["sessionId"], ChatMessage.role == "user"))
    if not anchor or anchor.run_id in excluded_runs or anchor.id in excluded_ids or anchor.text.strip() != text or (anchor.metadata_json or {}).get("retryOfRunId"):
        return "", [], source.created_at
    if cutoff and anchor.created_at <= cutoff:
        return "", [], anchor.created_at
    base = select(ChatMessage).where(ChatMessage.user_id == source.user_id,
        ChatMessage.session_id == anchor.session_id)
    if cutoff:
        base = base.where(ChatMessage.created_at > cutoff)
    before = (await session.scalars(base.where(ChatMessage.created_at < anchor.created_at)
        .order_by(ChatMessage.created_at.desc()).limit(32))).all()
    after = (await session.scalars(base.where(ChatMessage.created_at > anchor.created_at)
        .order_by(ChatMessage.created_at).limit(8))).all() if meta.get("automatic") else []
    context = []
    for rows, position, budget in ((before, "before", 10000), (after, "after", 2400)):
        group = []
        for row in rows:
            if row.run_id in excluded_runs or row.id in excluded_ids:
                continue
            if row.role not in {"assistant", "user"} or (row.metadata_json or {}).get("replyState") in {"failed", "cancelled"}:
                continue
            snippet = row.text[:min(1200, budget)]
            if not snippet or classify_memory_content(snippet).sensitivity != "normal":
                continue
            group.append({"id": row.id, "role": row.role, "text": snippet, "position": position})
            budget -= len(snippet)
            if budget <= 0:
                break
        context.extend(reversed(group) if position == "before" else group)
    return text, context, anchor.created_at


def matched_prior(fact, sources):
    source = sources.get(fact.source_id)
    if not source or fact.derived or fact.status != "active":
        return None
    meta = source.metadata_json or {}
    if fact.id in (*meta.get("hiddenIds", []), *meta.get("pendingIds", [])):
        return None
    claim = meta.get("claim") or {}
    if claim and claim["id"] != fact.id:
        return None
    return {"id": fact.id, "key": meta.get("canonicalKey", ""), "scope": meta.get("scope", "global"),
            "content": claim.get("content", fact.content)[:1200]}


async def input_segment(session, source, text, state):
    meta = source.metadata_json
    cutoff = datetime.fromisoformat(state["contextSince"]) if state.get("contextSince") else None
    tombstones = (await session.scalars(select(MemorySource).where(
        MemorySource.user_id == source.user_id, MemorySource.status == "deleted"))).all()
    excluded_runs, excluded_ids, deleted_fields = set(), set(), {}
    for tombstone in tombstones:
        removed = tombstone.metadata_json or {}
        excluded_runs.update(removed.get("evidenceRuns", []))
        excluded_ids.update(removed.get("evidenceIds", []))
        if removed.get("canonicalKey"):
            field = (removed["canonicalKey"], removed.get("scope", "global"))
            deleted_fields[field] = max(deleted_fields.get(field, datetime.min), tombstone.updated_at)
    inputs = [(source, text)]
    if meta.get("automatic") and meta.get("sessionId"):
        # Any queued turns in this short segment share one extraction pass. All
        # workers take the owner lock before processing, including explicit saves.
        others = (await session.scalars(select(MemorySource).where(
            MemorySource.user_id == source.user_id, MemorySource.status == "queued",
            MemorySource.id != source.id, MemorySource.generation == source.generation,
            MemorySource.rule_version == source.rule_version,
            MemorySource.metadata_json["sessionId"].as_string() == meta["sessionId"],
            MemorySource.metadata_json["automatic"].as_boolean().is_(True),
            MemorySource.created_at.between(source.created_at - timedelta(seconds=60),
                                            source.created_at + timedelta(seconds=60))
        ).order_by(MemorySource.created_at).limit(7))).all()
        for other in others:
            original = await session.scalar(select(ChatMessage.text).where(
                ChatMessage.user_id == source.user_id, ChatMessage.role == "user",
                ChatMessage.session_id == meta["sessionId"], ChatMessage.run_id == other.metadata_json["runId"]))
            if original:
                inputs.append((other, original.strip()))
    anchors = {}
    for origin, raw in inputs:
        message, context, moment = await conversation(session, origin, raw, cutoff, excluded_runs, excluded_ids)
        origin.status = "empty"
        if message and (not cutoff or moment > cutoff):
            anchors[origin.id] = (origin, message, context, moment)
    return anchors, deleted_fields


async def prior_facts(gateway, session, owner, query):
    sources = await gateway.sources(session, owner)
    lookup = {}
    known = {}
    for old in sorted(sources.values(), key=lambda s: s.updated_at, reverse=True):
        old_meta = old.metadata_json or {}
        claim = old_meta.get("claim")
        if not claim or not claim.get("active", True) or claim["id"] in old_meta.get("hiddenIds", []):
            continue
        lookup[claim["id"]] = old
        if len(known) < 32:
            known[claim["id"]] = {"id": claim["id"], "key": old_meta["canonicalKey"],
                                  "scope": old_meta["scope"], "content": claim["content"]}
    if sources:
        for hit in await gateway.engine.recall(owner, query):
            if hit.derived:
                continue
            if hit.source_id not in sources:
                try:
                    hit = await gateway.engine.get(owner, hit.id)
                except LookupError:
                    continue
            prior = matched_prior(hit, sources)
            if prior and classify_memory_content(prior["content"]).allowed:
                known[hit.id], lookup[hit.id] = prior, sources[hit.source_id]

    # Fetch referenced snippets in one owner-scoped query. They disambiguate
    # confirmations such as "yes, always" from an earlier conversation.
    evidence_ids = {mid for fact_id in known for mid in lookup[fact_id].metadata_json.get("contextIds", [])}
    evidence = {}
    if evidence_ids:
        rows = (await session.scalars(select(ChatMessage).where(
            ChatMessage.user_id == owner, ChatMessage.id.in_(evidence_ids)))).all()
        evidence = {r.id: {"role": r.role, "text": r.text[:300]} for r in rows
                    if classify_memory_content(r.text[:300]).sensitivity == "normal"}

    for fact_id, prior in known.items():
        meta = lookup[fact_id].metadata_json or {}
        if meta.get("evidence"):
            prior["evidence"] = meta["evidence"][:400]
            prior["effectiveAt"] = meta.get("effectiveAt")
            prior["context"] = [evidence[mid] for mid in meta.get("contextIds", [])[-3:] if mid in evidence]
    return known, lookup


async def organize(gateway, session, item, source, state):
    anchors, deleted_fields = await input_segment(session, source, item.payload["content"], state)
    if not anchors:
        item.payload = {}
        return
    segments = sorted(anchors.values(), key=lambda segment: segment[3])
    messages = [{"id": origin.id, "text": message} for origin, message, _, _ in segments]
    context = {entry["id"]: entry for _, _, entries, _ in segments for entry in entries}
    query = "\n".join(message["text"] for message in messages)[-3000:]
    known, lookup = await prior_facts(gateway, session, source.user_id, query)
    mission = effective_mission(state)
    changes = await resolve(messages if len(messages) > 1 else messages[0]["text"],
                            list(context.values()), list(known.values()), mission)
    groups = {}
    saved = {origin_id: [] for origin_id in anchors}
    retired = {origin_id: 0 for origin_id in anchors}
    for i, change in enumerate(changes):
        anchor_id = next(iter(anchors)) if len(anchors) == 1 else change.anchorId
        if anchor_id not in anchors:
            continue
        origin, message, entries, moment = anchors[anchor_id]
        if origin.metadata_json.get("manual"):
            change.scope = origin.metadata_json["scope"]
            change.type = origin.metadata_json["memoryType"]
        valid_context = {entry["id"] for entry in entries if entry["position"] == "before"}
        if (change.evidence not in message or not set(change.contextIds) <= valid_context
            or change.type not in MEMORY_TYPES or change.scope not in MEMORY_SCOPES
            or not set(change.replaces) <= known.keys()
            or (change.content and not classify_memory_content(change.content).allowed)):
            continue
        if moment <= deleted_fields.get((change.key, change.scope), datetime.min):
            continue
        replacements = set(change.replaces)
        if change.cardinality == "single" or is_single_value_key(change.key):
            replacements.update(fact_id for fact_id, old in lookup.items()
                if old.metadata_json.get("canonicalKey") == change.key
                and old.metadata_json.get("scope", "global") == change.scope)
        targets = [(fact_id, lookup[fact_id]) for fact_id in replacements
                   if lookup[fact_id].metadata_json.get("scope", change.scope) == change.scope]
        # A delayed earlier turn must not supersede a more recent statement or manual edit.
        if any((s.metadata_json.get("effectiveAt") or s.created_at.isoformat()) > moment.isoformat()
               for _, s in targets):
            continue
        if change.content:
            for _, old in targets:
                old_value = (old.metadata_json.get("claim") or {}).get("value")
                if old_value and old_value in change.content and old_value not in change.value:
                    change.content = f"{change.label}：{change.value}"
                    break
            if not change.value or not classify_memory_content(change.value).allowed:
                continue
            digest = hashlib.sha256(f"{origin.id}/{i}".encode()).hexdigest()
            key = digest[:40]
            # The stable document ID makes external writes safe to retry after SQL rollback.
            await gateway.engine.ensure_bank(source.user_id, effective_mission(state))
            await gateway.engine.delete_source(source.user_id, key)
            await gateway.engine.retain(source.user_id, content=change.content, source_id=key,
                                        metadata={"source": "confirmed_dialogue"})
            facts = [f for f in await gateway.engine.list(source.user_id, source_id=key)
                     if f.source_id == key and not f.derived and f.status == "active"]
            if not facts:
                raise MemoryUnavailable("记忆索引尚未形成，请重试。")
            fact = facts[0]
            # Keep dependent statements from the same sentence one visible deletion unit.
            sentence = len(re.findall(r"[。！？!?\n]", message[:message.index(change.evidence)]))
            siblings = groups.setdefault((origin.id, sentence), [])
            group = next((group_id for sibling, group_id in siblings
                          if change.key == sibling.key or
                          (change.value in sibling.content and sibling.value in change.content)), key)
            siblings.append((change, group))
            predecessors = [old.id for _, old in targets if (old.metadata_json or {}).get("claim")]
            child = MemorySource(id=key, user_id=source.user_id, source_key="claim:" + digest,
                engine=source.engine, status="stored", generation=source.generation, rule_version=source.rule_version,
                metadata_json={"source": "user_explicit", "scope": change.scope, "memoryType": change.type,
                    "canonicalKey": change.key, "layer": "episodic" if change.type == "episode" else "semantic",
                    "effectiveAt": moment.isoformat(), "claim": {"id": fact.id, "content": change.content, "value": change.value},
                    "hiddenIds": [f.id for f in facts[1:]], "turn": origin.id,
                    "group": group, "predecessors": predecessors,
                    "evidence": change.evidence, "contextIds": change.contextIds})
            session.add(child)
            saved[origin.id].append(key)
        for fact_id, old in targets:
            old_meta = dict(old.metadata_json or {})
            old_meta["hiddenIds"] = list(set(old_meta.get("hiddenIds", [])) | {fact_id})
            if change.previous == "historical" and change.content:
                old_meta["historical"] = {**old_meta.get("historical", {}), fact_id: key}
            old.metadata_json = old_meta
            retired[origin.id] += 1
            lookup.pop(fact_id, None)
            known.pop(fact_id, None)
        if change.content:
            lookup[fact.id] = child
    for origin_id, children in saved.items():
        origin = anchors[origin_id][0]
        origin.status = "organized" if children or retired[origin_id] else "empty"
        origin.metadata_json = {**origin.metadata_json, "children": children, "retired": retired[origin_id]}
    item.payload = {}

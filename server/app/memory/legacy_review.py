"""Reconcile legacy engine facts against confirmed, sourced dialogue without deleting originals."""
import json
from datetime import datetime, timezone
from typing import Literal

from pydantic import BaseModel, ValidationError

from app.memory.engine import MemoryUnavailable
from app.memory.policy import classify_memory_content
from app.services.ai import complete_chat
from app.services.prompt_templates import render_prompt


class Comparison(BaseModel):
    relations: dict[str, Literal["keep", "mistaken", "historical"]]


async def compare_legacy(confirmed, evidence, earlier):
    items = {str(i): text for i, text in enumerate(earlier)}
    messages = [
        {"role": "system", "content": render_prompt("memory/reconcile-legacy")},
        {"role": "user", "content": json.dumps({
            "confirmed": confirmed, "evidence": evidence, "earlier": items}, ensure_ascii=False)},
    ]
    response = await complete_chat(messages, temperature=0, max_tokens=600,
                                   response_format={"type": "json_object"})
    try:
        results = Comparison.model_validate_json(response).relations
    except ValidationError:
        raise MemoryUnavailable("旧记忆核对结果无效，请稍后重试。") from None
    if results.keys() != items.keys():
        raise MemoryUnavailable("未完整核对旧记忆，请稍后重试。")
    return [results[i] for i in items]


def outstanding_reviews(sources):
    if not any(not s.metadata_json.get("claim") for s in sources.values()):
        return []
    return sorted((s for s in sources.values() if needs_review(s.metadata_json)),
                  key=lambda s: s.metadata_json["effectiveAt"])


def needs_review(meta):
    claim = meta.get("claim") or {}
    return (claim.get("active", True) and claim.get("id") and claim["id"] not in meta.get("hiddenIds", [])
            and meta.get("evidence") and meta.get("effectiveAt") and not meta.get("legacyReviewed"))


async def reconcile_legacy(engine, owner, sources, claims):
    facts = await engine.list(owner, status="active")
    for current in claims:
        meta = current.metadata_json
        cutoff = datetime.fromisoformat(meta["effectiveAt"])
        confirmed = {"content": meta["claim"]["content"], "scope": meta["scope"]}
        evidence = confirmed["content"] if meta.get("manuallyEdited") else meta["evidence"]
        candidates = []
        for fact in facts:
            old = sources.get(fact.source_id)
            if not old or old.metadata_json.get("claim") or fact.derived or fact.status != "active":
                continue
            prior = old.metadata_json
            if prior.get("scope") not in (None, meta["scope"]) or old.created_at > cutoff:
                continue
            edited = fact.updated_at
            if edited and edited.tzinfo:
                edited = edited.astimezone(timezone.utc).replace(tzinfo=None)
            if edited and edited > cutoff:
                continue
            if fact.id in prior.get("hiddenIds", []) or fact.id in prior.get("pendingIds", []):
                continue
            if len(fact.content) <= 2000 and classify_memory_content(fact.content).allowed:
                candidates.append((fact, old))
        for start in range(0, len(candidates), 16):
            batch = candidates[start:start + 16]
            relations = await compare_legacy(confirmed, evidence,
                                             [f.content for f, _ in batch])
            for (fact, old), relation in zip(batch, relations, strict=True):
                if relation == "keep":
                    continue
                prior = dict(old.metadata_json)
                prior["hiddenIds"] = [*prior.get("hiddenIds", []), fact.id]
                if relation == "historical":
                    prior["historical"] = {**prior.get("historical", {}), fact.id: current.id}
                old.metadata_json = prior
        current.metadata_json = {**meta, "legacyReviewed": True}

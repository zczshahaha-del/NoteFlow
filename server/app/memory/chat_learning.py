"""Queue memory learning from a committed chat turn."""
from __future__ import annotations

import hashlib
from datetime import datetime

from sqlalchemy import select

from app.memory.policy import classify_memory_content
from app.models.db import AgentRun, ChatMessage, UserSettings
from app.providers.memory_registry import uses_legacy_index
from app.repositories.outbox import OutboxRepository


async def enqueue_chat_learning(session, run):
    content = run.input_text.strip()
    if not content or len(content) > 8000:
        return False
    decision = classify_memory_content(content)
    if not decision.allowed and decision.reason != "temporary_instruction":
        return False
    settings = await session.get(UserSettings, run.user_id)
    state = ((settings.preferences or {}).get("memoryControl") or {}) if settings else {}
    cutoff = state.get("contextSince")
    if settings and (not settings.memory_enabled or (cutoff and run.started_at <= datetime.fromisoformat(cutoff))):
        return False
    if uses_legacy_index():
        await OutboxRepository(session).enqueue(
            topic="memory.chat.learn", aggregate_type="agent_run", aggregate_id=run.id, user_id=run.user_id,
            idempotency_key=f"chat-learn:{run.id}", trace_id=run.trace_id,
            payload={"generation": state.get("generation", 0), "ruleVersion": state.get("ruleVersion", 1)},
            max_attempts=3,
        )
    else:
        from app.memory.gateway import MemoryGateway

        await MemoryGateway().enqueue_source(
            session, run.user_id, content, source_key=hashlib.sha256(run.id.encode()).hexdigest(),
            state=state, trace_id=run.trace_id,
            metadata={"dialogue": True, "automatic": True, "runId": run.id,
                      "sessionId": run.session_id, "source": "user_message"},
        )
    return True


async def learn_legacy_chat(session, item):
    if not uses_legacy_index():
        return
    from app.memory.agent import save_memory_from_question
    from app.schemas.agent import AgentChatMessageIn

    run = await session.get(AgentRun, item.aggregate_id)
    if not run or run.user_id != item.user_id or run.status != "completed":
        return
    rows = (await session.scalars(select(ChatMessage).where(
        ChatMessage.user_id == run.user_id,
        ChatMessage.session_id == run.session_id,
        ChatMessage.created_at < run.started_at,
    ).order_by(ChatMessage.created_at.desc()).limit(8))).all()
    history = [
        AgentChatMessageIn(role=row.role, text=row.text)
        for row in reversed(rows)
        if row.role in {"user", "assistant"}
        and (row.metadata_json or {}).get("replyState") not in {"failed", "cancelled"}
    ]
    await save_memory_from_question(
        run.user_id, run.input_text, history, explicit=False,
        expected_revision=(item.payload["generation"], item.payload["ruleVersion"]),
    )
    item.payload = {}

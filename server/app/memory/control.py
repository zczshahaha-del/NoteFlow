from __future__ import annotations

import asyncio
import hashlib
from sqlalchemy import text
from app.memory.engine import MemoryUnavailable
from app.services.prompt_templates import render_prompt
from app.services.user_settings import get_or_create_user_settings

def effective_mission(state: dict) -> str:
    return state.get("mission") or render_prompt("memory/default-mission")


async def lock_owner(session, user_id):
    key = int.from_bytes(hashlib.sha256(("memory:" + user_id).encode()).digest()[:8], "big", signed=True)
    clock = asyncio.get_running_loop().time
    deadline = clock() + 4
    # Poll without leaving a lock request queued on PostgreSQL after chat stops.
    while not await session.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": key}):
        remaining = deadline - clock()
        if remaining <= 0:
            raise MemoryUnavailable("记忆正在整理，请稍后再试。")
        await asyncio.sleep(min(0.2, remaining))


async def control(session, user_id):
    settings = await get_or_create_user_settings(session, user_id)
    return settings, dict((settings.preferences or {}).get("memoryControl") or {})


def save_control(settings, value):
    settings.preferences = {**(settings.preferences or {}), "memoryControl": value}


async def uses_engine(user_id):
    # Deployment selects a formal-fact engine or the legacy local-fact/index
    # backend. Old per-account opt-in values never change this boundary.
    from app.providers.memory_registry import uses_legacy_index
    return not uses_legacy_index()

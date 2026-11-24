"""Live multi-session acceptance. Point DB_NAME and --url at the same local test API."""
import argparse
import asyncio
import json
import uuid
from datetime import datetime, timedelta
from urllib.parse import urlsplit

import httpx
from sqlalchemy import delete, select

from app.database import AsyncSessionLocal, engine
from app.memory.control import lock_owner
from app.memory.gateway import MemoryGateway
from app.models.db import ChatMessage, IntegrationOutbox, MemorySource, User, UserSession
from app.providers.hindsight import HindsightProvider
from app.utils import create_token


async def exercise(client, owner):
    async def api(method, path, data=None):
        response = await client.request(method, path, json=data)
        response.raise_for_status()
        return response.json()

    async def new_chat():
        return (await api("POST", "/api/chat-sessions", {}))["session"]["id"]

    async def say(chat, message):
        history = (await api("GET", f"/api/chat-sessions/{chat}/messages"))["messages"][-8:]
        response = await client.post("/api/agent/chat", json={"sessionId": chat, "question": message,
            "history": [{"role": m["role"], "text": m["text"]} for m in history]})
        response.raise_for_status()
        events = [json.loads(line[6:]) for line in response.text.splitlines()
                  if line.startswith("data: ") and line != "data: [DONE]"]
        answer = "".join(e["choices"][0].get("delta", {}).get("content") or ""
                         for e in events if e.get("choices"))
        assert any(e.get("type") == "agent_done" and e["status"] == "completed" for e in events), answer
        print(chat[:6], message, "=>", answer[:170], flush=True)
        return answer

    async def active():
        return [m for m in (await api("GET", "/api/memories?allRecords=true"))["memories"]
                if m["status"] == "active"]

    async def containing(word):
        for _ in range(60):
            for record in await active():
                if word in record["content"]:
                    return record
            await asyncio.sleep(1)
        raise AssertionError("Missing active memory: " + word)

    first, second, third, fourth, fifth = [await new_chat() for _ in range(5)]
    await say(first, "讨论一个写作方案：润色技术笔记时保留原有小标题，并在小标题末尾加上青竹标记。用一句话重复方案，先不用记。")
    await say(first, "对，请记住，以后每次都按刚才的方案来。")
    style = await containing("青竹")
    evidence = (await api("GET", "/api/memory-console/source/" + style["sourceRef"]))["document"]["content"]
    assert "刚才" in evidence and "青竹" in evidence, evidence
    await say(second, "请记住，我目前的常住城市是苏州。")
    assert "青竹" in await say(third, "按我的长期偏好，润色技术笔记要怎样处理小标题？")
    await say(second, "只有这篇笔记去掉小标题，别改我的长期写作习惯。")
    move = "请更新我的长期记忆：我刚搬家，现在常住宁波，以后写地址用新城市。"
    await say(third, move)
    city = await containing("宁波")
    current = await say(fourth, "我现在常住哪个城市？")
    assert "宁波" in current and "苏州" not in current, current
    fill_city = "帮我填一项个人资料，只输出「常住城市：<我的当前常住城市>」。如果没有记录就写未记录。"
    current_in_old_chat = await say(second, fill_city)
    assert "宁波" in current_in_old_chat and "苏州" not in current_in_old_chat, current_in_old_chat
    introduce = "写一句发到同事群的自我介绍，带上我现在常住的城市，没资料就留空。只在这里回复，不创建笔记。"
    introduction = await say(second, introduce)
    assert "宁波" in introduction and "苏州" not in introduction, introduction
    assert "苏州" in await say(fifth, "我搬家前常住哪个城市，你还记得吗？")
    assert all("苏州" not in m["content"] for m in await active())

    impact = await api("GET", "/api/memory-console/impact/" + city["id"])
    assert city["id"] in {m["id"] for m in impact["memories"]}
    await api("DELETE", "/api/memories/" + city["id"] + "?confirmedSource=true")
    # A fresh job ID forces genuine worker execution against the original
    # committed message, rather than a simple idempotency-cache hit.
    async with AsyncSessionLocal() as session:
        original = await session.scalar(select(ChatMessage).where(ChatMessage.user_id == owner,
            ChatMessage.session_id == third, ChatMessage.role == "user", ChatMessage.text == move))
        assert original is not None
    gateway = MemoryGateway()
    replay = await gateway.submit(owner, move, source_key="replay-" + uuid.uuid4().hex,
        metadata={"dialogue": True, "runId": original.run_id, "sessionId": third})
    assert replay and not await gateway.wait(owner, replay, seconds=60)
    async with AsyncSessionLocal() as session:
        assert (await session.get(MemorySource, replay)).status in {"empty", "skipped"}

    deleted_in_old_chat = await say(second, fill_city)
    assert "苏州" not in deleted_in_old_chat and "宁波" not in deleted_in_old_chat, deleted_in_old_chat
    deleted_introduction = await say(second, introduce)
    assert "苏州" not in deleted_introduction and "宁波" not in deleted_introduction, deleted_introduction
    await say(third, "用一句话概括刚才的讨论。")
    await say(fifth, "我平常写技术笔记会先画流程图。这个习惯我保持了好几年。")
    await containing("流程图")
    fresh = await new_chat()
    answer = await say(fresh, "我现在常住哪个城市？")
    assert "苏州" not in answer and "宁波" not in answer, answer
    remaining = await active()
    assert all("苏州" not in m["content"] and "宁波" not in m["content"] for m in remaining)
    assert any("青竹" in m["content"] for m in remaining)
    # Seed the same source format used by pre-dialogue versions, through the
    # real worker. The correction must recognize a provider fact without a key.
    old_label = await gateway.submit(owner, "我整理读书笔记的默认标签是海蓝。",
                                     source_key="legacy-default-label",
                                     metadata={"source": "user_explicit"})
    old_records = await gateway.wait(owner, old_label, seconds=60)
    assert any("海蓝" in r.content and not r.canonical_key for r in old_records), old_records
    await say(second, "纠正一下：海蓝只是之前的测试值。请记住，我整理读书笔记的正式默认标签是松针。")
    label = await say(fresh, "读书笔记用我定的默认标签，应该填什么？")
    assert "松针" in label and "海蓝" not in label, label
    assert all("海蓝" not in m["content"] for m in await active())
    print("Passed six real sessions, source evidence, historical change, mistaken value, and deletion replay.")


async def main(url):
    if urlsplit(url).hostname not in {"localhost", "127.0.0.1"}:
        raise ValueError("This smoke test requires a local API.")
    owner = "memory-dialogue-" + uuid.uuid4().hex
    login = uuid.uuid4().hex
    email = owner + "@example.invalid"
    try:
        async with AsyncSessionLocal() as db:
            db.add(User(id=owner, email=email, display_name="Synthetic", email_verified_at=datetime.utcnow()))
            await db.flush()
            db.add(UserSession(id=login, user_id=owner, expires_at=datetime.utcnow() + timedelta(minutes=20)))
            await db.commit()
        async with httpx.AsyncClient(base_url=url, timeout=135, headers={
            "Authorization": "Bearer " + create_token(owner, email, "Synthetic", login),
        }) as client:
            await exercise(client, owner)
    finally:
        async with AsyncSessionLocal() as db:
            await lock_owner(db, owner)
            await db.execute(delete(IntegrationOutbox).where(IntegrationOutbox.user_id == owner))
            await db.execute(delete(User).where(User.id == owner))
            await db.commit()
        await HindsightProvider().delete_bank(owner)
        await engine.dispose()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", required=True)
    asyncio.run(main(parser.parse_args().url))

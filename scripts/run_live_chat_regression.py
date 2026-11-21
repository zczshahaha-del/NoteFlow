"""Run a fixed NoteFlow chat regression with real models and synthetic data."""
from __future__ import annotations

import asyncio
import json
import re
from uuid import uuid4

from sqlalchemy import delete

from app.agent.langgraph_readonly import invoke_readonly_graph, new_readonly_state
from app.config import cfg
from app.database import AsyncSessionLocal, engine
from app.models.db import Note, User
from app.rag.pipeline.indexer import create_rag_index_job, run_rag_index_job
from app.services.turn_planner import plan_turn_smart


async def main() -> None:
    if cfg.ENVIRONMENT != "development" or cfg.DB_HOST not in {"127.0.0.1", "localhost", "::1"}:
        raise RuntimeError("Use a local development database for the synthetic chat fixtures")
    if not cfg.DEEPSEEK_API_KEY:
        raise RuntimeError("DeepSeek credentials are required for this live regression")
    cfg.AI_CACHE_ENABLED = False
    owner = f"qa-chat-{uuid4().hex}"
    target_id, distractor_id = f"{owner}-lamp", f"{owner}-control"
    results: dict[str, bool] = {}

    async def answer(question: str, *, mode: str, page_state: dict | None = None, plan: dict | None = None) -> dict:
        return await invoke_readonly_graph(new_readonly_state(
            user_id=owner, question=question, mode=mode, page_state=page_state, turn_plan=plan,
            memory_enabled=False,
        ))

    def cited_evidence(state: dict, section: str, marker: str, *, only_target: bool = False) -> bool:
        sources = {source["citationIndex"]: source for source in state.get("sources", [])}
        reply = state.get("answer", "")
        citation_ids = [int(number) for number in re.findall(r"\[(\d+)\]", reply)]
        return (
            not state.get("error_code") and marker in reply and bool(citation_ids)
            and all(citation in sources for citation in citation_ids)
            and any(sources[citation]["noteId"] == target_id and section in sources[citation]["sectionTitle"]
                    and marker in sources[citation]["snippet"] for citation in citation_ids)
            and (not only_target or all(source["noteId"] == target_id for source in sources.values()))
        )

    try:
        async with AsyncSessionLocal.begin() as session:
            session.add(User(id=owner, email=f"{owner}@example.invalid", display_name="合成聊天评测"))
            await session.flush()
            documents = [
                (target_id, "松露灯塔巡检", "# 松露灯塔巡检\n\n## 行前准备\n检查备用电池。\n\n"
                 "## 钥匙记录\n松露灯塔北门的钥匙编号是 QF7736。\n\n"
                 "## 夜班交接\n松露灯塔夜班交接的唯一口令是 CY8029。"),
                (distractor_id, "青柠灯塔巡检", "# 青柠灯塔巡检\n\n## 钥匙记录\n"
                 "青柠灯塔北门的钥匙编号是 PL2851。\n\n## 夜班交接\n青柠灯塔夜班口令是 CK1157。"),
            ]
            for note_id, title, content in documents:
                note = Note(id=note_id, user_id=owner, title=title, content=content, index_status="pending")
                session.add(note)
                await session.flush()
                job = await create_rag_index_job(session, note)
                await run_rag_index_job(session, note, job)
                if job.status != "success":
                    raise RuntimeError(f"synthetic note indexing failed: {job.error_code}")

        state = await answer("依据笔记，松露灯塔北门的钥匙编号是什么？用一句话并标出引用。", mode="ask_notes")
        results["library_correct_note_and_citation"] = cited_evidence(state, "钥匙记录", "QF7736")

        state = await answer(
            "结合全文，松露灯塔夜班交接的口令是什么？用一句话并标出引用。", mode="chat",
            page_state={"currentNoteId": target_id, "contextScope": "selection", "selectedText": "检查备用电池。"},
            plan={"context_sources": ["selected_text"]},
        )
        results["selection_full_note_correct_section_and_citation"] = cited_evidence(
            state, "夜班交接", "CY8029", only_target=True,
        )

        state = await answer("用一句话解释月亮为什么有不同的形状。", mode="chat",
                             page_state={"currentNoteId": target_id, "contextScope": "none"},
                             plan={"context_sources": ["current_note"]})
        results["removed_reference_does_not_read_notes"] = (
            not state.get("error_code") and bool(state.get("answer")) and not state.get("sources")
            and not state.get("rag_called") and not state.get("context_text")
        )

        page_state = {"contextScope": "none", "boundDraftId": "synthetic-draft", "boundDraftTitle": "雨天读书计划"}
        for name, question, intent in (
            ("continue_bound_draft", "把刚才那篇再缩短一点", "draft_revise"),
            ("create_separate_draft", "另写一篇新笔记，主题是整理马克笔", "note_create"),
        ):
            plan = await plan_turn_smart(question=question, mode="chat", page_state=page_state)
            results[name] = (
                plan.primary_intent.value == intent and plan.source in {"llm", "repaired"}
                and (intent != "draft_revise" or bool(plan.intent_parameters.instruction))
            )
    finally:
        async with AsyncSessionLocal.begin() as session:
            await session.execute(delete(User).where(User.id == owner))
        await engine.dispose()

    print(json.dumps({"model": cfg.DEEPSEEK_MODEL, "passed": all(results.values()), "cases": results}, ensure_ascii=False))
    if not all(results.values()):
        raise SystemExit(1)


if __name__ == "__main__":
    asyncio.run(main())

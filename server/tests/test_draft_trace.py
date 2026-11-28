from __future__ import annotations

import unittest
from unittest.mock import AsyncMock, patch

from fastapi import HTTPException
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.ext.asyncio import async_sessionmaker, create_async_engine

from app.deps import CurrentUser
from app.memory.engine import MemoryRecord, MemoryUnavailable
from app.models.db import AgentCheckpoint, AgentRun, AgentToolTrace, ChatSession, User
from app.observability.context import current_trace, trace_scope
from app.rag.service import RagResult
from app.routers import agent
from app.services.ai_application import AIApplicationService
from app.services.draft_trace import DraftRunLink, draft_stage
from app.services.prompts import NoteGenerateRequest


class DraftTraceTest(unittest.IsolatedAsyncioTestCase):
    async def test_generation_records_sources_and_applicable_memory_without_logging_the_text(self):
        service = AIApplicationService()
        service.memory.enabled = AsyncMock(return_value=True)
        service.memory.recall = AsyncMock(return_value=[
            MemoryRecord(id="memory", content="合成写作习惯", memory_type="writing_style"),
        ])
        rag = AsyncMock()
        rag.retrieve.return_value = RagResult("note", "", [
            {"noteTitle": "原笔记", "sectionTitle": "章节", "snippet": "合成笔记正文"},
        ])
        link = DraftRunLink("owner", "run", "session", "chat-trace")
        with (
            trace_scope(request_id="generation-request", trace_id="request-trace"),
            patch("app.services.ai_application.configured_rag_service", return_value=rag),
            patch("app.services.draft_trace.record_tool_trace", AsyncMock()) as record,
        ):
            request = await service.prepare_note_generation(
                NoteGenerateRequest(topic="主题"), user_id="owner", source_note_id="note",
                memory_requested=True, link=link, mode="section",
            )
            self.assertEqual(current_trace().trace_id, "request-trace")
        self.assertIn("合成笔记正文", request.sourceContext)
        self.assertIn("合成写作习惯", request.memoryContext)
        note, memory = [call.kwargs for call in record.await_args_list]
        self.assertEqual((note["action"], note["metadata"]["excerptCount"]), ("read_source_note", 1))
        self.assertEqual((memory["action"], memory["metadata"]["writingStyleCount"]), ("read_writing_memory", 1))
        self.assertTrue(all(event["run_id"] == "run" and event["metadata"]["requestId"] == "generation-request"
                            for event in (note, memory)))
        self.assertNotIn("合成笔记正文", str(note))
        self.assertNotIn("合成写作习惯", str(memory))

    async def test_memory_failure_is_visible_and_generation_can_continue(self):
        service = AIApplicationService()
        service.memory.enabled = AsyncMock(return_value=True)
        service.memory.recall = AsyncMock(side_effect=MemoryUnavailable("合成服务不可用"))
        link = DraftRunLink("owner", "run", "session", "trace")
        with patch("app.services.draft_trace.record_tool_trace", AsyncMock()) as record:
            request = await service.prepare_note_generation(
                NoteGenerateRequest(), user_id="owner", source_note_id=None, memory_requested=True, link=link,
            )
        self.assertEqual(request.memoryContext, "")
        self.assertEqual([call.kwargs["status"] for call in record.await_args_list], ["skipped", "failed"])

    async def test_trace_write_failure_keeps_the_business_error(self):
        link = DraftRunLink("owner", "run", "session", "trace")
        with patch("app.services.draft_trace.record_tool_trace", AsyncMock(side_effect=SQLAlchemyError)) as record:
            with self.assertRaisesRegex(ValueError, "保存失败"):
                async with draft_stage(link, "save_note"):
                    raise ValueError("保存失败")
        self.assertEqual(record.await_args.kwargs["status"], "failed")

    async def test_persisted_trace_is_readable_only_by_the_run_owner(self):
        engine = create_async_engine("sqlite+aiosqlite:///:memory:")
        self.addAsyncCleanup(engine.dispose)
        sessions = async_sessionmaker(engine, expire_on_commit=False)
        async with engine.begin() as db:
            for model in (User, ChatSession, AgentRun, AgentCheckpoint, AgentToolTrace):
                await db.run_sync(model.__table__.create)
        async with sessions.begin() as db:
            db.add(User(id="owner", email="owner@example.invalid", display_name="合成账号"))
            db.add(ChatSession(id="chat", user_id="owner", title="测试"))
            db.add(AgentRun(id="run", session_id="chat", user_id="owner", trace_id="chat-trace"))

        link = DraftRunLink("owner", "run", "chat", "chat-trace")
        with (
            patch("app.services.agent_runtime.AsyncSessionLocal", sessions),
            patch("app.services.agent_checkpoints.AsyncSessionLocal", sessions),
        ):
            async with draft_stage(link, "save_note", draftId="draft") as step:
                step["noteId"] = "note"
            result = await agent.get_run("run", CurrentUser(id="owner", email="owner@example.invalid", display_name="合成账号"))
            with self.assertRaises(HTTPException) as denied:
                await agent.get_run("run", CurrentUser(id="other", email="other@example.invalid", display_name="另一个账号"))
        self.assertEqual(denied.exception.status_code, 404)
        run = result["task"]["run"]
        self.assertEqual((run["traceId"], run["toolTraces"][0]["traceId"]), ("chat-trace", "chat-trace"))
        self.assertEqual(run["toolTraces"][0]["metadata"]["noteId"], "note")

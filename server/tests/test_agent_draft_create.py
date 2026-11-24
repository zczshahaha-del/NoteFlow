from __future__ import annotations

import unittest
from contextlib import asynccontextmanager
from datetime import datetime
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.deps import CurrentUser
from app.routers import agent
from app.schemas.agent import AgentChatPageState, AgentChatPayload
from app.services.turn_planner import (
    ChatMode,
    IntentParameters,
    PlanResult,
    PrimaryIntent,
    TurnPlan,
)


class AgentDraftCreateTest(unittest.IsolatedAsyncioTestCase):
    async def test_follow_up_stays_bound_to_the_draft_and_strips_removed_reference(self) -> None:
        @asynccontextmanager
        async def fake_checkpointer():
            yield object()

        checkpoint = {"id": "checkpoint", "sessionId": "session", "checkpointType": "draft_workspace",
                      "payload": {"seed": "验证草稿", "draftId": "draft-1"}}
        draft = SimpleNamespace(id="draft-1", title="验证草稿", topic="主题",
                                status="assembled", updated_at=datetime(2026, 11, 19))
        plan = TurnPlan(primary_intent=PrimaryIntent.DRAFT_REVISE,
                        intent_parameters=IntentParameters(instruction="缩短正文"), confidence=0.99)
        planner = AsyncMock(return_value={"turn_plan": plan.model_dump(mode="json"), "status": "ready"})
        with (
            patch.object(agent, "postgres_checkpointer", fake_checkpointer),
            patch.object(agent, "graph_waiting_for_clarification", AsyncMock(return_value=False)),
            patch.object(agent, "invoke_turn_graph", planner),
            patch.object(agent, "_bound_draft", AsyncMock(return_value=(checkpoint, draft))),
            patch.object(agent, "_update_agent_run_intent", AsyncMock()),
            patch.object(agent, "_checkpoint_for_plan", AsyncMock(return_value=None)),
            patch.object(agent, "_effective_memory_enabled", AsyncMock(return_value=False)),
            patch.object(agent, "_start_agent_run", AsyncMock(return_value={
                "session_id": "session", "run_id": "run", "route_step_id": "step",
            })),
            patch.object(agent, "_record_tool_trace", AsyncMock(return_value={})),
            patch.object(agent, "_complete_tool_only_run", AsyncMock(return_value={})),
        ):
            response = await agent.chat(
                AgentChatPayload(sessionId="session", question="把刚才那篇再缩短一点", documentContent="私有正文",
                                 pageState=AgentChatPageState(currentNoteId="unreferenced", contextScope="none",
                                                             activeDraftId="draft-1", unsavedContent="私有未保存正文")),
                CurrentUser(id="owner", email="test@example.invalid", display_name="合成用户"),
            )
            frames = "".join([chunk async for chunk in response.body_iterator])
        page_state = planner.await_args.args[0]["page_state"]
        self.assertIsNone(page_state["currentNoteId"])
        self.assertEqual(page_state["unsavedContent"], "")
        self.assertEqual(page_state["boundDraftId"], "draft-1")
        self.assertIn('"action": "revise_body"', frames)
        self.assertIn('"draftId": "draft-1"', frames)
        self.assertNotIn("open_draft_workspace", frames)

    async def test_draft_card_metadata_is_persisted_after_checkpoint_creation(self) -> None:
        await self._check_draft_card(source_title="")

    async def test_sourced_draft_does_not_copy_a_stale_preference_into_requirements(self) -> None:
        await self._check_draft_card(source_title="Docker 巡检笔记")

    async def _check_draft_card(self, *, source_title: str) -> None:
        topic = "Docker 巡检" if source_title else "Python"
        question = (
            f"查我的{source_title}，按我的写作习惯整理一篇新笔记" if source_title else "帮我生成一份 Python 学习笔记"
        )
        planned_requirements = "开头写旧测试标记" if source_title else "生成一份 Python 学习笔记"
        plan = TurnPlan(
            mode=ChatMode.CHAT,
            primary_intent=PrimaryIntent.NOTE_CREATE,
            intent_parameters=IntentParameters(
                topic=topic,
                requirements=planned_requirements,
                source_note_title=source_title,
            ),
            confidence=0.99,
            result=PlanResult.EXECUTE,
            source="llm",
        )

        @asynccontextmanager
        async def fake_checkpointer():
            yield object()

        notes = SimpleNamespace(match_active_titles=AsyncMock(return_value=[
            ("source-note", source_title), ("similar-note", source_title + "备忘"),
        ]))

        checkpoint = {
            "id": "checkpoint-python",
            "runId": "run-python",
            "sessionId": "session-python",
            "checkpointType": "draft_workspace",
            "status": "waiting_user_confirm",
            "payload": {"seed": "Python"},
        }
        create_checkpoint = AsyncMock(return_value=checkpoint)
        complete_run = AsyncMock(
            return_value={
                "type": "tool_trace",
                "toolName": "note_draft_tool",
                "action": "open_draft_workspace",
            }
        )

        with (
            patch.object(agent, "postgres_checkpointer", fake_checkpointer),
            patch.object(agent, "AsyncSessionLocal", fake_checkpointer),
            patch.object(agent, "NoteRepository", return_value=notes),
            patch.object(agent, "graph_waiting_for_clarification", AsyncMock(return_value=False)),
            patch.object(
                agent,
                "invoke_turn_graph",
                AsyncMock(return_value={"turn_plan": plan.model_dump(mode="json"), "status": "ready"}),
            ),
            patch.object(agent, "_update_agent_run_intent", AsyncMock(return_value=None)),
            patch.object(agent, "_checkpoint_for_plan", AsyncMock(return_value=None)),
            patch.object(agent, "_effective_memory_enabled", AsyncMock(return_value=False)),
            patch.object(
                agent,
                "_start_agent_run",
                AsyncMock(
                    return_value={
                        "session_id": "session-python",
                        "run_id": "run-python",
                        "route_step_id": "step-python",
                        "trace_id": "trace-python",
                    }
                ),
            ),
            patch.object(
                agent,
                "_record_tool_trace",
                AsyncMock(return_value={"type": "tool_trace", "toolName": "response_mode", "action": "chat"}),
            ),
            patch.object(agent, "_create_checkpoint", create_checkpoint),
            patch.object(agent, "_complete_tool_only_run", complete_run),
        ):
            response = await agent.chat(
                AgentChatPayload(question=question, mode="chat", memoryEnabled=False),
                CurrentUser(id="user-python", email="test@example.com", display_name="Tester"),
            )
            chunks: list[str] = []
            async for chunk in response.body_iterator:
                chunks.append(chunk.decode() if isinstance(chunk, bytes) else chunk)

        self.assertIn('"action": "open_draft_workspace"', "".join(chunks))
        self.assertNotIn("agent_error", "".join(chunks))
        self.assertNotIn("message_metadata", create_checkpoint.await_args.kwargs)
        expected_requirements = question if source_title else planned_requirements
        self.assertEqual(
            complete_run.await_args.kwargs["message_metadata"],
            {"draftCard": {"seed": topic, "checkpointId": "checkpoint-python", "title": topic,
                "requirements": expected_requirements, "rawRequest": question, "manualOutline": True}},
        )
        checkpoint_payload = create_checkpoint.await_args.kwargs["payload"]
        self.assertEqual(checkpoint_payload["rawRequest"], question)
        self.assertTrue(checkpoint_payload["manualOutline"])
        self.assertEqual(checkpoint_payload["draftBrief"]["requirements"], expected_requirements)
        if source_title:
            notes.match_active_titles.assert_awaited_once_with("user-python", source_title)
            self.assertEqual(checkpoint_payload["sourceNoteId"], "source-note")
            self.assertIn('"sourceNoteId": "source-note"', "".join(chunks))
            self.assertNotIn(planned_requirements, "".join(chunks))


if __name__ == "__main__":
    unittest.main()

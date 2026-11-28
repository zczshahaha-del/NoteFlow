from __future__ import annotations

import asyncio
import uuid
from contextlib import aclosing, asynccontextmanager
from dataclasses import dataclass
from typing import Any, AsyncIterator, Awaitable, Callable, Optional, TypedDict
from urllib.parse import quote

import anyio

from app.agent.contracts import RuntimeEvent
from app.config import cfg
from app.providers.contracts import ChatProviderRequest, ProviderChatMessage
from app.rag.pipeline.context import validate_citation_indexes
from app.services.selection_context import has_selection_scope, selection_context_text, whole_note_id


OPTIONAL_MEMORY_TIMEOUT_SECONDS = 8


class ReadonlyAgentState(TypedDict, total=False):
    user_id: str
    session_id: str
    run_id: str
    trace_id: str
    thread_id: str
    question: str
    mode: str
    history: list[dict[str, str]]
    page_state: dict[str, Any]
    turn_plan: dict[str, Any]
    memory_enabled: bool
    status: str
    intent: str
    route: str
    requires_sources: bool
    memory_context: str
    context_mode: str
    context_text: str
    sources: list[dict[str, Any]]
    rag_called: bool
    answer: str
    error_code: str
    error_message: str


EmitEvent = Callable[[RuntimeEvent], Awaitable[None]]
MemoryRecall = Callable[[ReadonlyAgentState], Awaitable[str]]
RagSearch = Callable[[ReadonlyAgentState], Awaitable[dict[str, Any]]]
AnswerStream = Callable[[ReadonlyAgentState], AsyncIterator[dict[str, Any]]]


async def _ignore_event(_event: RuntimeEvent) -> None:
    return None


async def _default_memory_recall(state: ReadonlyAgentState) -> str:
    from app.memory.agent import (
        memory_context_from_records,
        query_memories_for_agent,
    )
    from app.memory.planning import memory_read_plan_from_turn_plan
    from app.services.turn_planner import TurnPlan

    turn_plan = TurnPlan.model_validate(state.get("turn_plan") or {})
    _, memories = await query_memories_for_agent(
        state["user_id"],
        state["question"],
        read_plan=memory_read_plan_from_turn_plan(
            turn_plan,
            question=state["question"],
        ),
    )
    return memory_context_from_records(memories)


async def _default_rag_search(state: ReadonlyAgentState) -> dict[str, Any]:
    from app.rag.service import RagRequest, configured_rag_service
    page_state = state.get("page_state") or {}
    if page_state.get("contextScope") == "none" and state.get("mode") != "ask_notes":
        return {"context_mode": "general", "context_text": "", "sources": [], "rag_called": False}
    selection_scope = has_selection_scope(page_state)
    selection = selection_context_text(page_state) if selection_scope else ""
    note_id = whole_note_id(page_state, state["question"])
    if note_id:
        result = await configured_rag_service().retrieve(RagRequest(
            user_id=state["user_id"],
            question=state["question"],
            note_id=note_id,
            limit_note_excerpts=True,
        ))
        related = result.context_text if result.sources else "没有找到这篇笔记中可引用的段落。"
        return {
            "context_mode": "note_excerpt",
            "context_text": f"{selection}\n\n同一篇笔记的检索结果：\n{related}".strip(),
            "sources": result.sources,
        }
    if selection_scope:
        return {"context_mode": "selection", "context_text": selection, "sources": [], "rag_called": False}
    plan = state.get("turn_plan") or {}
    context_sources = set(plan.get("context_sources") or [])
    library_scope = "knowledge_base" in context_sources
    use_current_note = "current_note" in context_sources
    use_selection = "selected_text" in context_sources
    result = await configured_rag_service().retrieve(RagRequest(
        user_id=state["user_id"], question=state["question"],
        note_id=str(page_state.get("currentNoteId") or "") if use_current_note else None,
        fallback_query=state["question"],
        selected_text=str(page_state.get("selectedText") or "") if use_selection else "",
        current_section_id=page_state.get("currentSectionId"),
        unsaved_content=(
            ""
            if library_scope or not use_current_note or not page_state.get("dirty")
            else str(page_state.get("unsavedContent") or "")
        ),
    ))
    return {"context_mode": result.context_mode, "context_text": result.context_text, "sources": result.sources}


async def _default_answer_stream(state: ReadonlyAgentState) -> AsyncIterator[dict[str, Any]]:
    from app.providers.chat import DeepSeekChatModelProvider
    provider = DeepSeekChatModelProvider()
    history = [
        ProviderChatMessage(role=str(item.get("role") or "user"), text=str(item.get("text") or ""))
        for item in state.get("history", [])[-8:]
    ]
    request = ChatProviderRequest(
        question=state["question"],
        document_title="用户笔记库" if state.get("requires_sources") else "",
        document_content=state.get("context_text", ""),
        memory_context=state.get("memory_context", ""),
        history=history,
        strict_note_answer=bool(state.get("requires_sources")),
    )
    async with aclosing(provider.stream(request)) as chunks:
        async for chunk in chunks:
            yield chunk


@dataclass
class ReadonlyGraphDependencies:
    emit: EmitEvent = _ignore_event
    memory_recall: MemoryRecall = _default_memory_recall
    rag_search: RagSearch = _default_rag_search
    answer_stream: AnswerStream = _default_answer_stream


def new_readonly_state(
    *, user_id: str, question: str, mode: str, history: list[dict[str, str]] | None = None,
    page_state: dict[str, Any] | None = None, memory_enabled: bool = True,
    turn_plan: dict[str, Any] | None = None,
    session_id: str | None = None, run_id: str | None = None,
    trace_id: str = "", thread_id: str | None = None,
) -> ReadonlyAgentState:
    resolved_session = session_id or uuid.uuid4().hex
    resolved_run = run_id or uuid.uuid4().hex
    resolved_plan = turn_plan
    if resolved_plan is None:
        resolved_plan = {
            "context_sources": ["knowledge_base"] if mode == "ask_notes" else [],
        }
    return ReadonlyAgentState(
        user_id=user_id, session_id=resolved_session, run_id=resolved_run,
        trace_id=trace_id, thread_id=thread_id or resolved_session,
        question=(question or "").strip(), mode=mode,
        history=history or [], page_state=page_state or {},
        turn_plan=resolved_plan,
        memory_enabled=memory_enabled,
        status="running", sources=[], rag_called=False, answer="",
        error_code="", error_message="",
    )


def _choice_delta(text: str) -> RuntimeEvent:
    return RuntimeEvent(type="choices", payload={"choices": [{"delta": {"content": text}, "finish_reason": "stop"}]})


def _chunk_text(chunk: dict[str, Any]) -> str:
    choices = chunk.get("choices") or []
    if not choices or not isinstance(choices[0], dict):
        return ""
    delta = choices[0].get("delta") or {}
    return str(delta.get("content") or "") if isinstance(delta, dict) else ""


def _route_after(state: ReadonlyAgentState) -> str:
    if state.get("error_code"):
        return "error"
    return "note_qa" if state.get("route") == "note_qa" else "general_chat"


def _error_or_next(next_node: str):
    def route(state: ReadonlyAgentState) -> str:
        return "error" if state.get("error_code") else next_node
    return route


def build_readonly_graph(
    dependencies: ReadonlyGraphDependencies | None = None,
    *, checkpointer=None, interrupt_after: list[str] | None = None,
):
    try:
        from langgraph.graph import END, START, StateGraph
    except ImportError as exc:
        raise RuntimeError("LangGraph runtime is not installed; use the locked AI runtime image") from exc

    deps = dependencies or ReadonlyGraphDependencies()

    async def ingress(state: ReadonlyAgentState) -> dict[str, Any]:
        if not state.get("question"):
            return {"error_code": "invalid_input", "error_message": "问题不能为空", "status": "failed"}
        await deps.emit(RuntimeEvent(
            type="agent_session", status="running",
            sessionId=state.get("session_id"), runId=state.get("run_id"), traceId=state.get("trace_id"),
            payload={"runtime": "langgraph_readonly"},
        ))
        return {"status": "running"}

    async def policy(state: ReadonlyAgentState) -> dict[str, Any]:
        mode = state.get("mode")
        if mode not in {"chat", "ask_notes"}:
            return {"error_code": "mode_not_allowed", "error_message": "只读图仅支持 chat 和 ask_notes", "status": "failed"}
        return {"mode": mode}

    async def planner(state: ReadonlyAgentState) -> dict[str, Any]:
        plan = state.get("turn_plan") or {}
        context_sources = set(plan.get("context_sources") or [])
        page_state = state.get("page_state") or {}
        if page_state.get("contextScope") == "none" and state.get("mode") != "ask_notes":
            context_sources &= {"user_memory"}
        bounded_note_scope = has_selection_scope(page_state) or bool(whole_note_id(page_state, state["question"]))
        use_note_context = bounded_note_scope or bool(
            context_sources.intersection({"knowledge_base", "current_note", "selected_text"})
        )
        intent = "note_qa" if use_note_context else "general_chat"
        route = intent
        await deps.emit(RuntimeEvent(
            type="tool_trace", status="success", sessionId=state.get("session_id"), runId=state.get("run_id"),
            payload={
                "toolName": "langgraph_planner", "action": route, "durationMs": 0,
                "metadata": {
                    "mode": state["mode"],
                    "hardPolicy": True,
                    "readOnly": True,
                    "contextSources": sorted(context_sources),
                },
            },
        ))
        return {
            "intent": intent,
            "route": route,
            "requires_sources": not bounded_note_scope and "knowledge_base" in context_sources,
        }

    async def memory_recall(state: ReadonlyAgentState) -> dict[str, Any]:
        plan = state.get("turn_plan") or {}
        context_sources = set(plan.get("context_sources") or [])
        if (
            not state.get("memory_enabled")
            or state.get("route") == "note_qa"
            or "user_memory" not in context_sources
        ):
            return {"memory_context": ""}
        try:
            async with asyncio.timeout(OPTIONAL_MEMORY_TIMEOUT_SECONDS):
                return {"memory_context": await deps.memory_recall(state)}
        except Exception as exc:
            # Personalization is optional in this read-only answer flow. Keep
            # explicit memory-management failures on their separate tool path.
            # Do not expose provider errors (URLs/credentials) in public traces
            # or reuse a previous checkpoint's memory after a failed refresh.
            await deps.emit(RuntimeEvent(
                type="tool_trace", status="failed", code="memory_read_failed",
                sessionId=state.get("session_id"), runId=state.get("run_id"),
                payload={
                    "toolName": "memory_recall", "action": "optional_context",
                    "metadata": {"degraded": True, "reason": type(exc).__name__},
                },
            ))
            return {"memory_context": ""}

    async def general_chat(state: ReadonlyAgentState) -> dict[str, Any]:
        return {"context_mode": "general", "context_text": "", "sources": [], "rag_called": False}

    async def note_qa(state: ReadonlyAgentState) -> dict[str, Any]:
        try:
            result = await deps.rag_search(state)
            sources = list(result.get("sources") or [])
            rag_called = bool(result.get("rag_called", True))
            await deps.emit(RuntimeEvent(
                type="context", status="success", sessionId=state.get("session_id"), runId=state.get("run_id"),
                contextMode=str(result.get("context_mode") or "library"),
                payload={"sources": sources, "ragCalled": rag_called, "readOnly": True},
            ))
            return {
                "context_mode": str(result.get("context_mode") or "library"),
                "context_text": str(result.get("context_text") or ""),
                "sources": sources, "rag_called": rag_called,
            }
        except Exception as exc:
            return {"error_code": "rag_read_failed", "error_message": str(exc), "status": "failed", "rag_called": True}

    async def answer(state: ReadonlyAgentState) -> dict[str, Any]:
        if state.get("requires_sources") and not state.get("sources"):
            text = "我在你的笔记里没有找到相关内容。"
            await deps.emit(_choice_delta(text))
            return {"answer": text}
        parts: list[str] = []
        try:
            async with aclosing(deps.answer_stream(state)) as chunks:
                async for chunk in chunks:
                    if chunk.get("type") in {"stream_error", "agent_error"}:
                        return {
                            "error_code": str(chunk.get("code") or "ai_stream_failed"),
                            "error_message": str(chunk.get("message") or "AI 流式回答失败"),
                            "status": "failed",
                        }
                    parts.append(_chunk_text(chunk))
                    await deps.emit(RuntimeEvent.from_wire(chunk))
            answer_text = "".join(parts)
            if state.get("requires_sources") or state.get("context_mode") == "note_excerpt":
                cleaned, validation = validate_citation_indexes(
                    answer_text,
                    set(range(1, len(state.get("sources") or []) + 1)),
                )
                if cleaned != answer_text:
                    await deps.emit(RuntimeEvent(
                        type="answer_replace",
                        status="success",
                        sessionId=state.get("session_id"),
                        runId=state.get("run_id"),
                        payload={"content": cleaned, "citationValidation": validation},
                    ))
                answer_text = cleaned
            return {"answer": answer_text}
        except Exception as exc:
            return {"error_code": "ai_stream_failed", "error_message": str(exc), "status": "failed"}

    async def finalize(state: ReadonlyAgentState) -> dict[str, Any]:
        await deps.emit(RuntimeEvent(
            type="agent_done", status="completed", sessionId=state.get("session_id"), runId=state.get("run_id"),
            payload={"runtime": "langgraph_readonly", "readOnly": True},
        ))
        return {"status": "success"}

    async def error(state: ReadonlyAgentState) -> dict[str, Any]:
        public_message = "只读 Agent 暂时无法完成请求。"
        await deps.emit(RuntimeEvent(
            type="agent_error", status="failed", code=state.get("error_code") or "agent_failed",
            message=public_message, sessionId=state.get("session_id"), runId=state.get("run_id"),
        ))
        await deps.emit(_choice_delta(public_message))
        await deps.emit(RuntimeEvent(
            type="agent_done", status="failed", sessionId=state.get("session_id"), runId=state.get("run_id"),
            payload={"runtime": "langgraph_readonly", "readOnly": True},
        ))
        return {"status": "failed", "answer": public_message}

    builder = StateGraph(ReadonlyAgentState)
    for name, node in (
        ("ingress", ingress), ("policy", policy), ("planner", planner),
        ("memory_recall", memory_recall), ("general_chat", general_chat),
        ("note_qa", note_qa), ("answer_node", answer),
        ("finalize", finalize), ("error_node", error),
    ):
        builder.add_node(name, node)
    builder.add_edge(START, "ingress")
    builder.add_conditional_edges("ingress", _error_or_next("policy"), {"policy": "policy", "error": "error_node"})
    builder.add_conditional_edges("policy", _error_or_next("planner"), {"planner": "planner", "error": "error_node"})
    builder.add_edge("planner", "memory_recall")
    builder.add_conditional_edges(
        "memory_recall", _route_after,
        {"general_chat": "general_chat", "note_qa": "note_qa", "error": "error_node"},
    )
    builder.add_edge("general_chat", "answer_node")
    builder.add_conditional_edges("note_qa", _error_or_next("answer_node"), {"answer_node": "answer_node", "error": "error_node"})
    builder.add_conditional_edges(
        "answer_node",
        _error_or_next("finalize"),
        {"finalize": "finalize", "error": "error_node"},
    )
    builder.add_edge("finalize", END)
    builder.add_edge("error_node", END)
    kwargs = {"checkpointer": checkpointer}
    if interrupt_after:
        kwargs["interrupt_after"] = interrupt_after
    return builder.compile(**kwargs)


async def invoke_readonly_graph(
    state: ReadonlyAgentState | None, *, dependencies: ReadonlyGraphDependencies | None = None,
    checkpointer=None, thread_id: str | None = None, interrupt_after: list[str] | None = None,
) -> ReadonlyAgentState:
    graph = build_readonly_graph(dependencies, checkpointer=checkpointer, interrupt_after=interrupt_after)
    resolved_thread = thread_id or (state or {}).get("thread_id") or uuid.uuid4().hex
    config = {"configurable": {"thread_id": resolved_thread, "checkpoint_ns": ""}}
    return await graph.ainvoke(state, config=config)


async def stream_readonly_graph(
    state: ReadonlyAgentState, *, checkpointer=None,
    dependencies: ReadonlyGraphDependencies | None = None,
) -> AsyncIterator[RuntimeEvent]:
    queue: asyncio.Queue[RuntimeEvent | object] = asyncio.Queue()
    done = object()

    async def emit(event: RuntimeEvent) -> None:
        await queue.put(event)

    async def execute() -> None:
        try:
            base = dependencies or ReadonlyGraphDependencies()
            runtime_dependencies = ReadonlyGraphDependencies(
                emit=emit, memory_recall=base.memory_recall,
                rag_search=base.rag_search, answer_stream=base.answer_stream,
            )
            await invoke_readonly_graph(
                state, dependencies=runtime_dependencies,
                checkpointer=checkpointer, thread_id=state.get("thread_id"),
            )
        except Exception:
            await emit(RuntimeEvent(
                type="agent_error", status="failed", code="langgraph_runtime_failed",
                message="只读 Agent 暂时无法完成请求。", sessionId=state.get("session_id"), runId=state.get("run_id"),
            ))
            await emit(RuntimeEvent(type="agent_done", status="failed", sessionId=state.get("session_id"), runId=state.get("run_id")))
        finally:
            await queue.put(done)

    task = asyncio.create_task(execute(), name=f"langgraph-readonly-{state.get('run_id', 'run')}")
    try:
        while True:
            item = await queue.get()
            if item is done:
                break
            yield item  # type: ignore[misc]
    finally:
        if not task.done():
            task.cancel()
        with anyio.CancelScope(shield=True):
            await asyncio.gather(task, return_exceptions=True)


def postgres_checkpoint_uri() -> str:
    return (
        f"postgresql://{quote(cfg.DB_USER)}:{quote(cfg.DB_PASSWORD)}"
        f"@{cfg.DB_HOST}:{cfg.DB_PORT}/{quote(cfg.DB_NAME)}"
    )


@asynccontextmanager
async def postgres_checkpointer():
    try:
        from langgraph.checkpoint.postgres.aio import AsyncPostgresSaver
    except ImportError as exc:
        raise RuntimeError("langgraph-checkpoint-postgres is not installed") from exc
    async with AsyncPostgresSaver.from_conn_string(postgres_checkpoint_uri()) as saver:
        yield saver

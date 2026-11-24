from __future__ import annotations

from dataclasses import replace

from app.memory.context import MemoryContextService
from app.memory.engine import MemoryUnavailable
from app.memory.planning import broad_memory_read_plan
from app.memory.presentation import format_context
from app.rag.service import RagRequest, configured_rag_service
from app.services.draft_trace import DraftRunLink, draft_stage
from app.services.prompts import NoteGenerateRequest


MEMORY_TYPES = [
    "identity",
    "personal_info",
    "interest",
    "preference",
    "goal",
    "writing_style",
    "constraint",
    "workflow",
    "skill",
]


class AIApplicationService:
    def __init__(self) -> None:
        self.memory = MemoryContextService()

    async def prepare_note_generation(
        self,
        request: NoteGenerateRequest,
        *,
        user_id: str,
        source_note_id: str | None,
        memory_requested: bool,
        link: DraftRunLink | None,
        **stage_details,
    ) -> NoteGenerateRequest:
        source_context = ""
        async with draft_stage(link, "read_source_note", **stage_details, sourceNoteId=source_note_id) as trace:
            if source_note_id:
                result = await configured_rag_service().retrieve(RagRequest(
                    user_id=user_id, note_id=source_note_id,
                    question=f"{request.topic}\n{request.outlinePlan}", limit_note_excerpts=True,
                ))
                trace["excerptCount"] = len(result.sources)
                if not result.sources:
                    raise ValueError("找不到来源笔记中可用的内容，请确认笔记仍在知识库中，并等索引完成后再试。")
                source_context = "\n\n".join(
                    f"{source['noteTitle']} / {source['sectionTitle']}\n{source['snippet']}"
                    for source in result.sources
                )
            else:
                trace["skipped"] = "no_source_note"

        plan = broad_memory_read_plan(
            f"{request.topic} {request.extraRequest}",
            scopes=["note_generation", "learning"],
            memory_types=MEMORY_TYPES,
            limit=6,
        )
        plan.include_scoped_types = ("writing_style",) if source_note_id else ()
        memory_context = ""
        try:
            async with draft_stage(link, "read_writing_memory", **stage_details) as trace:
                if await self.memory.enabled(user_id=user_id, requested=memory_requested):
                    memories = await self.memory.recall(user_id, plan)
                    trace["memoryCount"] = len(memories)
                    trace["memoryIds"] = [memory.id for memory in memories]
                    trace["writingStyleCount"] = sum(memory.memory_type == "writing_style" for memory in memories)
                    memory_context = format_context(memories)
                else:
                    trace["skipped"] = "memory_disabled"
        except MemoryUnavailable:
            pass
        return replace(request, sourceContext=source_context, memoryContext=memory_context)

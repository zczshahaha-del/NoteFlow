from __future__ import annotations

from app.memory.context import MemoryContextService
from app.rag.service import RagRequest, configured_rag_service


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

    async def note_generation_source(self, *, user_id: str, note_id: str, query: str) -> str:
        result = await configured_rag_service().retrieve(RagRequest(
            user_id=user_id, note_id=note_id, question=query, limit_note_excerpts=True,
        ))
        if not result.sources:
            raise ValueError("找不到来源笔记中可用的内容，请确认笔记仍在知识库中，并等索引完成后再试。")
        return "\n\n".join(
            f"{source['noteTitle']} / {source['sectionTitle']}\n{source['snippet']}"
            for source in result.sources
        )

    async def note_generation_memory(
        self,
        *,
        user_id: str,
        requested: bool,
        topic: str,
        extra_request: str,
        sourced_draft: bool = False,
    ) -> str:
        return await self.memory.context(
            user_id=user_id,
            requested=requested,
            query=f"{topic} {extra_request}",
            scopes=["note_generation", "learning"],
            memory_types=MEMORY_TYPES,
            include_scoped_types=("writing_style",) if sourced_draft else (),
        )

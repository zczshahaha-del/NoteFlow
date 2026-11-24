from __future__ import annotations

import logging
from contextlib import aclosing
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field
from app.config import cfg
from app.database import AsyncSessionLocal
from app.deps import CurrentUser, redis_rate_limit
from app.repositories.drafts import DraftRepository
from app.repositories.runs import RunRepository
from app.services.ai import stream_note_generate
from app.services.ai_application import AIApplicationService
from app.services.draft_trace import DraftRunLink, draft_checkpoint, draft_stage
from app.services.prompts import NoteGenerateRequest
from app.services.runtime_errors import public_error_message
from app.agent.sse import encode_event

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/ai", tags=["ai"])


class NoteGeneratePayload(BaseModel):
    mode: str = "generate"
    topic: str = ""
    noteType: str = ""
    writingTone: str = ""
    noteFormat: str = ""
    headingLevel: str = ""
    includeCode: bool = False
    includeExercises: bool = False
    extraRequest: str = ""
    markdown: str = ""
    outlinePlan: str = ""
    sourceNoteId: str | None = Field(default=None, max_length=64)
    checkpointId: str | None = Field(default=None, max_length=64)
    draftId: str | None = Field(default=None, max_length=64)
    sectionId: str | None = Field(default=None, max_length=64)
    maxTokens: int = 0
    temperature: Optional[float] = None
    memoryEnabled: bool = True


def _sse_format(data: dict) -> str:
    return encode_event(data)


def _stream_error_event(message: str, code: str = "ai_stream_failed") -> dict:
    return {
        "type": "stream_error",
        "status": "failed",
        "code": code,
        "message": message,
    }


async def _generation_source(payload: NoteGeneratePayload, user_id: str) -> tuple[str | None, DraftRunLink | None]:
    if payload.draftId and payload.checkpointId:
        raise HTTPException(400, detail="请提供草稿或草稿任务中的一个。")
    if not (payload.draftId or payload.checkpointId):
        return payload.sourceNoteId, None
    async with AsyncSessionLocal() as db:
        if payload.draftId:
            draft = await DraftRepository(db).get(user_id, payload.draftId)
            if draft is None:
                raise HTTPException(404, detail="草稿不存在。")
            source = (draft.draft_config or {}).get("sourceNoteId")
            checkpoint = await draft_checkpoint(db, user_id, draft.id)
        else:
            checkpoint = await RunRepository(db).get_checkpoint(user_id, payload.checkpointId)
            if checkpoint is None or checkpoint.checkpoint_type != "draft_workspace":
                raise HTTPException(404, detail="草稿任务不存在。")
            if checkpoint.status != "waiting_user_confirm":
                raise HTTPException(409, detail="草稿任务已结束。")
            source = (checkpoint.payload or {}).get("sourceNoteId")
        link = await DraftRunLink.from_checkpoint(db, checkpoint) if checkpoint else None
    return source if isinstance(source, str) and source else None, link


@router.post("/notes/generate")
async def generate_note(
    payload: NoteGeneratePayload,
    user: CurrentUser = Depends(redis_rate_limit("ai-note", cfg.AI_RATE_LIMIT)),
):
    try:
        source_note_id, link = await _generation_source(payload, user.id)
        mode = payload.mode if payload.mode in {"outline", "section", "fromOutline", "continue", "expand", "revise"} else "generate"
        details = {"mode": mode, "draftId": payload.draftId, "sectionId": payload.sectionId, "entry": "interactive"}
        note_req = NoteGenerateRequest(
            mode=payload.mode,
            topic=payload.topic,
            noteType=payload.noteType,
            writingTone=payload.writingTone,
            noteFormat=payload.noteFormat,
            headingLevel=payload.headingLevel,
            includeCode=payload.includeCode,
            includeExercises=payload.includeExercises,
            extraRequest=payload.extraRequest,
            markdown=payload.markdown,
            outlinePlan=payload.outlinePlan,
            maxTokens=payload.maxTokens,
            temperature=payload.temperature,
        )
        note_req = await AIApplicationService().prepare_note_generation(
            note_req, user_id=user.id, source_note_id=source_note_id,
            memory_requested=payload.memoryEnabled, link=link, **details,
        )

        async def _stream():
            try:
                async with draft_stage(link, "generate", **details, model=cfg.DEEPSEEK_MODEL) as trace:
                    trace["outputChars"] = 0
                    async with aclosing(stream_note_generate(note_req)) as chunks:
                        async for chunk in chunks:
                            choice = (chunk.get("choices") or [{}])[0]
                            trace["outputChars"] += len((choice.get("delta") or {}).get("content") or "")
                            if choice.get("finish_reason"):
                                trace["finishReason"] = choice["finish_reason"]
                            yield _sse_format(chunk)
                    if not trace["outputChars"]:
                        trace["errorType"] = "EmptyModelResponse"
            except ValueError as e:
                if "not configured" in str(e).lower():
                    yield _sse_format(_stream_error_event("AI 服务还没有配置好，暂时不能生成回复。", "ai_not_configured"))
                else:
                    yield _sse_format(_stream_error_event(public_error_message(e)))
                yield "data: [DONE]\n\n"
                return
            except Exception as e:
                yield _sse_format(_stream_error_event(public_error_message(e)))
                yield "data: [DONE]\n\n"
                return
            yield "data: [DONE]\n\n"

        return StreamingResponse(
            _stream(),
            media_type="text/event-stream; charset=utf-8",
            headers={"Cache-Control": "no-cache", "Connection": "keep-alive"},
        )
    except ValueError as e:
        if "not configured" in str(e).lower():
            raise HTTPException(status_code=503, detail=public_error_message(e))
        raise HTTPException(status_code=400, detail=public_error_message(e))

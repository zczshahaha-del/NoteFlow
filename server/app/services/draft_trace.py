from __future__ import annotations

import asyncio
import logging
import time
from contextlib import asynccontextmanager
from dataclasses import dataclass

import anyio
from sqlalchemy import select
from sqlalchemy.exc import SQLAlchemyError

from app.models.db import AgentCheckpoint, NoteDraft
from app.observability.context import current_trace, new_trace_id, structured_log, trace_scope
from app.repositories.runs import RunRepository
from app.services.agent_runtime import record_tool_trace

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class DraftRunLink:
    user_id: str
    run_id: str
    session_id: str
    trace_id: str

    @classmethod
    async def from_checkpoint(cls, db, checkpoint: AgentCheckpoint) -> DraftRunLink | None:
        run = await RunRepository(db).get_run(checkpoint.user_id, checkpoint.run_id)
        if run is None:
            return None
        return cls(run.user_id, run.id, run.session_id, run.trace_id or run.id)

    def scope(self):
        return trace_scope(run_id=self.run_id, session_id=self.session_id, trace_id=self.trace_id)


async def draft_checkpoint(db, user_id: str, draft_id: str) -> AgentCheckpoint | None:
    return await db.scalar(
        select(AgentCheckpoint).where(
            AgentCheckpoint.user_id == user_id,
            AgentCheckpoint.checkpoint_type == "draft_workspace",
            AgentCheckpoint.payload["draftId"].as_string() == draft_id,
        ).order_by(AgentCheckpoint.created_at, AgentCheckpoint.id).limit(1)
    )


async def draft_run_link(user_id: str, draft_id: str, *, sessions) -> DraftRunLink | None:
    try:
        async with sessions() as db:
            checkpoint = await draft_checkpoint(db, user_id, draft_id)
            return await DraftRunLink.from_checkpoint(db, checkpoint) if checkpoint else None
    except SQLAlchemyError:
        logger.warning("Could not load draft trace link")
        return None


@asynccontextmanager
async def draft_stage(link: DraftRunLink | None, action: str, *, started: float | None = None, **metadata):
    started = time.perf_counter() if started is None else started
    if current_trace().request_id:
        metadata["requestId"] = current_trace().request_id
    status = "success"
    scope = link.scope() if link else trace_scope(trace_id=current_trace().trace_id or new_trace_id())
    with scope:
        try:
            yield metadata
        except (asyncio.CancelledError, GeneratorExit):
            status = "cancelled"
            raise
        except Exception as exc:
            status = "failed"
            metadata["errorType"] = type(exc).__name__
            if getattr(exc, "status_code", None) in {400, 404, 409, 429}:
                metadata["httpStatus"] = exc.status_code
            raise
        finally:
            if status == "success":
                if metadata.get("skipped"):
                    status = "skipped"
                elif metadata.get("errorType"):
                    status = "failed"
            duration_ms = round((time.perf_counter() - started) * 1000)
            structured_log(logger, logging.INFO, "draft.stage", action=action, status=status,
                           durationMs=duration_ms, **metadata)
            if link:
                try:
                    with anyio.move_on_after(1, shield=True):
                        await record_tool_trace(
                            user_id=link.user_id, run_id=link.run_id, step_id=None,
                            tool_name="note_draft_tool", action=action, status=status,
                            duration_ms=duration_ms, metadata=metadata,
                        )
                except SQLAlchemyError:
                    logger.warning("Could not persist draft stage %s", action)

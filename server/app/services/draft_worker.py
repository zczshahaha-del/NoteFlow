from __future__ import annotations

import asyncio
import logging
import re
from contextlib import nullcontext
from datetime import datetime
from uuid import uuid4

from sqlalchemy import select, text

from app.services.prompt_templates import render_prompt
from app.config import cfg
from app.database import AsyncSessionLocal, engine
from app.models.db import NoteDraft, NoteDraftSection
from app.services.ai import ERR_RATE_LIMITED, complete_chat
from app.services.ai_application import AIApplicationService
from app.services.draft_trace import DraftRunLink, draft_run_link, draft_stage
from app.services.prompts import NoteGenerateRequest, build_note_generate_prompt
from app.services.heading_numbers import draft_numbering_style, number_generated_section, number_markdown_headings

logger = logging.getLogger(__name__)

_worker_task: asyncio.Task | None = None
_wake_event: asyncio.Event | None = None
_stop_event: asyncio.Event | None = None
_active_claim_tasks: dict[str, asyncio.Task] = {}
_DRAFT_WORKER_LOCK = 1_289_786_611
_PENDING_SECTION_STATUSES = {"outline_only", "failed", "generating", "needs_revision"}


def _now() -> datetime:
    return datetime.utcnow()


def _job_config(draft: NoteDraft) -> dict:
    config = dict(draft.draft_config or {})
    job = dict(config.get("generationJob") or {})
    job["attempts"] = dict(job.get("attempts") or {})
    return job


def _persist_job_config(draft: NoteDraft, job: dict) -> None:
    config = dict(draft.draft_config or {})
    config["generationJob"] = {
        **job,
        "attempts": dict(job.get("attempts") or {}),
    }
    draft.draft_config = config


def notify_draft_worker() -> None:
    if _wake_event is not None:
        _wake_event.set()


def reset_interrupted_sections(sections: list[NoteDraftSection]) -> None:
    for section in sections:
        if section.status == "generating":
            section.status = "generated" if (section.content or "").strip() else "outline_only"


async def _resume_interrupted_jobs() -> int:
    recovered = 0
    async with AsyncSessionLocal() as session:
        async with session.begin():
            result = await session.execute(
                select(NoteDraft).where(NoteDraft.status == "generating").with_for_update()
            )
            for draft in result.scalars().all():
                job = _job_config(draft)
                interrupted_id = job.get("currentSectionId")
                if job.get("status") != "generating" or job.get("cancelRequested") or not interrupted_id:
                    continue
                sections_result = await session.execute(
                    select(NoteDraftSection).where(
                        NoteDraftSection.draft_id == draft.id,
                        NoteDraftSection.user_id == draft.user_id,
                    )
                )
                for section in sections_result.scalars():
                    if section.id == interrupted_id and section.status == "generating":
                        section.status = "outline_only"
                        section.generation_key = None
                        job["attempts"][section.id] = max(0, int(job["attempts"].get(section.id, 0)) - 1)
                job["status"] = "resuming"
                job["error"] = ""
                job.pop("currentSectionId", None)
                job.pop("currentSectionTitle", None)
                job["updatedAt"] = _now().isoformat()
                _persist_job_config(draft, job)
                recovered += 1
    if recovered:
        logger.info("resuming %s interrupted note draft generation job(s)", recovered)
    return recovered


async def cancel_active_draft_generation(draft_id: str) -> bool:
    task = _active_claim_tasks.get(draft_id)
    if task is None or task.done():
        return False
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass
    except Exception:
        logger.exception("cancelled draft task ended with an error draft=%s", draft_id)
    finally:
        if _active_claim_tasks.get(draft_id) is task:
            _active_claim_tasks.pop(draft_id, None)
    return True


def _raw_request(draft: NoteDraft) -> str:
    config = draft.draft_config or {}
    brief = config.get("generationBrief")
    if isinstance(brief, dict) and brief.get("title"):
        from app.services.draft_brief import generation_request
        return generation_request(brief)
    raw = str(config.get("rawRequest") or config.get("trigger") or "").strip()
    return raw or draft.extra_request.strip() or draft.topic


def _body_instruction(draft: NoteDraft) -> str:
    return str((draft.draft_config or {}).get("bodyInstruction") or "").strip()


def _section_outline(section: NoteDraftSection) -> str:
    heading = "#" * max(2, min(section.level, 4))
    text = section.outline_text.strip()
    if text.startswith(f"{heading} "):
        return text
    return f"{heading} {section.title}\n{text}".strip()


def _strip_main_title(content: str, title: str) -> str:
    normalized_title = title.strip()
    if not normalized_title:
        return content.strip()
    title_pattern = re.compile(
        rf"(^|\n{{2,}})#\s+{re.escape(normalized_title)}\s*\n+",
        re.IGNORECASE,
    )
    return title_pattern.sub(lambda match: match.group(1), content).strip()


def _assemble(draft: NoteDraft, sections: list[NoteDraftSection]) -> str:
    parts: list[str] = []
    for section in sorted(sections, key=lambda item: item.sort_order):
        if section.deleted_at is not None:
            continue
        content = (section.content or "").strip()
        if content:
            parts.append(_strip_main_title(content, draft.title))
    return number_markdown_headings("\n\n".join(parts).strip(), draft.title, style=draft_numbering_style(draft))


async def _complete_with_retry(messages: list[dict], *, max_tokens: int, temperature: float,
                               link: DraftRunLink | None, action: str, **details) -> str:
    delays = [4.0, 10.0, 20.0]
    for attempt in range(len(delays) + 1):
        try:
            async with draft_stage(link, action, **details, attempt=attempt + 1, model=cfg.DEEPSEEK_MODEL) as trace:
                content = await complete_chat(
                    messages, max_tokens=max_tokens, temperature=temperature, thinking="disabled",
                )
                trace["outputChars"] = len(content or "")
                if not (content or "").strip():
                    trace["errorType"] = "EmptyModelResponse"
                return content
        except ValueError as error:
            if str(error) != ERR_RATE_LIMITED or attempt >= len(delays):
                raise
            await asyncio.sleep(delays[attempt])
    return ""


async def _generate_section(draft: NoteDraft, section: NoteDraftSection, *, link: DraftRunLink | None = None) -> str:
    raw_request = _raw_request(draft)
    body_instruction = _body_instruction(draft)
    from app.services.draft_brief import body_supplement
    generation_request = raw_request + body_supplement(body_instruction)
    source_note_id = (draft.draft_config or {}).get("sourceNoteId")
    outline = _section_outline(section)
    details = {"mode": "section", "draftId": draft.id, "sectionId": section.id, "entry": "batch"}
    request = await AIApplicationService().prepare_note_generation(
        NoteGenerateRequest(
            mode="section", topic=draft.topic, noteType=draft.note_type, writingTone=draft.writing_tone,
            noteFormat=draft.note_format, headingLevel=draft.heading_level, includeCode=draft.include_code,
            includeExercises=draft.include_exercises, extraRequest=generation_request, outlinePlan=outline,
        ),
        user_id=draft.user_id, source_note_id=source_note_id if isinstance(source_note_id, str) else None,
        memory_requested=True, link=link, **details,
    )
    with link.scope() if link else nullcontext():
        provider_context = render_prompt("drafts/provider-context", base_url=cfg.DEEPSEEK_BASE_URL, model=cfg.DEEPSEEK_MODEL)
        prompt = build_note_generate_prompt(request)
        generation_system = render_prompt("drafts/section-system")
    first_pass = await _complete_with_retry(
        [
            {"role": "system", "content": generation_system},
            {"role": "user", "content": f"{provider_context}\n\n{prompt}"},
        ],
        max_tokens=7000,
        temperature=0.55,
        link=link, action="generate", **details,
    )
    if not first_pass.strip():
        raise ValueError("模型返回了空内容")

    review_requirements = generation_request
    with link.scope() if link else nullcontext():
        if request.sourceContext:
            review_requirements += "\n\n" + render_prompt("notes/source-context", source=request.sourceContext)
        if request.memoryContext:
            review_requirements += "\n\n" + render_prompt("notes/memory-preference") + request.memoryContext
        review_prompt = render_prompt(
            "drafts/review", topic=draft.topic, requirements=review_requirements, section_outline=outline,
            provider_context=provider_context, first_pass=first_pass,
        ).strip()
        review_system = render_prompt("drafts/review-system")
    reviewed = await _complete_with_retry(
        [
            {"role": "system", "content": review_system},
            {"role": "user", "content": review_prompt},
        ],
        max_tokens=7600,
        temperature=0.25,
        link=link, action="review", **details,
    )
    from app.services.heading_numbers import unnumber_generated_headings
    return unnumber_generated_headings(reviewed.strip() or first_pass.strip())


async def _claim_one() -> tuple[str, str, str | None] | None:
    async with AsyncSessionLocal() as session:
        async with session.begin():
            result = await session.execute(
                select(NoteDraft)
                .where(NoteDraft.status == "generating")
                .order_by(NoteDraft.updated_at, NoteDraft.created_at)
                .with_for_update(skip_locked=True)
                .limit(20)
            )
            draft = next(
                (
                    item
                    for item in result.scalars().all()
                    if isinstance((item.draft_config or {}).get("generationJob"), dict)
                ),
                None,
            )
            if draft is None:
                return None

            job = _job_config(draft)
            if job.get("cancelRequested"):
                sections_result = await session.execute(
                    select(NoteDraftSection).where(
                        NoteDraftSection.draft_id == draft.id,
                        NoteDraftSection.user_id == draft.user_id,
                    )
                )
                reset_interrupted_sections(list(sections_result.scalars().all()))
                draft.status = "outline_ready"
                job["status"] = "stopped"
                job.pop("currentSectionId", None)
                job.pop("currentSectionTitle", None)
                job["updatedAt"] = _now().isoformat()
                _persist_job_config(draft, job)
                return None

            sections_result = await session.execute(
                select(NoteDraftSection)
                .where(NoteDraftSection.draft_id == draft.id, NoteDraftSection.user_id == draft.user_id)
                .order_by(NoteDraftSection.sort_order, NoteDraftSection.created_at)
            )
            sections = [item for item in sections_result.scalars().all() if item.deleted_at is None]
            pending = next(
                (
                    item
                    for item in sections
                    if item.status in _PENDING_SECTION_STATUSES
                    and int(job["attempts"].get(item.id, 0)) < 3
                ),
                None,
            )
            if pending is None:
                exhausted = [
                    item
                    for item in sections
                    if item.status == "failed" and int(job["attempts"].get(item.id, 0)) >= 3
                ]
                if exhausted:
                    draft.status = "failed"
                    job["status"] = "failed"
                    job["error"] = f"{exhausted[0].title} 连续生成失败，可点击继续重试"
                    job["updatedAt"] = _now().isoformat()
                    _persist_job_config(draft, job)
                    return None
                if sections and all(item.status in {"generated", "confirmed"} for item in sections):
                    job["status"] = "assembling"
                    job["updatedAt"] = _now().isoformat()
                    _persist_job_config(draft, job)
                    return draft.id, "", None
                draft.status = "failed"
                job["status"] = "failed"
                job["error"] = "生成队列状态异常，请点击继续重试。"
                job["updatedAt"] = _now().isoformat()
                _persist_job_config(draft, job)
                return None

            attempts = int(job["attempts"].get(pending.id, 0)) + 1
            job["attempts"][pending.id] = attempts
            job["status"] = "generating"
            job["currentSectionId"] = pending.id
            job["currentSectionTitle"] = pending.title
            job["updatedAt"] = _now().isoformat()
            _persist_job_config(draft, job)
            pending.status = "generating"
            pending.retry_count = max(0, attempts - 1)
            pending.generation_key = uuid4().hex
            return draft.id, pending.id, pending.generation_key


async def _finalize_draft(draft_id: str) -> None:
    async with AsyncSessionLocal() as session:
        async with session.begin():
            draft_result = await session.execute(
                select(NoteDraft).where(NoteDraft.id == draft_id).with_for_update()
            )
            draft = draft_result.scalar_one_or_none()
            if draft is None or draft.status != "generating" or draft.saved_note_id:
                return
            sections_result = await session.execute(
                select(NoteDraftSection)
                .where(NoteDraftSection.draft_id == draft.id, NoteDraftSection.user_id == draft.user_id)
                .order_by(NoteDraftSection.sort_order, NoteDraftSection.created_at)
            )
            sections = list(sections_result.scalars().all())
            active = [item for item in sections if item.deleted_at is None]
            if not active or not all(item.status in {"generated", "confirmed"} for item in active):
                return
            content = _assemble(draft, active)
            if not content:
                raise ValueError("课程正文为空")

            draft.assembled_content = content
            # A generated draft is still only a draft. The formal Note and its
            # index job are created exclusively by the explicit save endpoint.
            draft.status = "assembled"
            job = _job_config(draft)
            job["status"] = "awaiting_save_confirmation"
            job["updatedAt"] = _now().isoformat()
            _persist_job_config(draft, job)


async def _mark_claim_cancelled(draft_id: str, section_id: str) -> None:
    async with AsyncSessionLocal() as session:
        async with session.begin():
            draft = (
                await session.execute(select(NoteDraft).where(NoteDraft.id == draft_id).with_for_update())
            ).scalar_one_or_none()
            section = (
                await session.execute(
                    select(NoteDraftSection).where(
                        NoteDraftSection.id == section_id,
                        NoteDraftSection.draft_id == draft_id,
                    ).with_for_update()
                )
            ).scalar_one_or_none()
            if draft is None or section is None:
                return
            job = _job_config(draft)
            if not job.get("cancelRequested"):
                return
            reset_interrupted_sections([section])
            draft.status = "outline_ready"
            job["status"] = "stopped"
            job.pop("currentSectionId", None)
            job.pop("currentSectionTitle", None)
            job["updatedAt"] = _now().isoformat()
            _persist_job_config(draft, job)


async def _run_claim(draft_id: str, section_id: str, generation_key: str | None = None) -> None:
    if not section_id:
        await _finalize_draft(draft_id)
        return

    try:
        async with AsyncSessionLocal() as session:
            draft = (
                await session.execute(select(NoteDraft).where(NoteDraft.id == draft_id))
            ).scalar_one_or_none()
            section = (
                await session.execute(
                    select(NoteDraftSection).where(
                        NoteDraftSection.id == section_id,
                        NoteDraftSection.draft_id == draft_id,
                    )
                )
            ).scalar_one_or_none()
            if draft is None or section is None:
                return
            if generation_key and (section.generation_key != generation_key or draft.status != "generating"):
                return
        link = await draft_run_link(draft.user_id, draft_id, sessions=AsyncSessionLocal)
        try:
            content = await _generate_section(draft, section, link=link)
            error_text = ""
        except Exception as error:
            logger.exception("draft section generation failed draft=%s section=%s", draft_id, section_id)
            content = ""
            error_text = str(error)[:1000]
    except asyncio.CancelledError:
        await _mark_claim_cancelled(draft_id, section_id)
        raise

    async with draft_stage(link, "save_section", draftId=draft_id, sectionId=section_id, entry="batch") as trace:
        async with AsyncSessionLocal() as session:
            async with session.begin():
                draft = (
                    await session.execute(select(NoteDraft).where(NoteDraft.id == draft_id).with_for_update())
                ).scalar_one_or_none()
                section = (
                    await session.execute(
                        select(NoteDraftSection).where(
                            NoteDraftSection.id == section_id,
                            NoteDraftSection.draft_id == draft_id,
                        ).with_for_update()
                    )
                ).scalar_one_or_none()
                if draft is None or section is None:
                    trace["skipped"] = "draft_or_section_removed"
                    return
                if generation_key and (section.generation_key != generation_key or draft.status != "generating" or section.status != "generating"):
                    trace["skipped"] = "stale_generation"
                    return
                job = _job_config(draft)
                if job.get("cancelRequested"):
                    trace["skipped"] = "cancel_requested"
                    reset_interrupted_sections([section])
                    draft.status = "outline_ready"
                    job["status"] = "stopped"
                    job.pop("currentSectionId", None)
                    job.pop("currentSectionTitle", None)
                elif content:
                    siblings = list((await session.execute(select(NoteDraftSection).where(NoteDraftSection.draft_id == draft.id, NoteDraftSection.user_id == draft.user_id))).scalars().all())
                    try:
                        numbered = number_generated_section(draft, section, siblings, content)
                    except ValueError as error:
                        trace["errorType"] = type(error).__name__
                        section.status = "failed"
                        job["error"] = str(error)
                    else:
                        section.content = numbered
                        section.status = "generated"
                        job["error"] = ""
                else:
                    trace["errorType"] = "GenerationFailed"
                    section.status = "failed"
                    job["error"] = error_text or "本节生成失败"
                job["updatedAt"] = _now().isoformat()
                _persist_job_config(draft, job)


async def _worker_loop() -> None:
    assert _stop_event is not None
    while not _stop_event.is_set():
        try:
            async with engine.connect() as connection:
                acquired = await connection.scalar(text("SELECT pg_try_advisory_lock(:key)"), {"key": _DRAFT_WORKER_LOCK})
                if acquired:
                    try:
                        await connection.commit()
                        await _resume_interrupted_jobs()
                        await _process_generation_queue(connection)
                    finally:
                        # The session lock must never return to the pool held.
                        try:
                            await connection.execute(text("SELECT pg_advisory_unlock(:key)"), {"key": _DRAFT_WORKER_LOCK})
                        except BaseException:
                            await connection.invalidate()
                            raise
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("note draft worker could not run")
        try:
            await asyncio.wait_for(_stop_event.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            pass


async def _process_generation_queue(connection) -> None:
    assert _wake_event is not None and _stop_event is not None
    logger.info("note draft generation worker started")
    while not _stop_event.is_set():
        await connection.execute(text("SELECT 1"))
        await connection.commit()
        try:
            claimed = await _claim_one()
            if claimed is not None:
                draft_id, section_id, generation_key = claimed
                claim_task = asyncio.create_task(
                    _run_claim(draft_id, section_id, generation_key),
                    name=f"noteflow-draft-claim-{draft_id}",
                )
                _active_claim_tasks[draft_id] = claim_task
                try:
                    await claim_task
                except asyncio.CancelledError:
                    if _stop_event.is_set():
                        break
                finally:
                    if _active_claim_tasks.get(draft_id) is claim_task:
                        _active_claim_tasks.pop(draft_id, None)
                await asyncio.sleep(1.5)
                continue
        except asyncio.CancelledError:
            break
        except Exception:
            logger.exception("note draft worker iteration failed")

        _wake_event.clear()
        try:
            await asyncio.wait_for(_wake_event.wait(), timeout=2.0)
        except asyncio.TimeoutError:
            pass
    logger.info("note draft generation worker stopped")


async def start_draft_worker() -> None:
    global _worker_task, _wake_event, _stop_event
    if _worker_task and not _worker_task.done():
        return
    _wake_event = asyncio.Event()
    _stop_event = asyncio.Event()
    _worker_task = asyncio.create_task(_worker_loop(), name="noteflow-draft-worker")


async def stop_draft_worker() -> None:
    global _worker_task
    if _stop_event is not None:
        _stop_event.set()
    if _wake_event is not None:
        _wake_event.set()
    if _worker_task is not None:
        try:
            await asyncio.wait_for(_worker_task, timeout=5)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            _worker_task.cancel()
        _worker_task = None

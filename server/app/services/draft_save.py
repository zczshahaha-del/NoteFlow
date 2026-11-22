from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone

from app.database import AsyncSessionLocal
from app.models.db import Note, NoteDraft, NoteDraftSection, NoteIndexJob, NoteVersion
from app.repositories.drafts import DraftRepository
from app.repositories.index_jobs import IndexJobRepository
from app.repositories.notes import NoteRepository
from app.services.agent_checkpoints import settle_linked_checkpoints
from app.services.draft_content import assemble_draft_content, clean_draft_title
from app.services.markdown_index import index_note_now
from app.utils import random_id


class DraftSaveError(Exception):
    pass


@dataclass
class SavedDraft:
    draft: NoteDraft
    sections: list[NoteDraftSection]
    note: Note
    job: NoteIndexJob | None
    replayed: bool = False


async def save_confirmed_draft(
    *,
    user_id: str,
    draft_id: str,
    confirmed: bool,
    title: str | None,
    category_id: str | None,
    use_draft_category: bool,
) -> SavedDraft:
    if not confirmed:
        raise DraftSaveError("confirmation_required")

    async with AsyncSessionLocal() as session:
        drafts = DraftRepository(session)
        notes = NoteRepository(session)
        draft = await drafts.get_for_update(user_id, draft_id)
        if draft is None:
            raise DraftSaveError("draft_missing")

        if draft.saved_note_id:
            note = await notes.get(user_id, draft.saved_note_id)
            if note is None:
                raise DraftSaveError("saved_note_missing")
            job = await IndexJobRepository(session).latest_for_note(user_id, draft.saved_note_id)
            sections = await drafts.list_sections(user_id, draft.id)
            if await settle_linked_checkpoints(
                session, user_id=user_id, checkpoint_type="draft_workspace",
                object_id=draft.id, status="resolved", note_id=note.id,
            ):
                await session.commit()
            return SavedDraft(draft, sections, note, job, replayed=True)

        if draft.status == "canceled":
            raise DraftSaveError("draft_canceled")

        if use_draft_category:
            category_id = draft.category_id
        if category_id and await notes.get_category(user_id, category_id) is None:
            raise DraftSaveError("category_missing")

        sections = await drafts.list_sections(user_id, draft.id)
        content = (draft.assembled_content or assemble_draft_content(draft, sections)).strip()
        if not content:
            raise DraftSaveError("empty_draft")

        note = Note(
            id=random_id(), user_id=user_id, title=clean_draft_title(title or draft.title), category_id=category_id,
            summary=None, tags=[], content=content, is_pinned=False, is_favorite=False, index_status="pending",
        )
        session.add(note)
        await session.flush()
        session.add(NoteVersion(
            id=random_id(), note_id=note.id, user_id=user_id, title=note.title, content=content,
            change_summary="AI 草稿保存为正式笔记", source="draft_save",
        ))
        job = await index_note_now(session, note)
        draft.status = "saved"
        draft.saved_note_id = note.id
        draft.saved_at = datetime.now(timezone.utc).replace(tzinfo=None)
        draft.assembled_content = content
        await settle_linked_checkpoints(
            session, user_id=user_id, checkpoint_type="draft_workspace",
            object_id=draft.id, status="resolved", note_id=note.id,
        )
        await session.commit()
        await session.refresh(draft)
        await session.refresh(note)
        await session.refresh(job)
        return SavedDraft(draft, sections, note, job)

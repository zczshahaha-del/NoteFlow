from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import Note, NoteVersion
from app.repositories.notes import NoteRepository
from app.services.markdown_index import index_note_now
from app.utils import random_id


@dataclass(frozen=True)
class NoteChanges:
    title: str | None = None
    category_id: str | None = None
    category_provided: bool = False
    summary: str | None = None
    tags: list[str] | None = None
    content: str | None = None
    pinned: bool | None = None
    favorite: bool | None = None
    index_status: str | None = None
    source: str = "manual_edit"
    change_summary: str | None = None
    expected_updated_at: str | None = None
    expected_content_hash: str | None = None


class NoteWriteError(Exception):
    def __init__(self, code: str, detail: str | dict[str, str]):
        super().__init__(code)
        self.code = code
        self.detail = detail


def has_note_version_conflict(expected_updated_at: Optional[str], current_updated_at: Optional[datetime]) -> bool:
    return bool(
        expected_updated_at
        and current_updated_at
        and expected_updated_at != current_updated_at.isoformat()
    )


def content_hash(content: str) -> str:
    return hashlib.sha256((content or "").encode("utf-8")).hexdigest()


def has_note_content_conflict(expected_content_hash: Optional[str], current_content: str) -> bool:
    return bool(expected_content_hash and expected_content_hash != content_hash(current_content))


def clean_note_title(value: str) -> str:
    title = value.strip().removesuffix(".md").strip()
    return title[:255] or "未命名笔记"


def make_note_version(note: Note, source: str, change_summary: Optional[str]) -> NoteVersion:
    return NoteVersion(
        id=random_id(),
        note_id=note.id,
        user_id=note.user_id,
        title=note.title,
        content=note.content or "",
        change_summary=change_summary,
        source=source or "manual_edit",
    )


async def apply_note_changes(session: AsyncSession, user_id: str, note_id: str, changes: NoteChanges) -> Note:
    repository = NoteRepository(session)
    note = await repository.get(user_id, note_id, include_deleted=True, for_update=True)
    if note is None:
        raise NoteWriteError("note_missing", "note not found")
    if note.deleted_at is not None:
        raise NoteWriteError("note_deleted", "note is deleted")
    if changes.category_provided and changes.category_id and await repository.get_category(user_id, changes.category_id) is None:
        raise NoteWriteError("category_missing", "category not found")

    content_conflict = (
        has_note_content_conflict(changes.expected_content_hash, note.content or "")
        if changes.expected_content_hash
        else has_note_version_conflict(changes.expected_updated_at, note.updated_at)
    )
    if content_conflict:
        raise NoteWriteError("version_conflict", {
            "code": "NOTE_VERSION_CONFLICT",
            "message": "笔记已在其他设备更新，请先处理同步冲突。",
            "currentUpdatedAt": note.updated_at.isoformat(),
            "currentContentHash": content_hash(note.content or ""),
        })

    content_changed = changes.content is not None and changes.content != (note.content or "")
    if content_changed:
        session.add(make_note_version(note, changes.source, changes.change_summary))
        note.content = changes.content or ""
        note.index_status = changes.index_status or "pending"

    if changes.title is not None:
        note.title = clean_note_title(changes.title)
    if changes.category_provided:
        note.category_id = changes.category_id
    if changes.summary is not None:
        note.summary = changes.summary
    if changes.tags is not None:
        note.tags = changes.tags
    if changes.pinned is not None:
        note.is_pinned = changes.pinned
    if changes.favorite is not None:
        note.is_favorite = changes.favorite
    if changes.index_status is not None and not content_changed:
        note.index_status = changes.index_status
    if content_changed:
        await index_note_now(session, note)

    await session.flush()
    await session.refresh(note)
    await session.commit()
    return note

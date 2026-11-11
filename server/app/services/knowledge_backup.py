"""Portable knowledge-library backup. Auth/session/chat/memory data is excluded."""
from __future__ import annotations

import base64
import hashlib
import json
import re
from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator
from sqlalchemy import select, text

from app.config import cfg
from app.database import AsyncSessionLocal
from app.models.db import User, NoteCategory, Note, NoteVersion, NoteAttachment, IntegrationOutbox
from app.services.attachment_service import cleanup_intent
from app.services.object_storage import build_object_storage
from app.utils import random_id

MAX_BACKUP_BYTES = 25 * 1024 * 1024


class Record(BaseModel):
    model_config = ConfigDict(extra='forbid')
    id: str = Field(min_length=1, max_length=64, pattern=r'^[A-Za-z0-9_-]+$')
    createdAt: datetime
    updatedAt: datetime | None = None
    deletedAt: datetime | None = None

    @field_validator('createdAt', 'updatedAt', 'deletedAt')
    @classmethod
    def naive_utc(cls, value):
        if value is not None and value.tzinfo is not None:
            from datetime import timezone
            return value.astimezone(timezone.utc).replace(tzinfo=None)
        return value


class CategoryRecord(Record):
    name: str = Field(min_length=1, max_length=100)
    parentId: str | None = None
    sortOrder: int = 0


class NoteRecord(Record):
    title: str = Field(min_length=1, max_length=255)
    categoryId: str | None = None
    content: str = Field(max_length=8 * 1024 * 1024)
    summary: str | None = None
    tags: list[str] = Field(default_factory=list, max_length=100)
    pinned: bool = False
    favorite: bool = False


class VersionRecord(Record):
    noteId: str
    title: str = Field(min_length=1, max_length=255)
    content: str = Field(max_length=8 * 1024 * 1024)
    changeSummary: str | None = None
    source: str = Field(default='manual_edit', max_length=50)


class AttachmentRecord(Record):
    noteId: str | None = None
    fileName: str = Field(min_length=1, max_length=180)
    contentType: str = Field(max_length=120)
    sha256: str = Field(pattern=r'^[a-f0-9]{64}$')
    data: str = Field(max_length=14 * 1024 * 1024)


class KnowledgeBackup(BaseModel):
    model_config = ConfigDict(extra='forbid')
    format: Literal['noteflow-knowledge-backup'] = 'noteflow-knowledge-backup'
    version: Literal[2] = 2
    exportedAt: datetime
    categories: list[CategoryRecord] = Field(default_factory=list, max_length=500)
    notes: list[NoteRecord] = Field(default_factory=list, max_length=500)
    versions: list[VersionRecord] = Field(default_factory=list, max_length=5000)
    attachments: list[AttachmentRecord] = Field(default_factory=list, max_length=500)

    @model_validator(mode='after')
    def validate_graph(self):
        categories = {row.id: row for row in self.categories}
        notes = {row.id for row in self.notes}
        for rows in (self.categories, self.notes, self.versions, self.attachments):
            if len({row.id for row in rows}) != len(rows):
                raise ValueError('duplicate backup record ID')
        for row in self.categories:
            seen = {row.id}; parent = row.parentId
            while parent is not None:
                if parent not in categories or parent in seen or len(seen) > 64:
                    raise ValueError('invalid folder hierarchy')
                seen.add(parent); parent = categories[parent].parentId
        for row in self.notes:
            if row.categoryId is not None and row.categoryId not in categories:
                raise ValueError('unknown note folder')
        for row in self.versions:
            if row.noteId not in notes:
                raise ValueError('unknown version note')
        for row in self.attachments:
            if row.noteId is not None and row.noteId not in notes:
                raise ValueError('unknown attachment note')
        return self


def _common(row):
    return dict(id=row.id, createdAt=row.created_at, updatedAt=getattr(row, 'updated_at', None), deletedAt=getattr(row, 'deleted_at', None))


async def export_knowledge(user_id: str) -> KnowledgeBackup:
    storage = build_object_storage(cfg.ATTACHMENT_STORAGE_ROOT)
    async with AsyncSessionLocal() as session:
        # One consistent SQL snapshot. Immutable object contents are retained by
        # locking their metadata until bytes have been read.
        if session.bind.dialect.name == 'postgresql':
            await session.execute(text('SET TRANSACTION ISOLATION LEVEL REPEATABLE READ'))
        async def rows(model, limit):
            query = select(model).where(model.user_id == user_id).limit(limit + 1)
            if model is NoteAttachment: query = query.with_for_update()
            values = list((await session.scalars(query)).all())
            if len(values) > limit: raise ValueError('知识库超过单次备份条目上限，请分批导出')
            return values
        categories = await rows(NoteCategory, 500)
        notes = await rows(Note, 500)
        versions = await rows(NoteVersion, 5000)
        attachments = await rows(NoteAttachment, 500)
        data = []
        size = 0
        for row in attachments:
            content = await storage.get(row.storage_key)
            size += len(content)
            if size > MAX_BACKUP_BYTES: raise ValueError('附件超过 25 MB 备份限制')
            if hashlib.sha256(content).hexdigest() != row.sha256:
                raise ValueError('附件校验失败，未生成不完整备份')
            data.append(AttachmentRecord(**_common(row), noteId=row.note_id, fileName=row.file_name,
                                         contentType=row.content_type, sha256=row.sha256, data=base64.b64encode(content).decode()))
        backup = KnowledgeBackup(exportedAt=datetime.utcnow(),
            categories=[CategoryRecord(**_common(r), name=r.name, parentId=r.parent_id, sortOrder=r.sort_order) for r in categories],
            notes=[NoteRecord(**_common(r), title=r.title, categoryId=r.category_id, content=r.content, summary=r.summary,
                              tags=r.tags, pinned=r.is_pinned, favorite=r.is_favorite) for r in notes],
            versions=[VersionRecord(**_common(r), noteId=r.note_id, title=r.title, content=r.content,
                                    changeSummary=r.change_summary, source=r.source) for r in versions], attachments=data)
        if len(backup.model_dump_json().encode()) > MAX_BACKUP_BYTES:
            raise ValueError('备份超过 25 MB 限制，未生成截断文件')
        return backup


def decode_attachments(backup: KnowledgeBackup):
    from app.routers.attachments import validated_content_type, _safe_filename
    decoded = {}
    for row in backup.attachments:
        content = base64.b64decode(row.data, validate=True)
        if not content or len(content) > cfg.ATTACHMENT_MAX_BYTES or hashlib.sha256(content).hexdigest() != row.sha256:
            raise ValueError('附件内容或校验和不正确')
        validated_content_type(_safe_filename(row.fileName), row.contentType, content)
        decoded[row.id] = content
    return decoded


def remap_attachment_links(content: str, mapping: dict[str, str]) -> str:
    # Strip historical signatures while rebinding only included attachments.
    return re.sub(r'/api/attachments/([A-Za-z0-9_-]+)(?:/content)?(?:\?sig=[a-fA-F0-9]+)?',
                  lambda m: f'/api/attachments/{mapping[m[1]]}/content' if m[1] in mapping else m[0], content)


async def import_knowledge(user_id: str, backup: KnowledgeBackup, request_id: str) -> dict:
    from app.routers.attachments import _safe_filename, validated_content_type
    decoded = decode_attachments(backup)  # Entire archive validates before writes.
    digest = hashlib.sha256(backup.model_dump_json().encode()).hexdigest()
    receipt_key = f'knowledge-import:{user_id}:{request_id}'
    storage = build_object_storage(cfg.ATTACHMENT_STORAGE_ROOT)
    async with AsyncSessionLocal() as session:
        existing = await session.scalar(select(IntegrationOutbox).where(IntegrationOutbox.idempotency_key == receipt_key))
        if existing:
            if existing.payload['digest'] != digest: raise ValueError('同一导入请求不能更换文件')
            return existing.payload['result']
    # Unique keys and durable recovery intents cover failure/crash before final SQL commit.
    objects = {}
    async with AsyncSessionLocal() as session:
        for row in backup.attachments:
            new_id = f'att_{random_id()}'
            key = f'{user_id}/{new_id}'
            intent = cleanup_intent(user_id, new_id, key, delayed=True)
            session.add(intent)
            objects[row.id] = (new_id, key, intent)
        await session.commit()
    async with AsyncSessionLocal() as session:
        await session.get(User, user_id, with_for_update=True)
        existing = await session.scalar(select(IntegrationOutbox).where(IntegrationOutbox.idempotency_key == receipt_key))
        if existing:
            if existing.payload['digest'] != digest: raise ValueError('同一导入请求不能更换文件')
            return existing.payload['result']
        # Prefer original IDs in an empty destination, remap conflicts without overwriting.
        async def ids(model, records):
            result = {}
            for row in records:
                result[row.id] = random_id() if await session.get(model, row.id) else row.id
            return result
        category_ids = await ids(NoteCategory, backup.categories)
        note_ids = await ids(Note, backup.notes)
        attachment_ids = {old: item[0] for old, item in objects.items()}
        for old, (_, key, intent) in objects.items():
            job = await session.get(IntegrationOutbox, intent.id, with_for_update=True)
            if job is None or job.status != 'attachment_pending': raise ValueError('导入准备已过期，请重试')
            await storage.put(key, decoded[old])
            await session.delete(job)
        pending = list(backup.categories)
        while pending:
            ready = [r for r in pending if r.parentId is None or all(p.id != r.parentId for p in pending)]
            for r in ready:
                session.add(NoteCategory(id=category_ids[r.id], user_id=user_id, name=r.name,
                    parent_id=category_ids.get(r.parentId), sort_order=r.sortOrder, created_at=r.createdAt,
                    updated_at=r.updatedAt or r.createdAt, deleted_at=r.deletedAt))
                pending.remove(r)
            await session.flush()
        for r in backup.notes:
            session.add(Note(id=note_ids[r.id], user_id=user_id, title=r.title, category_id=category_ids.get(r.categoryId),
                content=remap_attachment_links(r.content, attachment_ids), summary=r.summary, tags=r.tags,
                is_pinned=r.pinned, is_favorite=r.favorite, created_at=r.createdAt,
                updated_at=r.updatedAt or r.createdAt, deleted_at=r.deletedAt, index_status='pending'))
        await session.flush()
        version_ids = await ids(NoteVersion, backup.versions)
        for r in backup.versions:
            session.add(NoteVersion(id=version_ids[r.id], user_id=user_id, note_id=note_ids[r.noteId], title=r.title,
                content=remap_attachment_links(r.content, attachment_ids), source=r.source, change_summary=r.changeSummary, created_at=r.createdAt))
        for r in backup.attachments:
            new_id, key, _ = objects[r.id]
            content_type = validated_content_type(_safe_filename(r.fileName), r.contentType, decoded[r.id])
            session.add(NoteAttachment(id=new_id, user_id=user_id, note_id=note_ids.get(r.noteId), file_name=_safe_filename(r.fileName),
                content_type=content_type, size=len(decoded[r.id]), sha256=r.sha256, storage_key=key, created_at=r.createdAt))
        for r in backup.notes:
            if r.deletedAt: continue
            session.add(IntegrationOutbox(id=random_id(), user_id=user_id, topic='index.note.requested', aggregate_type='note',
                aggregate_id=note_ids[r.id], idempotency_key=f'import-index:{user_id}:{request_id}:{r.id}', payload={}))
        result = {'notesCreated': len(backup.notes), 'categoriesCreated': len(backup.categories),
                  'attachmentsCreated': len(backup.attachments), 'selectedNoteId': next((note_ids[r.id] for r in backup.notes if not r.deletedAt), None)}
        session.add(IntegrationOutbox(id=random_id(), user_id=user_id, topic='knowledge.import.receipt', aggregate_type='knowledge',
            aggregate_id=request_id, idempotency_key=receipt_key, status='succeeded', processed_at=datetime.utcnow(),
            payload={'digest': digest, 'result': result}))
        await session.commit()
        return result

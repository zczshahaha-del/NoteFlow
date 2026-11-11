from __future__ import annotations

from pathlib import Path
from datetime import datetime, timedelta
import logging

from sqlalchemy import select

from app.config import cfg
from app.database import AsyncSessionLocal
from app.models.db import NoteAttachment, IntegrationOutbox
from app.repositories.attachments import AttachmentRepository
from app.repositories.notes import NoteRepository
from app.services.object_storage import build_object_storage
from app.utils import random_id


logger = logging.getLogger(__name__)
CLEANUP_TOPIC = "attachment.object.cleanup"


def cleanup_intent(user_id: str, attachment_id: str, key: str, *, delayed: bool = False) -> IntegrationOutbox:
    return IntegrationOutbox(
        id=random_id(), user_id=user_id, topic=CLEANUP_TOPIC,
        aggregate_type="attachment", aggregate_id=attachment_id,
        idempotency_key=f"attachment-cleanup:{attachment_id}:{random_id()}",
        payload={"storageKey": key}, status="attachment_pending",
        available_at=datetime.utcnow() + timedelta(minutes=5) if delayed else datetime.utcnow(),
        max_attempts=cfg.OUTBOX_MAX_ATTEMPTS,
    )


async def cleanup_attachment_object(session, item: IntegrationOutbox, storage=None) -> None:
    # A failed/ambiguous upload commit must never remove a committed attachment.
    key = item.payload["storageKey"]
    referenced = await session.scalar(select(NoteAttachment.id).where(NoteAttachment.storage_key == key))
    if referenced is None:
        await (storage or build_object_storage(cfg.ATTACHMENT_STORAGE_ROOT)).delete(key)


class AttachmentService:
    def __init__(self) -> None:
        self.storage = build_object_storage(cfg.ATTACHMENT_STORAGE_ROOT)

    async def create(
        self,
        *,
        user_id: str,
        note_id: str | None,
        file_name: str,
        content_type: str,
        content: bytes,
    ) -> NoteAttachment:
        attachment_id = f"att_{random_id()}"
        key = f"{user_id}/{attachment_id}{Path(file_name).suffix.lower()[:16]}"
        # Persist recovery intent before touching storage. A crash at any following
        # point leaves a discoverable job, including partially written objects.
        async with AsyncSessionLocal() as session:
            if note_id and await NoteRepository(session).get_active(user_id, note_id) is None:
                raise LookupError("note not found")
            intent = cleanup_intent(user_id, attachment_id, key, delayed=True)
            session.add(intent)
            await session.commit()
            intent_id = intent.id
        async with AsyncSessionLocal() as session:
            # Serialize with recovery so it cannot delete an in-flight upload.
            intent = await session.get(IntegrationOutbox, intent_id, with_for_update=True)
            if intent is None or intent.status != "attachment_pending":
                raise RuntimeError("attachment upload expired; retry upload")
            stored = await self.storage.put(key, content)
            item = NoteAttachment(
                id=attachment_id, user_id=user_id, note_id=note_id,
                file_name=file_name, content_type=content_type, size=stored.size,
                sha256=stored.sha256, storage_key=stored.key,
            )
            session.add(item)
            await session.delete(intent)
            await session.commit()
            return item

    async def list_for_note(self, user_id: str, note_id: str) -> list[NoteAttachment]:
        async with AsyncSessionLocal() as session:
            return await AttachmentRepository(session).list(user_id, note_id)

    async def read(self, user_id: str, attachment_id: str) -> tuple[NoteAttachment, bytes]:
        async with AsyncSessionLocal() as session:
            item = await AttachmentRepository(session).get(user_id, attachment_id)
            if item is None:
                raise LookupError("attachment not found")
            return item, await self.storage.get(item.storage_key)

    async def delete(self, user_id: str, attachment_id: str) -> None:
        async with AsyncSessionLocal() as session:
            item = await AttachmentRepository(session).get(user_id, attachment_id)
            if item is None:
                raise LookupError("attachment not found")
            intent = cleanup_intent(user_id, attachment_id, item.storage_key)
            session.add(intent)
            await session.delete(item)
            await session.commit()
        # The committed deletion is immediately invisible. Physical cleanup is
        # best effort here and durable/retryable by the outbox worker after restart.
        try:
            async with AsyncSessionLocal() as session:
                intent = await session.get(IntegrationOutbox, intent.id, with_for_update=True)
                if intent and intent.status == "attachment_pending":
                    await cleanup_attachment_object(session, intent, self.storage)
                    await session.delete(intent)
                    await session.commit()
        except Exception:
            logger.warning("attachment cleanup deferred to worker")
        from app.workers.outbox_worker import notify_outbox_worker
        notify_outbox_worker()

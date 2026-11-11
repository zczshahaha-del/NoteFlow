"""Storage/SQL failure boundaries, using real services and disposable SQLite/files."""
from datetime import datetime, timedelta
import os
import asyncio
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

import httpx
from fastapi import FastAPI
from sqlalchemy import select, func
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from app import deps
from app.config import cfg
from app.models.db import Base, User, UserSession, NoteCategory, Note, NoteVersion, NoteAttachment, IntegrationOutbox
from app.routers import attachments
from app.services.attachment_service import AttachmentService, cleanup_attachment_object, cleanup_intent
from app.services.outbox import dispatch_outbox
from app.repositories.outbox import OutboxRepository
from app.utils import create_token


class AttachmentRecoveryTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        database_url=os.environ.get('NOTEFLOW_ATTACHMENT_TEST_DB_URL','sqlite+aiosqlite:///:memory:')
        url=make_url(database_url)
        if url.get_backend_name()!='sqlite' and (url.database!='noteflow_attachment_recovery_test' or url.host not in {'127.0.0.1','localhost'}):
            raise RuntimeError('attachment integration tests require the disposable local database')
        self.engine = create_async_engine(database_url)
        async with self.engine.begin() as c:
            await c.run_sync(lambda sync: Base.metadata.create_all(sync, tables=[
                model.__table__ for model in (User, UserSession, NoteCategory, Note, NoteVersion, NoteAttachment, IntegrationOutbox)]))
        self.sessions = async_sessionmaker(self.engine, expire_on_commit=False)
        self.folder = tempfile.TemporaryDirectory()
        self.patches = [patch('app.services.attachment_service.AsyncSessionLocal', self.sessions),
                        patch.object(deps, 'AsyncSessionLocal', self.sessions),
                        patch.object(cfg, 'ATTACHMENT_STORAGE_ROOT', self.folder.name)]
        for p in self.patches: p.start()
        async with self.sessions() as s:
            s.add_all([User(id='owner', email='owner@example.invalid', display_name='test'),
                       User(id='other', email='other@example.invalid', display_name='test'),
                       UserSession(id='owner-session', user_id='owner', expires_at=datetime.utcnow()+timedelta(days=1)),
                       UserSession(id='other-session', user_id='other', expires_at=datetime.utcnow()+timedelta(days=1))])
            await s.commit()
        self.service = AttachmentService()
        app = FastAPI(); app.include_router(attachments.router, prefix='/api')
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test')

    async def asyncTearDown(self):
        await self.client.aclose()
        for p in reversed(self.patches): p.stop()
        self.folder.cleanup()
        if self.engine.dialect.name=='postgresql':
            async with self.engine.begin() as c:
                await c.run_sync(lambda sync:Base.metadata.drop_all(sync,tables=[model.__table__ for model in
                    (NoteVersion,NoteAttachment,IntegrationOutbox,Note,NoteCategory,UserSession,User)]))
        await self.engine.dispose()

    async def upload(self):
        return await self.service.create(user_id='owner', note_id=None, file_name='test.txt', content_type='text/plain', content=b'synthetic')

    async def drain(self):
        async with self.sessions() as s:
            jobs = list((await s.scalars(select(IntegrationOutbox))).all())
            for job in jobs:
                await dispatch_outbox(s, job)
                await OutboxRepository(s).succeed(job)
            await s.commit()

    async def test_upload_final_commit_failure_leaves_recoverable_intent(self):
        original = AsyncSession.commit
        count = 0
        async def fail_second(session):
            nonlocal count
            count += 1
            if count == 2: raise RuntimeError('synthetic commit failure')
            return await original(session)
        with patch.object(AsyncSession, 'commit', fail_second):
            with self.assertRaises(RuntimeError): await self.upload()
        async with self.sessions() as s:
            self.assertEqual(await s.scalar(select(func.count()).select_from(NoteAttachment)), 0)
            self.assertEqual(await s.scalar(select(func.count()).select_from(IntegrationOutbox)), 1)
        self.assertTrue(list(Path(self.folder.name).rglob('*.txt')))
        await self.drain()
        self.assertFalse(list(Path(self.folder.name).rglob('*.txt')))

    async def test_upload_first_commit_failure_writes_no_file(self):
        with patch.object(AsyncSession, 'commit', side_effect=RuntimeError('synthetic')):
            with self.assertRaises(RuntimeError): await self.upload()
        self.assertFalse([p for p in Path(self.folder.name).rglob('*') if p.is_file()])

    async def test_delete_commit_failure_preserves_file_and_record(self):
        item = await self.upload()
        with patch.object(AsyncSession, 'commit', side_effect=RuntimeError('synthetic')):
            with self.assertRaises(RuntimeError): await self.service.delete('owner', item.id)
        self.assertEqual((await self.service.read('owner', item.id))[1], b'synthetic')

    async def test_storage_delete_failure_is_retryable_after_logical_delete(self):
        item = await self.upload()
        with patch.object(self.service.storage, 'delete', side_effect=OSError('synthetic')):
            await self.service.delete('owner', item.id)
        with self.assertRaises(LookupError): await self.service.read('owner', item.id)
        self.assertTrue((Path(self.folder.name)/item.storage_key).exists())
        await self.drain()
        self.assertFalse((Path(self.folder.name)/item.storage_key).exists())
        await self.drain()  # retry is idempotent

    async def test_recovery_never_removes_referenced_object_and_cleans_partial_write(self):
        item = await self.upload()
        async with self.sessions() as s:
            intent = cleanup_intent('owner', item.id, item.storage_key)
            await cleanup_attachment_object(s, intent)
        self.assertEqual((await self.service.read('owner', item.id))[1], b'synthetic')
        pending = Path(self.folder.name)/'owner/interrupted.pending'
        pending.write_bytes(b'partial')
        async with self.sessions() as s:
            s.add(cleanup_intent('owner', 'interrupted', 'owner/interrupted')); await s.commit()
        await self.drain()
        self.assertFalse(pending.exists())

    async def test_worker_claim_failure_retry_and_stale_lease(self):
        async with self.sessions() as s:
            job=cleanup_intent('owner','interrupted','owner/interrupted');s.add(job);await s.commit()
            claimed=await OutboxRepository(s).claim_one(worker_id='synthetic-worker',stale_seconds=60)
            self.assertEqual(claimed.status,'attachment_processing');self.assertEqual(claimed.id,job.id)
            await OutboxRepository(s).fail(claimed,code='OSError',message='synthetic');await s.commit()
            self.assertEqual(claimed.status,'attachment_pending')
            claimed.available_at=datetime.utcnow()-timedelta(seconds=1);await s.commit()
            again=await OutboxRepository(s).claim_one(worker_id='synthetic-worker',stale_seconds=60)
            self.assertEqual(again.attempts,2)
            again.heartbeat_at=datetime.utcnow()-timedelta(minutes=2);await s.commit()
            recovered=await OutboxRepository(s).claim_one(worker_id='restarted-worker',stale_seconds=60)
            self.assertEqual(recovered.locked_by,'restarted-worker');self.assertEqual(recovered.attempts,3)
            await dispatch_outbox(s,recovered);await OutboxRepository(s).succeed(recovered);await s.commit()

    @unittest.skipUnless(os.environ.get('NOTEFLOW_ATTACHMENT_TEST_DB_URL'), 'requires disposable PostgreSQL')
    async def test_cleanup_worker_cannot_claim_upload_while_object_is_written(self):
        entered,release=asyncio.Event(),asyncio.Event()
        original_put=self.service.storage.put
        async def slow_put(key,content):
            entered.set();await release.wait();return await original_put(key,content)
        def immediate(*args,**kwargs):
            job=cleanup_intent(*args,**kwargs);job.available_at=datetime.utcnow()-timedelta(seconds=1);return job
        with patch.object(self.service.storage,'put',slow_put), patch('app.services.attachment_service.cleanup_intent',immediate):
            task=asyncio.create_task(self.upload())
            try:
                await asyncio.wait_for(entered.wait(),5)
                async with self.sessions() as s:
                    self.assertIsNone(await OutboxRepository(s).claim_one(worker_id='concurrent-cleanup',stale_seconds=60))
            finally:
                release.set()
                item=await asyncio.wait_for(task,5)
        self.assertEqual((await self.service.read('owner',item.id))[1],b'synthetic')

    def headers(self, owner='owner'):
        return {'Authorization': 'Bearer '+create_token(owner, owner+'@example.invalid', 'test', session_id=owner+'-session')}

    async def test_content_requires_live_owner_session_even_with_old_signature(self):
        item = await self.upload()
        url = f'/api/attachments/{item.id}/content?sig=historical-signature'
        self.assertEqual((await self.client.get(url)).status_code, 401)
        self.assertEqual((await self.client.get(url, headers=self.headers('other'))).status_code, 404)
        result = await self.client.get(url, headers=self.headers())
        self.assertEqual(result.status_code, 200)
        self.assertEqual(result.content, b'synthetic')
        self.assertEqual(result.headers['cache-control'], 'private, no-store')
        self.assertTrue(result.headers['content-disposition'].startswith('attachment'))
        async with self.sessions() as s:
            row = await s.get(UserSession, 'owner-session'); row.revoked_at=datetime.utcnow(); await s.commit()
        self.assertEqual((await self.client.get(url, headers=self.headers())).status_code, 401)

    async def test_active_mime_extension_and_image_spoofing_are_rejected(self):
        for mime, name, body in [('Text/Html', 'x.txt', b'hello'), ('application/xhtml+xml', 'x.txt', b'hello'),
                                 ('application/octet-stream', 'x.HTML', b'hello'), ('image/png', 'x.png', b'<html>hello'),
                                 ('text/plain', 'x.txt', b'<!DOCTYPE html><html>hello'), ('image/png', 'x.png', b'not-image')]:
            result = await self.client.post('/api/attachments', headers={**self.headers(), 'Content-Type':'application/octet-stream',
                                              'X-Content-Type':mime, 'X-File-Name':name}, content=body)
            self.assertEqual(result.status_code, 415, (mime, name, result.text))
        result = await self.client.post('/api/attachments', headers={**self.headers(), 'Content-Type':'application/octet-stream',
                                          'X-Content-Type':'Text/Plain; charset=utf-8', 'X-File-Name':'x.txt'}, content=b'notes')
        self.assertEqual(result.status_code, 200, result.text)
        self.assertNotIn('sig=', result.json()['attachment']['downloadUrl'])

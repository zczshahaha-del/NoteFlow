from datetime import datetime
import base64
import hashlib
import json
from pathlib import Path
import unittest
from unittest.mock import patch

from fastapi import FastAPI
import httpx
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.db import Note, NoteCategory, NoteVersion, NoteAttachment, IntegrationOutbox
from app.routers import knowledge_backup as routes
from app.services import knowledge_backup as service
from tests import test_attachment_recovery as fixture


class KnowledgeBackupTest(unittest.IsolatedAsyncioTestCase):
    asyncSetUp = fixture.AttachmentRecoveryTest.asyncSetUp
    asyncTearDown = fixture.AttachmentRecoveryTest.asyncTearDown
    headers = fixture.AttachmentRecoveryTest.headers
    drain = fixture.AttachmentRecoveryTest.drain

    async def seed(self):
        self.backup_patch=patch.object(service,'AsyncSessionLocal',self.sessions);self.backup_patch.start();self.patches.append(self.backup_patch)
        date=datetime(2020,1,2,3,4,5)
        async with self.sessions() as s:
            s.add(NoteCategory(id='parent',user_id='owner',name='父目录',sort_order=3,created_at=date))
            await s.flush()
            s.add(NoteCategory(id='child',user_id='owner',name='子目录',parent_id='parent',created_at=date));await s.flush()
            s.add(Note(id='original-note',user_id='owner',title='备份正文',content='正文',category_id='child',tags=['标签'],
                       is_pinned=True,is_favorite=True,created_at=date,updated_at=date));await s.flush()
            s.add(NoteVersion(id='old-version',user_id='owner',note_id='original-note',title='旧标题',content='旧正文',created_at=date))
            await s.commit()
        item=await self.service.create(user_id='owner',note_id='original-note',file_name='attachment.txt',content_type='text/plain',content=b'portable')
        async with self.sessions() as s:
            note=await s.get(Note,'original-note');note.content=f'正文 [附件](/api/attachments/{item.id}/content?sig=abcdef)';await s.commit()
        return item

    async def test_roundtrip_preserves_hierarchy_metadata_versions_and_portable_attachment(self):
        await self.seed()
        backup=await service.export_knowledge('owner')
        result=await service.import_knowledge('other',backup,'request-1')
        self.assertEqual(result['notesCreated'],1);self.assertEqual(result['attachmentsCreated'],1)
        self.assertNotEqual(result['selectedNoteId'],'original-note')
        async with self.sessions() as s:
            note=await s.get(Note,result['selectedNoteId'])
            self.assertTrue(note.is_pinned and note.is_favorite);self.assertEqual(note.tags,['标签']);self.assertEqual(note.created_at.year,2020)
            child=await s.get(NoteCategory,note.category_id);parent=await s.get(NoteCategory,child.parent_id)
            self.assertEqual((child.name,parent.name),('子目录','父目录'));self.assertEqual(parent.sort_order,3)
            version=await s.scalar(select(NoteVersion).where(NoteVersion.user_id=='other'));self.assertEqual(version.content,'旧正文')
            attachment=await s.scalar(select(NoteAttachment).where(NoteAttachment.user_id=='other'))
            self.assertIn(f'/api/attachments/{attachment.id}/content',note.content);self.assertNotIn('?sig=',note.content)
            self.assertEqual((Path(self.folder.name)/attachment.storage_key).read_bytes(),b'portable')
        again=await service.export_knowledge('other');self.assertEqual(again.notes[0].title,'备份正文');self.assertEqual(len(again.attachments),1)
        self.assertNotIn('user_id',again.model_dump_json());self.assertNotIn('storage_key',again.model_dump_json())

    async def test_retry_does_not_duplicate_and_empty_destination_preserves_note_ids(self):
        await self.seed();backup=await service.export_knowledge('owner')
        # A backup with independent IDs represents restore to a genuinely empty destination.
        backup.notes[0].id='portable-note';backup.versions[0].noteId='portable-note';backup.attachments[0].noteId='portable-note'
        result=await service.import_knowledge('other',backup,'same-request')
        self.assertEqual(result['selectedNoteId'],'portable-note')
        self.assertEqual(await service.import_knowledge('other',backup,'same-request'),result)
        async with self.sessions() as s:
            self.assertEqual(await s.scalar(select(func.count()).select_from(Note).where(Note.user_id=='other')),1)
        reserved=backup.model_copy(deep=True);reserved.notes[0].id='__proto__';reserved.versions[0].noteId='__proto__';reserved.attachments[0].noteId='__proto__'
        safe=await service.import_knowledge('other',reserved,'reserved-key')
        self.assertNotEqual(safe['selectedNoteId'],'__proto__')
        backup.notes[0].content='different'
        with self.assertRaises(ValueError):await service.import_knowledge('other',backup,'same-request')

    async def test_commit_failure_rolls_back_entire_import_and_worker_cleans_files(self):
        await self.seed();backup=await service.export_knowledge('owner')
        original=AsyncSession.commit;count=0
        async def fail_second(s):
            nonlocal count
            count+=1
            if count==2:raise RuntimeError('synthetic final SQL failure')
            return await original(s)
        with patch.object(AsyncSession,'commit',fail_second):
            with self.assertRaises(RuntimeError):await service.import_knowledge('other',backup,'rollback')
        async with self.sessions() as s:
            for model in (Note,NoteCategory,NoteVersion,NoteAttachment):
                self.assertEqual(await s.scalar(select(func.count()).select_from(model).where(model.user_id=='other')),0)
        await self.drain()
        self.assertFalse([p for p in (Path(self.folder.name)/'other').glob('*') if p.is_file()])

    async def test_bad_checksum_cycle_and_unknown_references_do_not_write(self):
        await self.seed();backup=await service.export_knowledge('owner')
        bad=backup.model_copy(deep=True);bad.attachments[0].sha256='0'*64
        with self.assertRaises(ValueError):await service.import_knowledge('other',bad,'bad')
        value=backup.model_dump(mode='json');value['categories'][0]['parentId']=value['categories'][0]['id']
        with self.assertRaises(ValueError):service.KnowledgeBackup.model_validate(value)
        value=backup.model_dump(mode='json');value['notes'][0]['categoryId']='missing'
        with self.assertRaises(ValueError):service.KnowledgeBackup.model_validate(value)
        value=backup.model_dump(mode='json');value['versions'][0]['noteId']=''
        with self.assertRaises(ValueError):service.KnowledgeBackup.model_validate(value)
        async with self.sessions() as s:
            self.assertEqual(await s.scalar(select(func.count()).select_from(Note).where(Note.user_id=='other')),0)

    async def test_missing_attachment_refuses_incomplete_export(self):
        item=await self.seed();await self.service.storage.delete(item.storage_key)
        with self.assertRaises(FileNotFoundError):await service.export_knowledge('owner')

    async def test_routes_validate_auth_and_archive_before_accepting(self):
        await self.seed();app=FastAPI();app.include_router(routes.router,prefix='/api')
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://test') as client:
            self.assertEqual((await client.get('/api/knowledge-backup')).status_code,401)
            export=await client.get('/api/knowledge-backup',headers=self.headers());self.assertEqual(export.status_code,200)
            self.assertEqual(export.headers['cache-control'],'private, no-store')
            response=await client.post('/api/knowledge-backup/import',headers={**self.headers('other'),'X-Import-Id':'route-test'},json=export.json())
            self.assertEqual(response.status_code,200,response.text);self.assertTrue(response.json()['ok'])
            bad=await client.post('/api/knowledge-backup/import',headers={**self.headers(),'X-Import-Id':'bad'},json={'notes':[{'title':'invalid'}]})
            self.assertEqual(bad.status_code,422)

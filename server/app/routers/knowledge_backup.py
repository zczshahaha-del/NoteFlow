from __future__ import annotations

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from fastapi.responses import Response
from pydantic import ValidationError

from app.deps import CurrentUser, get_current_user
from app.services.knowledge_backup import KnowledgeBackup, MAX_BACKUP_BYTES, export_knowledge, import_knowledge

router = APIRouter(tags=['knowledge'])


@router.get('/knowledge-backup')
async def download_backup(user: CurrentUser = Depends(get_current_user)):
    try:
        backup = await export_knowledge(user.id)
    except (ValueError, FileNotFoundError) as exc:
        raise HTTPException(409, detail='备份未完成：'+str(exc)) from exc
    return Response(backup.model_dump_json(), media_type='application/json', headers={'Cache-Control':'private, no-store'})


@router.post('/knowledge-backup/import')
async def restore_backup(request: Request, x_import_id: str = Header(..., pattern=r'^[A-Za-z0-9_-]{1,64}$'),
                         user: CurrentUser = Depends(get_current_user)):
    raw = bytearray()
    async for chunk in request.stream():
        raw.extend(chunk)
        if len(raw) > MAX_BACKUP_BYTES: raise HTTPException(413, detail='备份超过 25 MB 导入限制')
    try:
        backup = KnowledgeBackup.model_validate_json(raw)
        result = await import_knowledge(user.id, backup, x_import_id)
    except ValidationError as exc:
        raise HTTPException(422, detail='备份格式或引用关系不正确') from exc
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc)) from exc
    return {'ok': True, **result}

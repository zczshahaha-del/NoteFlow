from __future__ import annotations

import mimetypes
import re
from datetime import datetime
from pathlib import Path
from typing import Optional
from urllib.parse import unquote

from fastapi import APIRouter, Body, Depends, Header, HTTPException, Response
from app.config import cfg
from app.deps import CurrentUser, get_current_user
from app.models.db import NoteAttachment
from app.services.attachment_service import AttachmentService

router = APIRouter(tags=["attachments"])


def _dt(value: Optional[datetime]) -> Optional[str]:
    return value.isoformat() if value else None


def _safe_filename(value: str) -> str:
    name = Path(unquote(value).strip().replace("\\", "/")).name
    name = re.sub(r"[^\w.()\-\u4e00-\u9fff ]+", "-", name).strip(" .-")
    return (name or "attachment")[:180]


def _attachment_out(item: NoteAttachment) -> dict:
    return {
        "id": item.id,
        "noteId": item.note_id,
        "fileName": item.file_name,
        "contentType": item.content_type,
        "size": item.size,
        "sha256": item.sha256,
        "downloadUrl": f"/api/attachments/{item.id}/content",
        "createdAt": _dt(item.created_at),
    }


def validated_content_type(file_name: str, claimed: str | None, content: bytes) -> str:
    content_type = (claimed or mimetypes.guess_type(file_name)[0] or "application/octet-stream").split(";", 1)[0].strip().lower()
    allowed = {"application/octet-stream", "application/pdf", "application/zip", "text/plain", "text/markdown", "text/csv", "image/png", "image/jpeg", "image/gif", "image/webp"}
    active_extensions = {".html", ".htm", ".xhtml", ".svg", ".xml", ".js", ".mjs"}
    if content_type in {"text/html", "application/xhtml+xml", "image/svg+xml", "text/javascript", "application/javascript", "text/xml", "application/xml"} or Path(file_name).suffix.lower() in active_extensions:
        raise HTTPException(status_code=415, detail="unsupported or active attachment type")
    if re.search(rb"<(?:!doctype\s+html|html|svg|script)\b", content[:4096].lstrip().lower()):
        raise HTTPException(status_code=415, detail="active HTML/SVG attachments are not allowed")
    image_checks = {
        "image/png": content.startswith(b"\x89PNG\r\n\x1a\n"),
        "image/jpeg": content.startswith(b"\xff\xd8\xff"),
        "image/gif": content.startswith((b"GIF87a", b"GIF89a")),
        "image/webp": content.startswith(b"RIFF") and content[8:12] == b"WEBP",
    }
    if content_type in image_checks and not image_checks[content_type]:
        raise HTTPException(status_code=415, detail="image content does not match its type")
    # Unknown document formats remain downloadable without trusting their MIME.
    return content_type if content_type in allowed else "application/octet-stream"


def attachment_response(item: NoteAttachment, content: bytes) -> Response:
    # Historical uploads may contain active formats; only verified raster images
    # are inline. Everything else is downloaded with a sandbox and no MIME sniff.
    try:
        kind = validated_content_type(item.file_name, item.content_type, content)
    except HTTPException:
        kind = "application/octet-stream"
    disposition = "inline" if kind.startswith("image/") else "attachment"
    return Response(content=content, media_type=kind, headers={
        "Content-Disposition": f'{disposition}; filename="{item.file_name.encode("ascii", "ignore").decode() or "attachment"}"',
        "Cache-Control": "private, no-store",
        "X-Content-Type-Options": "nosniff",
        "Content-Security-Policy": "default-src 'none'; sandbox",
    })


@router.post("/attachments")
async def upload_attachment(
    content: bytes = Body(..., media_type="application/octet-stream"),
    x_file_name: str = Header("attachment", alias="X-File-Name"),
    x_note_id: Optional[str] = Header(None, alias="X-Note-Id"),
    x_content_type: Optional[str] = Header(None, alias="X-Content-Type"),
    user: CurrentUser = Depends(get_current_user),
):
    if not content:
        raise HTTPException(status_code=400, detail="attachment is empty")
    if len(content) > cfg.ATTACHMENT_MAX_BYTES:
        raise HTTPException(status_code=413, detail="attachment exceeds size limit")

    file_name = _safe_filename(x_file_name)
    content_type = validated_content_type(file_name, x_content_type, content)

    try:
        item = await AttachmentService().create(
            user_id=user.id,
            note_id=x_note_id,
            file_name=file_name,
            content_type=content_type,
            content=content,
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return {"attachment": _attachment_out(item)}


@router.get("/notes/{note_id}/attachments")
async def list_note_attachments(note_id: str, user: CurrentUser = Depends(get_current_user)):
    items = await AttachmentService().list_for_note(user.id, note_id)
    return {"attachments": [_attachment_out(item) for item in items]}


@router.get("/attachments/{attachment_id}")
async def download_attachment(attachment_id: str, user: CurrentUser = Depends(get_current_user)):
    try:
        item, content = await AttachmentService().read(user.id, attachment_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except FileNotFoundError as error:
        raise HTTPException(status_code=410, detail="attachment content is missing") from error
    return attachment_response(item, content)


@router.get("/attachments/{attachment_id}/content")
async def read_attachment_content(attachment_id: str, user: CurrentUser = Depends(get_current_user)):
    # Old saved URLs may still carry sig=. The session is always authoritative;
    # possession of a historical URL grants no access and logout revokes access.
    return await download_attachment(attachment_id, user)


@router.delete("/attachments/{attachment_id}")
async def delete_attachment(attachment_id: str, user: CurrentUser = Depends(get_current_user)):
    try:
        await AttachmentService().delete(user.id, attachment_id)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    return {"ok": True}

from __future__ import annotations

import re

from app.models.db import NoteDraft, NoteDraftSection
from app.services.heading_numbers import draft_numbering_style, number_markdown_headings


def clean_draft_title(value: str) -> str:
    title = value.strip().removesuffix(".md").strip()
    return title[:255] or "未命名笔记"


def assemble_draft_content(draft: NoteDraft, sections: list[NoteDraftSection]) -> str:
    title_pattern = re.compile(rf"(^|\n{{2,}})#\s+{re.escape(draft.title)}\s*\n+", re.IGNORECASE)
    ordered = sorted(
        [section for section in sections if section.deleted_at is None],
        key=lambda item: item.sort_order,
    )
    body_parts: list[str] = []
    for section in ordered:
        content = (section.content or "").strip()
        if content:
            body_parts.append(title_pattern.sub(lambda match: match.group(1), content).strip())
            continue
        heading = "#" * max(1, min(section.level, 4))
        outline_text = section.outline_text.strip()
        body_parts.append(outline_text if re.match(r"^#{1,6}\s+", outline_text) else f"{heading} {section.title}\n\n{outline_text}".strip())
    if body_parts:
        return number_markdown_headings("\n\n".join(body_parts).strip(), draft.title, style=draft_numbering_style(draft))
    return number_markdown_headings((draft.assembled_content or draft.outline or "").strip(), draft.title, style=draft_numbering_style(draft))

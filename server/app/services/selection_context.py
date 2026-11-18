from __future__ import annotations

import re
from typing import Any


SELECTION_LIMIT = 4000
HEADING_LIMIT = 120
NEARBY_LIMIT = 400

_QUOTED = re.compile(r'“[^”]*”|「[^」]*」|『[^』]*』|"[^"]*"|`[^`]*`')
_WHOLE_NOTE = re.compile(
    r"(?:结合|参考|根据|基于|通读|查阅|看|总结|概括)"
    r"(?:一下|这篇笔记的?|这篇文章的?|这篇|本篇|当前笔记的?|当前文章的?|当前)*"
    r"(?:全文|全篇|整篇(?:笔记|文章)?|整个(?:笔记|文章))"
)
_DENIAL = re.compile(r"(?<!能)不$|(?:不要|不用|无需|不必|不需要|(?<!能)不能|不可|不允许|别|禁止|避免|不是要)[^，。！？；,!?;\n]{0,8}$")


def has_selection_scope(state: dict[str, Any]) -> bool:
    return state.get("contextScope") == "selection" and bool(str(state.get("selectedText") or "").strip())


def asks_for_whole_note(question: str) -> bool:
    text = re.sub(r"\s+", "", _QUOTED.sub(" ", question))
    matches = list(_WHOLE_NOTE.finditer(text))
    return bool(matches) and all(not _DENIAL.search(text[:match.start()]) for match in matches)


def whole_note_id(state: dict[str, Any], question: str) -> str:
    note_id = str(state.get("currentNoteId") or "").strip()
    referenced = state.get("contextScope") == "current_note" or has_selection_scope(state)
    return note_id if note_id and referenced and asks_for_whole_note(question) else ""


def selection_context_text(state: dict[str, Any]) -> str:
    context = state.get("selectionContext") or {}
    selected = str(state.get("selectedText") or "").strip()[:SELECTION_LIMIT]
    heading = str(context.get("heading") or "").strip()[:HEADING_LIMIT]
    before = str(context.get("before") or "").strip()[-NEARBY_LIMIT:]
    after = str(context.get("after") or "").strip()[:NEARBY_LIMIT]
    parts = [("小节标题", heading), ("选区前", before), ("选中文字", selected), ("选区后", after)]
    return "\n\n".join(f"{label}：\n{text}" for label, text in parts if text)

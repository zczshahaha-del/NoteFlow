from __future__ import annotations

from typing import Optional

from pydantic import BaseModel, Field

from app.services.selection_context import HEADING_LIMIT, NEARBY_LIMIT


class AgentChatMessageIn(BaseModel):
    role: str
    text: str


class SelectionSurroundings(BaseModel):
    heading: str = Field(default="", max_length=HEADING_LIMIT)
    before: str = Field(default="", max_length=NEARBY_LIMIT)
    after: str = Field(default="", max_length=NEARBY_LIMIT)


class AgentChatPageState(BaseModel):
    currentNoteId: Optional[str] = None
    selectedText: str = ""
    selectionContext: SelectionSurroundings | None = None
    currentSectionId: Optional[str] = None
    dirty: bool = False
    unsavedContent: str = ""
    centerMode: Optional[str] = None
    activeEditPreviewId: Optional[str] = None
    draftSeed: str = ""
    activeDraftId: Optional[str] = None
    activeDraftTitle: str = ""
    activeDraftTopic: str = ""
    contextScope: str = "auto"


class AgentChatPayload(BaseModel):
    sessionId: Optional[str] = None
    question: str
    mode: str = "chat"
    retryAnswerOnly: bool = False
    retryOfRunId: Optional[str] = None
    documentTitle: str = ""
    documentContent: str = ""
    history: list[AgentChatMessageIn] = Field(default_factory=list)
    maxTokens: int = 0
    temperature: Optional[float] = None
    pageState: Optional[AgentChatPageState] = None
    memoryEnabled: bool = True


class CheckpointBindPayload(BaseModel):
    editPreviewId: Optional[str] = None
    draftId: Optional[str] = None
    payload: dict = Field(default_factory=dict)


class CheckpointResolvePayload(BaseModel):
    status: str = "resolved"
    payload: dict = Field(default_factory=dict)


class RunCancelPayload(BaseModel):
    reason: str = "user_cancelled"


class ChatSessionCreatePayload(BaseModel):
    currentNoteId: Optional[str] = None


class ChatSessionUpdatePayload(BaseModel):
    title: str = Field(min_length=1, max_length=255)

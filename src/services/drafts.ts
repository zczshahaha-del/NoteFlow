import { apiFetch, readApiError, readApiJson } from "./api";
import type { NoteIndexJobRecord, NoteRecord } from "./notes";

export type NoteDraftStatus =
  | "configuring"
  | "outline_ready"
  | "generating"
  | "assembled"
  | "saved"
  | "canceled"
  | "failed"
  | string;

export type NoteDraftSectionStatus =
  | "outline_only"
  | "generating"
  | "generated"
  | "needs_revision"
  | "confirmed"
  | "deleted"
  | "failed"
  | string;

export interface NoteDraftSectionRecord {
  id: string;
  draftId: string;
  title: string;
  level: number;
  sortOrder: number;
  outlineText: string;
  content: string;
  status: NoteDraftSectionStatus;
  createdAt: string | null;
  updatedAt: string | null;
  deletedAt: string | null;
  confirmedAt: string | null;
}

export interface NoteDraftRecord {
  id: string;
  title: string;
  topic: string;
  categoryId: string | null;
  noteType: string;
  writingTone: string;
  noteFormat: string;
  headingLevel: string;
  includeCode: boolean;
  includeExercises: boolean;
  extraRequest: string;
  draftConfig: Record<string, unknown>;
  bodyInstruction: string;
  outline: string;
  assembledContent: string;
  status: NoteDraftStatus;
  savedNoteId: string | null;
  createdAt: string | null;
  updatedAt: string | null;
  canceledAt: string | null;
  savedAt: string | null;
  sections: NoteDraftSectionRecord[];
}

export interface DraftSectionPayload {
  id?: string;
  title: string;
  level?: number;
  sortOrder?: number;
  outlineText?: string;
  content?: string;
  status?: NoteDraftSectionStatus;
}

export interface DraftCreatePayload {
  title?: string;
  topic: string;
  categoryId?: string | null;
  noteType: string;
  writingTone: string;
  noteFormat: string;
  headingLevel: string;
  includeCode: boolean;
  includeExercises: boolean;
  extraRequest: string;
  draftConfig?: Record<string, unknown>;
  checkpointId?: string;
  outline?: string;
  sections?: DraftSectionPayload[];
  bodyInstruction?: string;
}

export interface DraftUpdatePayload {
  expectedUpdatedAt?: string;
  title?: string;
  categoryId?: string | null;
  outline?: string;
  assembledContent?: string;
  status?: NoteDraftStatus;
  sections?: DraftSectionPayload[];
  bodyInstruction?: string;
  regenerateGenerated?: boolean;
  sectionOrder?: string[];
  generationBrief?: import("../utils/draftBrief").DraftBrief;
  includeCode?: boolean;
  includeExercises?: boolean;
}

export interface DraftSectionUpdatePayload {
  title?: string;
  level?: number;
  sortOrder?: number;
  outlineText?: string;
  content?: string;
  status?: NoteDraftSectionStatus;
  source?: string;
  generationKey?: string;
}

export class DraftSectionConflictError extends Error {}

export async function createNoteDraft(payload: DraftCreatePayload): Promise<NoteDraftRecord> {
  const response = await apiFetch("/api/note-drafts", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) throw new Error(await readApiError(response));
  const data = await readApiJson<{ draft: NoteDraftRecord }>(response);
  return data.draft;
}

export async function getNoteDraft(draftId: string): Promise<NoteDraftRecord> {
  const response = await apiFetch(`/api/note-drafts/${encodeURIComponent(draftId)}`);
  if (!response.ok) throw new Error(await readApiError(response));
  const data = await readApiJson<{ draft: NoteDraftRecord }>(response);
  return data.draft;
}

export async function updateNoteDraft(
  draftId: string,
  payload: DraftUpdatePayload
): Promise<NoteDraftRecord> {
  const response = await apiFetch(`/api/note-drafts/${encodeURIComponent(draftId)}`, {
    method: "PUT",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) throw new Error(await readApiError(response));
  const data = await readApiJson<{ draft: NoteDraftRecord }>(response);
  return data.draft;
}

async function writeDraftSection(
  draftId: string,
  sectionId: string,
  payload: DraftSectionUpdatePayload,
  method: "PUT" | "POST" = "PUT",
) {
  const response = await apiFetch(
    `/api/note-drafts/${encodeURIComponent(draftId)}/sections/${encodeURIComponent(sectionId)}${method === "POST" ? "/generate" : ""}`,
    {
      method,
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }
  );
  if (!response.ok) {
    const message = await readApiError(response);
    if (response.status === 409) throw new DraftSectionConflictError(message);
    throw new Error(message);
  }
  return readApiJson<{ draft: NoteDraftRecord; generationKey?: string }>(response);
}

export async function updateDraftSection(
  draftId: string,
  sectionId: string,
  payload: DraftSectionUpdatePayload
): Promise<NoteDraftRecord> {
  return (await writeDraftSection(draftId, sectionId, payload)).draft;
}

export async function startDraftSectionGeneration(draftId: string, sectionId: string): Promise<string> {
  const { generationKey } = await writeDraftSection(draftId, sectionId, {
    status: "generating",
    source: "generate_section",
  });
  if (!generationKey) throw new Error("未能启动章节生成，请刷新后重试。");
  return generationKey;
}

export async function createDraftSection(
  draftId: string,
  payload: DraftSectionPayload
): Promise<NoteDraftRecord> {
  const response = await apiFetch(`/api/note-drafts/${encodeURIComponent(draftId)}/sections`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  if (!response.ok) throw new Error(await readApiError(response));
  const data = await readApiJson<{ draft: NoteDraftRecord }>(response);
  return data.draft;
}

export async function generateDraftSection(
  draftId: string,
  sectionId: string,
  payload: DraftSectionUpdatePayload
): Promise<NoteDraftRecord> {
  return (await writeDraftSection(draftId, sectionId, payload, "POST")).draft;
}

export async function confirmDraftSection(
  draftId: string,
  sectionId: string
): Promise<NoteDraftRecord> {
  const response = await apiFetch(
    `/api/note-drafts/${encodeURIComponent(draftId)}/sections/${encodeURIComponent(sectionId)}/confirm`,
    { method: "POST" }
  );
  if (!response.ok) throw new Error(await readApiError(response));
  const data = await readApiJson<{ draft: NoteDraftRecord }>(response);
  return data.draft;
}

export async function deleteDraftSection(
  draftId: string,
  sectionId: string
): Promise<NoteDraftRecord> {
  const response = await apiFetch(
    `/api/note-drafts/${encodeURIComponent(draftId)}/sections/${encodeURIComponent(sectionId)}/delete`,
    { method: "POST" }
  );
  if (!response.ok) throw new Error(await readApiError(response));
  const data = await readApiJson<{ draft: NoteDraftRecord }>(response);
  return data.draft;
}

export async function restoreDraftSection(
  draftId: string,
  sectionId: string
): Promise<NoteDraftRecord> {
  const response = await apiFetch(
    `/api/note-drafts/${encodeURIComponent(draftId)}/sections/${encodeURIComponent(sectionId)}/restore`,
    { method: "POST" }
  );
  if (!response.ok) throw new Error(await readApiError(response));
  const data = await readApiJson<{ draft: NoteDraftRecord }>(response);
  return data.draft;
}

export async function assembleNoteDraft(
  draftId: string,
  content?: string,
  expectedUpdatedAt?: string
): Promise<NoteDraftRecord> {
  const response = await apiFetch(`/api/note-drafts/${encodeURIComponent(draftId)}/assemble`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ content, expectedUpdatedAt }),
  });
  if (!response.ok) throw new Error(await readApiError(response));
  const data = await readApiJson<{ draft: NoteDraftRecord }>(response);
  return data.draft;
}

export async function generateAllDraftSections(
  draftId: string,
  retryFailed = false
): Promise<NoteDraftRecord> {
  const response = await apiFetch(
    `/api/note-drafts/${encodeURIComponent(draftId)}/generate-all`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ retryFailed }),
    }
  );
  if (!response.ok) throw new Error(await readApiError(response));
  const data = await readApiJson<{ draft: NoteDraftRecord }>(response);
  return data.draft;
}

export async function stopAllDraftSections(draftId: string): Promise<NoteDraftRecord> {
  const response = await apiFetch(
    `/api/note-drafts/${encodeURIComponent(draftId)}/generate-all/stop`,
    { method: "POST" }
  );
  if (!response.ok) throw new Error(await readApiError(response));
  const data = await readApiJson<{ draft: NoteDraftRecord }>(response);
  return data.draft;
}

export async function saveDraftToNotes(
  draftId: string,
  payload: { categoryId?: string | null; title?: string; confirm: boolean }
): Promise<{ draft: NoteDraftRecord; note: NoteRecord; job: NoteIndexJobRecord }> {
  const response = await apiFetch(
    `/api/note-drafts/${encodeURIComponent(draftId)}/save-to-notes`,
    {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    }
  );
  if (!response.ok) throw new Error(await readApiError(response));
  return readApiJson<{ draft: NoteDraftRecord; note: NoteRecord; job: NoteIndexJobRecord }>(
    response
  );
}

export async function cancelNoteDraft(draftId: string): Promise<NoteDraftRecord> {
  const response = await apiFetch(`/api/note-drafts/${encodeURIComponent(draftId)}/cancel`, {
    method: "POST",
  });
  if (!response.ok) throw new Error(await readApiError(response));
  const data = await readApiJson<{ draft: NoteDraftRecord }>(response);
  return data.draft;
}

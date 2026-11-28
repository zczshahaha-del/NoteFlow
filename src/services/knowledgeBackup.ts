import { apiFetch, readApiError, readApiJson } from './api';
import type { KnowledgeBackup } from '../utils/knowledgeBackup';

export interface ImportResult { ok: boolean; notesCreated:number; categoriesCreated:number; attachmentsCreated:number; selectedNoteId:string|null }
export async function exportKnowledgeBackup(): Promise<KnowledgeBackup> {
  const response=await apiFetch('/api/knowledge-backup');
  if(!response.ok)throw new Error(await readApiError(response));
  return readApiJson<KnowledgeBackup>(response);
}
export async function importKnowledgeBackup(backup:KnowledgeBackup, requestId:string):Promise<ImportResult> {
  const response=await apiFetch('/api/knowledge-backup/import',{method:'POST',headers:{'Content-Type':'application/json','X-Import-Id':requestId},body:JSON.stringify(backup)});
  if(!response.ok)throw new Error(await readApiError(response));
  return readApiJson<ImportResult>(response);
}

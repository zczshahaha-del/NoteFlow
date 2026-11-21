import type { KnowledgeBaseSnapshot } from "../services/knowledgeBase";
import type { FileNode } from "../types";
import { findFileById } from "../workspaceTree";
import { updateFileContentInTree, updateFileMetadata } from "./tree";
import { noteJournalScope, previousNoteJournalScope } from "./noteJournalScope";

const BUNDLED_DEMO_FILE_IDS = new Set([
  "redis-cache-3-problems",
  "redis-distributed-lock",
  "redis-persistence",
  "redis-data-types",
  "redis-cluster",
]);

function userStorageKey(userId: string, suffix: string): string {
  return `noteflow:${userId}:${suffix}`;
}

function loadFromStorage<T>(key: string, fallback: T): T {
  try {
    const raw = localStorage.getItem(key);
    if (raw) return JSON.parse(raw);
  } catch {}
  return fallback;
}

function saveToStorage(key: string, value: unknown) {
  try {
    localStorage.setItem(key, JSON.stringify(value));
    return true;
  } catch { return false; }
}

// A synchronous, per-note journal: typing never serializes other notes, and there
// is no debounce window in which closing the tab would lose the last characters.
function draftPrefix(userId: string) { return userStorageKey(userId, "note-draft:"); }
function scopedDraftPrefix(userId: string) { return userStorageKey(userId, "note-draft-by-tab:"); }
function scopedDraftKey(userId: string, noteId: string, scope = noteJournalScope()) {
  return `${scopedDraftPrefix(userId)}${scope}:${noteId}`;
}
interface NoteEditBase { updatedAt: string | null; contentHash: string | null }
interface PendingNoteEdit extends NoteEditBase { content: string }
interface RecoverableNoteEdit extends PendingNoteEdit { noteId: string; scope: string }

function pendingEdit(value: unknown): PendingNoteEdit | null {
  if (!value || typeof value !== "object" || !("content" in value) || typeof value.content !== "string") return null;
  const { content, updatedAt, contentHash } = value as PendingNoteEdit;
  return { content, updatedAt: typeof updatedAt === "string" ? updatedAt : null,
    contentHash: typeof contentHash === "string" ? contentHash : null };
}

export function saveLocalNoteContent(userId: string, noteId: string, content: string, base?: NoteEditBase) {
  return base ? saveToStorage(scopedDraftKey(userId, noteId), { ...base, content })
    : saveToStorage(draftPrefix(userId) + noteId, content);
}

export function rebaseLocalNoteContent(userId: string, noteId: string, content: string, base: NoteEditBase) {
  const previous = pendingEdit(loadFromStorage<unknown>(scopedDraftKey(userId, noteId), null));
  if (previous?.content === content) saveLocalNoteContent(userId, noteId, content, base);
}

export function hasPendingLocalNoteContent(userId: string, noteId: string, content: string) {
  return pendingEdit(loadFromStorage<unknown>(scopedDraftKey(userId, noteId), null))?.content === content;
}

function restoreValue(userId: string, noteId: string): unknown {
  const own = loadFromStorage<unknown>(scopedDraftKey(userId, noteId), null);
  if (own !== null) return own;
  const previous = previousNoteJournalScope();
  return (previous && loadFromStorage<unknown>(scopedDraftKey(userId, noteId, previous), null))
    ?? loadFromStorage<unknown>(draftPrefix(userId) + noteId, null);
}

export function listRecoverableNoteEdits(userId: string): RecoverableNoteEdit[] {
  const prefix = scopedDraftPrefix(userId);
  const records: RecoverableNoteEdit[] = [];
  try {
    for (const key of Object.keys(localStorage)) {
      if (!key.startsWith(prefix)) continue;
      const suffix = key.slice(prefix.length);
      const separator = suffix.indexOf(":");
      if (separator < 1) continue;
      const scope = suffix.slice(0, separator);
      if (scope === noteJournalScope()) continue;
      const edit = pendingEdit(loadFromStorage<unknown>(key, null));
      if (edit) records.push({ ...edit, scope, noteId: suffix.slice(separator + 1) });
    }
  } catch {}
  return records;
}

export function readRecoverableNoteEdit(userId: string, noteId: string, scope: string) {
  return pendingEdit(loadFromStorage<unknown>(scopedDraftKey(userId, noteId, scope), null));
}

export function clearRecoveredNoteEdit(userId: string, noteId: string, scope: string, edit: PendingNoteEdit) {
  try {
    const key = scopedDraftKey(userId, noteId, scope);
    const current = pendingEdit(loadFromStorage<unknown>(key, null));
    if (current?.content === edit.content && current.updatedAt === edit.updatedAt && current.contentHash === edit.contentHash) {
      localStorage.removeItem(key);
    }
  } catch {}
}

export function loadPendingNoteEdits(userId: string, tree: FileNode[]): Map<string, PendingNoteEdit> {
  const edits = new Map<string, PendingNoteEdit>();
  const visit = (nodes: FileNode[]) => {
    for (const node of nodes) {
      if (node.type === "file") {
        const value = restoreValue(userId, node.id);
        const edit = typeof value === "string" ? {
          content: value, updatedAt: node.updatedAt ?? null, contentHash: node.contentHash ?? null,
        } : pendingEdit(value);
        if (edit) {
          if (!hasPendingLocalNoteContent(userId, node.id, edit.content)) saveLocalNoteContent(userId, node.id, edit.content, edit);
          edits.set(node.id, edit);
        }
      } else if (node.children) visit(node.children);
    }
  };
  visit(tree);
  return edits;
}

export function clearLocalNoteContentIfMatches(userId: string, noteId: string, content: string) {
  try {
    for (const key of [scopedDraftKey(userId, noteId), draftPrefix(userId) + noteId]) {
      const item = loadFromStorage<unknown>(key, null);
      if (item === content || pendingEdit(item)?.content === content) localStorage.removeItem(key);
    }
  } catch {}
}

function findFirstFileId(nodes: FileNode[]): string | null {
  for (const node of nodes) {
    if (node.type === "file") return node.id;
    const childId = node.children ? findFirstFileId(node.children) : null;
    if (childId) return childId;
  }
  return null;
}

export function resolveSelectedFileId(
  treeData: FileNode[],
  preferred: string | null
): string | null {
  if (preferred === null) return null;
  if (preferred && findFileById(treeData, preferred)) return preferred;
  return findFirstFileId(treeData);
}

function collectBundledDemoFileIds(nodes: FileNode[]): string[] {
  return nodes.flatMap((node) => {
    if (node.type === "file") return [node.id];
    return node.children ? collectBundledDemoFileIds(node.children) : [];
  });
}

export function isBundledDemoSnapshot(
  snapshot: KnowledgeBaseSnapshot
): boolean {
  const fileIds = collectBundledDemoFileIds(snapshot.treeData);
  return (
    fileIds.length === BUNDLED_DEMO_FILE_IDS.size &&
    fileIds.every((id) => BUNDLED_DEMO_FILE_IDS.has(id))
  );
}

export function loadLocalSnapshot(userId: string): KnowledgeBaseSnapshot {
  let treeData = loadFromStorage<FileNode[]>(
    userStorageKey(userId, "tree-data"),
    []
  );
  let fileContents = loadFromStorage<Record<string, string>>(
    userStorageKey(userId, "file-contents"),
    {}
  );
  if (isBundledDemoSnapshot({ treeData, fileContents, selectedFileId: null })) {
    treeData = [];
    fileContents = {};
  }
  // Only hydrate live note IDs from the authoritative tree, never resurrect a
  // deleted note from a journal. Empty strings are valid edits.
  const restore = (nodes: FileNode[]) => {
    for (const node of nodes) {
      if (node.type === "file") {
        const value = restoreValue(userId, node.id);
        const edit = pendingEdit(value);
        const content = edit?.content ?? value;
        if (typeof content === "string") {
          fileContents[node.id] = content;
          treeData = updateFileContentInTree(treeData, node.id, content);
          if (edit) treeData = updateFileMetadata(treeData, node.id, {
            updatedAt: edit.updatedAt ?? undefined, contentHash: edit.contentHash ?? undefined,
          });
        }
      } else if (node.children) restore(node.children);
    }
  };
  restore(treeData);
  const selectedFileId = resolveSelectedFileId(
    treeData,
    loadFromStorage<string | null>(
      userStorageKey(userId, "selected-file-id"),
      null
    )
  );

  return { treeData, fileContents, selectedFileId };
}

export function saveLocalSnapshot(
  userId: string,
  snapshot: KnowledgeBaseSnapshot,
  pendingNoteIds = new Set<string>(),
) {
  let { treeData, fileContents } = snapshot;
  if (pendingNoteIds.size) {
    fileContents = { ...fileContents };
    const withoutPendingBodies = (nodes: FileNode[]): FileNode[] => nodes.map(node => {
      if (node.type === "file" && pendingNoteIds.has(node.id)) return { ...node, content: undefined };
      return node.children ? { ...node, children: withoutPendingBodies(node.children) } : node;
    });
    treeData = withoutPendingBodies(treeData);
    for (const id of pendingNoteIds) delete fileContents[id];
  }
  const contentsSaved = saveToStorage(userStorageKey(userId, "file-contents"), fileContents);
  const treeSaved = saveToStorage(userStorageKey(userId, "tree-data"), treeData);
  const selectionSaved = saveToStorage(
    userStorageKey(userId, "selected-file-id"),
    snapshot.selectedFileId
  );
  // Compact only after both baseline writes succeed. On quota/storage failure,
  // retain the journal so the last successfully written edit is recoverable.
  if (contentsSaved && treeSaved) {
    try {
      const prefix = draftPrefix(userId);
      const keys = Array.from({ length: localStorage.length }, (_, index) => localStorage.key(index));
      for (const key of keys) {
        if (!key?.startsWith(prefix)) continue;
        const noteId = key.slice(prefix.length);
        const content = snapshot.fileContents[noteId];
        // Another tab may have journaled a draft that this snapshot lacks.
        if ((content === undefined && !findFileById(snapshot.treeData, noteId)) ||
            (content !== undefined && loadFromStorage<unknown>(key, null) === content)) localStorage.removeItem(key);
      }
    } catch {}
  }
  return contentsSaved && treeSaved && selectionSaved;
}

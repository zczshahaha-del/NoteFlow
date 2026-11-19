import type { KnowledgeBaseSnapshot } from "../services/knowledgeBase";
import type { FileNode } from "../types";
import { findFileById } from "../workspaceTree";
import { updateFileContentInTree, updateFileMetadata } from "./tree";

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
interface NoteEditBase { updatedAt: string | null; contentHash: string | null }
interface PendingNoteEdit extends NoteEditBase { content: string }

function pendingEdit(value: unknown): PendingNoteEdit | null {
  if (!value || typeof value !== "object" || !("content" in value) || typeof value.content !== "string") return null;
  const { content, updatedAt, contentHash } = value as PendingNoteEdit;
  return { content, updatedAt: typeof updatedAt === "string" ? updatedAt : null,
    contentHash: typeof contentHash === "string" ? contentHash : null };
}

export function saveLocalNoteContent(userId: string, noteId: string, content: string, base?: NoteEditBase) {
  return saveToStorage(draftPrefix(userId) + noteId, base ? { ...base, content } : content);
}

export function rebaseLocalNoteContent(userId: string, noteId: string, content: string, base: NoteEditBase) {
  const previous = pendingEdit(loadFromStorage<unknown>(draftPrefix(userId) + noteId, null));
  if (previous?.content === content) saveLocalNoteContent(userId, noteId, content, base);
}

export function hasPendingLocalNoteContent(userId: string, noteId: string, content: string) {
  return pendingEdit(loadFromStorage<unknown>(draftPrefix(userId) + noteId, null))?.content === content;
}

export function loadPendingNoteEdits(userId: string, tree: FileNode[]): Map<string, PendingNoteEdit> {
  const edits = new Map<string, PendingNoteEdit>();
  const visit = (nodes: FileNode[]) => {
    for (const node of nodes) {
      if (node.type === "file") {
        const value = loadFromStorage<unknown>(draftPrefix(userId) + node.id, null);
        const edit = typeof value === "string" ? {
          content: value, updatedAt: node.updatedAt ?? null, contentHash: node.contentHash ?? null,
        } : pendingEdit(value);
        if (edit && typeof value === "string") saveLocalNoteContent(userId, node.id, value, edit);
        if (edit) edits.set(node.id, edit);
      } else if (node.children) visit(node.children);
    }
  };
  visit(tree);
  return edits;
}

export function clearLocalNoteContentIfMatches(userId: string, noteId: string, content: string) {
  try {
    const key = draftPrefix(userId) + noteId;
    const item = loadFromStorage<unknown>(key, null);
    if (item === content || pendingEdit(item)?.content === content) localStorage.removeItem(key);
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
        const value = loadFromStorage<unknown>(draftPrefix(userId) + node.id, null);
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
  snapshot: KnowledgeBaseSnapshot
) {
  const contentsSaved = saveToStorage(userStorageKey(userId, "file-contents"), snapshot.fileContents);
  const treeSaved = saveToStorage(userStorageKey(userId, "tree-data"), snapshot.treeData);
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

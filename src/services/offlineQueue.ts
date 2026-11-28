export interface OfflineNoteEdit {
  key: string;
  userId: string;
  noteId: string;
  tabId?: string;
  content: string;
  baseUpdatedAt: string | null;
  baseContentHash?: string | null;
  queuedAt: string;
}

const DB_NAME = "noteflow-offline";
const STORE_NAME = "note_edits";
const DB_VERSION = 1;

function editKey(userId: string, noteId: string, tabId?: string): string {
  return tabId ? JSON.stringify([userId, tabId, noteId]) : `${userId}:${noteId}`;
}

function openDatabase(): Promise<IDBDatabase> {
  return new Promise((resolve, reject) => {
    const request = indexedDB.open(DB_NAME, DB_VERSION);
    request.onupgradeneeded = () => {
      if (!request.result.objectStoreNames.contains(STORE_NAME)) {
        request.result.createObjectStore(STORE_NAME, { keyPath: "key" });
      }
    };
    request.onsuccess = () => resolve(request.result);
    request.onerror = () => reject(request.error ?? new Error("无法打开离线存储"));
  });
}

async function withStore<T>(
  mode: IDBTransactionMode,
  operation: (store: IDBObjectStore, resolve: (value: T) => void, reject: (error: unknown) => void) => void
): Promise<T> {
  const database = await openDatabase();
  return new Promise<T>((resolve, reject) => {
    const transaction = database.transaction(STORE_NAME, mode);
    let result: T;
    operation(transaction.objectStore(STORE_NAME), value => { result = value; }, reject);
    transaction.oncomplete = () => { database.close(); resolve(result); };
    transaction.onabort = () => { database.close(); reject(transaction.error ?? new Error("离线存储事务失败")); };
  });
}

export async function queueOfflineNoteEdit(
  edit: Omit<OfflineNoteEdit, "key" | "queuedAt">
): Promise<OfflineNoteEdit> {
  const record: OfflineNoteEdit = {
    ...edit,
    key: editKey(edit.userId, edit.noteId, edit.tabId),
    queuedAt: new Date().toISOString(),
  };
  return withStore("readwrite", (store, resolve, reject) => {
    const request = store.put(record);
    request.onsuccess = () => resolve(record);
    request.onerror = () => reject(request.error);
  });
}

export async function listOfflineNoteEdits(userId: string): Promise<OfflineNoteEdit[]> {
  return withStore("readonly", (store, resolve, reject) => {
    const request = store.getAll();
    request.onsuccess = () => resolve(
      (request.result as OfflineNoteEdit[])
        .filter((record) => record.userId === userId)
        .sort((left, right) => left.queuedAt.localeCompare(right.queuedAt))
    );
    request.onerror = () => reject(request.error);
  });
}

export async function getOfflineNoteEdit(key: string): Promise<OfflineNoteEdit | undefined> {
  return withStore("readonly", (store, resolve, reject) => {
    const request = store.get(key);
    request.onsuccess = () => resolve(request.result as OfflineNoteEdit | undefined);
    request.onerror = () => reject(request.error);
  });
}

export async function removeOfflineNoteEdit(expected: OfflineNoteEdit): Promise<void> {
  return withStore("readwrite", (store, resolve, reject) => {
    const key = expected.key;
    const lookup = store.get(key);
    lookup.onsuccess = () => {
      const current = lookup.result as OfflineNoteEdit | undefined;
      if (current?.queuedAt === expected.queuedAt && current.content === expected.content &&
          current.baseContentHash === expected.baseContentHash && current.baseUpdatedAt === expected.baseUpdatedAt) {
        const request = store.delete(key);
        request.onsuccess = () => resolve();
        request.onerror = () => reject(request.error);
      } else resolve();
    };
    lookup.onerror = () => reject(lookup.error);
  });
}

export function hasRemoteConflict(
  baseContentHash: string | null | undefined,
  currentContentHash: string | null | undefined,
  baseUpdatedAt: string | null,
  currentUpdatedAt: string | null,
  remoteContent: string,
  localContent: string
): boolean {
  if (remoteContent === localContent) return false;
  if (baseContentHash && currentContentHash) return baseContentHash !== currentContentHash;
  if (!baseUpdatedAt || !currentUpdatedAt) return false;
  return new Date(currentUpdatedAt).getTime() !== new Date(baseUpdatedAt).getTime();
}

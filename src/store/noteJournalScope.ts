// A duplicated tab can inherit sessionStorage. A browser lock distinguishes an
// active owner from a reload and prevents two documents writing the same journal.
const SCOPE_KEY = "noteflow:note-journal-tab";
const pageScope = globalThis.crypto?.randomUUID?.() ?? `${Date.now()}-${Math.random().toString(36).slice(2)}`;
let scope: string = pageScope;
let previousScope: string | null = null;
let initialization: Promise<void> | undefined;

const lockName = (id: string) => `noteflow:note-journal:${id}`;
const browserLocks = () => typeof navigator === "undefined" ? undefined : navigator.locks;

export function noteJournalScope() {
  return scope;
}

export function previousNoteJournalScope() {
  return previousScope;
}

function acquireScope(locks: LockManager, id: string): Promise<boolean> {
  return new Promise<boolean>(resolve => {
    void locks.request(lockName(id), { ifAvailable: true }, async lock => {
      if (!lock) {
        resolve(false);
        return;
      }
      await new Promise<void>(release => {
        window.addEventListener("pagehide", event => {
          if (!event.persisted) release();
        });
        resolve(true);
      });
    }).catch(() => resolve(false));
  }).catch(() => false);
}

export function prepareNoteJournal(): Promise<void> {
  if (!initialization) initialization = (async () => {
    const locks = browserLocks();
    let restored: string | null = null;
    try { restored = window.sessionStorage.getItem(SCOPE_KEY); } catch {}
    if (locks && restored && await acquireScope(locks, restored)) {
      scope = restored;
    } else {
      // Without Web Locks every document still has its own write key. A
      // reloaded page can read its prior records before starting new edits.
      if (!locks) previousScope = restored;
      if (locks) await acquireScope(locks, pageScope);
    }
    try { window.sessionStorage.setItem(SCOPE_KEY, scope); } catch {}
  })();
  return initialization;
}

export async function withInactiveNoteJournal(id: string, sync: () => Promise<void>): Promise<void> {
  if (id === scope) return;
  const locks = browserLocks();
  if (!locks) return sync();
  await locks.request(lockName(id), { ifAvailable: true }, async lock => {
    if (lock) await sync();
  });
}

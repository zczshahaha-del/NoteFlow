import { useEffect, useMemo, useRef, useState } from "react";
import { ChevronLeft, ChevronRight, Search } from "lucide-react";
import { memoryConsole } from "../services/memoryConsole";
import {
  deleteMemory, isMemoryEnabled, listMemories, loadMemorySettings, setMemoryEnabled, updateMemory,
  type UserMemoryRecord,
} from "../services/memories";

function readableMemory(memory: UserMemoryRecord) {
  const content = memory.content.trim();
  if (memory.provider !== "hindsight") return content;
  return content.split(/\s+\|\s*Involving:\s*/i, 1)[0].trim() || content;
}

// Recognize only explicit statements about the user's own name.
function nameClaim(text: string) {
  const name = "([\\p{L}·]{2,8})";
  const end = "(?=$|[，,。.!！？?、]|\\s)";
  const negative = text.match(new RegExp("^用户(?:澄清自己)?(?:不叫|的名字不是)[“「]?" + name + "[”」]?" + end, "u"));
  if (negative) return { name: negative[1], rejected: true };
  const positive = text.match(new RegExp("^用户(?:的名字是|名叫|叫)[“「]?" + name + "[”」]?" + end, "u"))
    ?? text.match(new RegExp("^用户表示[“「]" + name + "[”」]是准确的" + end, "u"));
  return positive ? { name: positive[1], rejected: false } : null;
}

const sectionLabels = { review: "需要核对", active: "在用", pending: "待确认", archived: "已停用", other: "其他记录" } as const;
type MemorySection = keyof typeof sectionLabels;

function groupMemories(memories: UserMemoryRecord[]) {
  const claims = memories
    .filter(memory => memory.status === "active" && (memory.scope ?? "global") === "global")
    .map(memory => nameClaim(readableMemory(memory)));
  const names = new Set(claims.filter(claim => claim && !claim.rejected).map(claim => claim?.name));
  const conflict = names.size > 1 || claims.some(claim => claim?.rejected && names.has(claim.name));
  const groups = new Map<string, { section: MemorySection; title: string; records: UserMemoryRecord[] }>();
  for (const memory of memories) {
    const text = readableMemory(memory);
    const claim = (memory.scope ?? "global") === "global" ? nameClaim(text) : null;
    const section = conflict && claim && memory.status === "active" ? "review"
      : memory.status === "active" || memory.status === "pending" || memory.status === "archived" ? memory.status : "other";
    const title = claim ? `${claim.rejected ? "不使用的称呼" : "称呼"}：${claim.name}` : text;
    const key = JSON.stringify([section, memory.scope ?? "global", title.replace(/\s+/g, " ")]);
    const existing = groups.get(key);
    if (existing) existing.records.push(memory);
    else groups.set(key, { section, title, records: [memory] });
  }
  return [...groups.values()];
}

export default function MemorySettings() {
  const [enabled, setEnabled] = useState(isMemoryEnabled);
  const [savingSetting, setSavingSetting] = useState(true);
  const [settingError, setSettingError] = useState("");
  const [memories, setMemories] = useState<UserMemoryRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [listError, setListError] = useState("");
  const [query, setQuery] = useState("");
  const [selected, setSelected] = useState<string | null>(null);
  const [draft, setDraft] = useState<string | null>(null);
  const [impact, setImpact] = useState<UserMemoryRecord[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [notice, setNotice] = useState("");
  const listRef = useRef<HTMLDivElement>(null);
  const backRef = useRef<HTMLButtonElement>(null);
  const editRef = useRef<HTMLTextAreaElement>(null);
  const cancelDeleteRef = useRef<HTMLButtonElement>(null);
  const editButtonRef = useRef<HTMLButtonElement>(null);
  const returnTo = useRef<string | null>(null);
  const listRequest = useRef(0);

  async function refresh() {
    const request = ++listRequest.current;
    setLoading(true);
    setListError("");
    try {
      const records = await listMemories(false);
      if (request === listRequest.current) setMemories(records.filter(record => record.status !== "deleted"));
    } catch (failure) {
      if (request === listRequest.current) setListError(failure instanceof Error ? failure.message : "记忆加载失败");
    } finally {
      if (request === listRequest.current) setLoading(false);
    }
  }

  useEffect(() => {
    let cancelled = false;
    void refresh();
    loadMemorySettings()
      .then(settings => { if (!cancelled) setEnabled(settings.memoryEnabled); })
      .catch(() => { if (!cancelled) setEnabled(isMemoryEnabled()); })
      .finally(() => { if (!cancelled) setSavingSetting(false); });
    return () => {
      cancelled = true;
      listRequest.current += 1;
    };
  }, []);

  const groups = useMemo(() => groupMemories(memories), [memories]);
  const group = groups.find(item => item.records.some(memory => memory.id === selected));
  const memory = group?.records.find(record => record.id === selected);
  const filter = query.trim().toLocaleLowerCase();
  const visible = groups.filter(item => !filter || item.title.toLocaleLowerCase().includes(filter)
    || item.records.some(record => readableMemory(record).toLocaleLowerCase().includes(filter)));

  useEffect(() => {
    if (selected) backRef.current?.focus();
    else if (returnTo.current) {
      const row = Array.from(listRef.current?.querySelectorAll<HTMLButtonElement>("[data-memory-id]") ?? [])
        .find(button => button.dataset.memoryId === returnTo.current);
      (row ?? listRef.current)?.focus();
      returnTo.current = null;
    }
  }, [selected]);

  const isEditing = draft !== null;
  const isConfirming = impact !== null;
  useEffect(() => { if (isEditing) editRef.current?.focus(); }, [isEditing]);
  useEffect(() => { if (isConfirming) cancelDeleteRef.current?.focus(); }, [isConfirming]);
  const restoreEditButton = useRef(false);
  useEffect(() => {
    if (!isEditing && !isConfirming && restoreEditButton.current) {
      editButtonRef.current?.focus();
      restoreEditButton.current = false;
    }
  }, [isEditing, isConfirming]);

  async function toggleMemory() {
    const next = !enabled;
    setEnabled(next);
    setSavingSetting(true);
    setSettingError("");
    try {
      setEnabled((await setMemoryEnabled(next)).memoryEnabled);
    } catch (failure) {
      setEnabled(!next);
      setSettingError(failure instanceof Error ? failure.message : "记忆设置保存失败");
    } finally {
      setSavingSetting(false);
    }
  }

  function selectRecord(id: string | null) {
    setSelected(id);
    setDraft(null);
    setImpact(null);
    setError("");
  }

  async function changeMemory(action: "save" | "review-delete" | "delete") {
    if (!memory || busy) return;
    setBusy(true);
    setError("");
    setNotice("");
    try {
      if (action === "save") {
        const content = draft?.trim();
        if (!content) return;
        const updated = await updateMemory(memory.id, {
          content, ...(memory.status === "pending" ? { status: "active" } : {}), reason: "profile_memory_edit",
        });
        setMemories(current => current.map(record => record.id === memory.id ? updated : record));
        restoreEditButton.current = true;
        setDraft(null);
        setNotice("已保存");
      } else if (action === "review-delete") {
        const linked = memory.sourceRef
          ? (await memoryConsole<{ memories: UserMemoryRecord[] }>(`/impact/${encodeURIComponent(memory.id)}`)).memories
          : [memory];
        setImpact(linked);
      } else {
        await deleteMemory(memory.id, Boolean(memory.sourceRef));
        setMemories(current => current.filter(record =>
          record.id !== memory.id && !(memory.sourceRef && memory.sourceRef === record.sourceRef)));
        selectRecord(null);
        setNotice("已删除记忆");
      }
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "记忆操作失败");
    } finally {
      setBusy(false);
    }
  }

  if (group && memory) {
    const text = readableMemory(memory);
    return <div className="nf-memory-detail">
      <button ref={backRef} type="button" className="nf-setting-back" disabled={busy} onClick={() => { selectRecord(null); setNotice(""); }}>
        <ChevronLeft size={17} aria-hidden="true" /> 返回记忆列表
      </button>
      <h3>记忆详情</h3>
      {group.records.length > 1 && <label className="nf-memory-source">
        查看同内容的记录
        <select className="nf-input" value={memory.id} disabled={busy || impact !== null || draft !== null}
          onChange={event => selectRecord(event.target.value)}>
          {group.records.map((record, index) =>
            <option value={record.id} key={record.id}>记录 {index + 1}</option>)}
        </select>
      </label>}
      {draft !== null ? <label className="nf-memory-source">
        记忆内容
        <textarea ref={editRef} className="nf-input" value={draft} onChange={event => setDraft(event.target.value)} disabled={busy} />
      </label> : <div className="nf-memory-paper">
        <p>{text}</p>
        {text !== memory.content.trim() && <details>
          <summary>查看原始记录</summary>
          <p>{memory.content}</p>
        </details>}
      </div>}
      {error && <p role="alert" className="nf-notice nf-error">{error}</p>}
      {notice && <p role="status" className="nf-notice">{notice}</p>}
      {impact !== null ? <div className="nf-memory-confirm">
        <h4>{memory.sourceRef ? `删除同一来源的 ${impact.length} 条记忆？` : "删除这条记忆？"}</h4>
        <p>原聊天和笔记会保留。</p>
        {memory.sourceRef && <ul>{impact.map(record => <li key={record.id}>{readableMemory(record)}</li>)}</ul>}
        <div className="nf-setting-actions">
          <button ref={cancelDeleteRef} type="button" className="nf-button" disabled={busy}
            onClick={() => { restoreEditButton.current = true; setImpact(null); }}>取消</button>
          <button type="button" className="nf-button nf-danger" disabled={busy} onClick={() => void changeMemory("delete")}>确认删除</button>
        </div>
      </div> : <div className="nf-setting-actions">
        {draft !== null ? <>
          <button type="button" className="nf-button" disabled={busy}
            onClick={() => { restoreEditButton.current = true; setDraft(null); }}>取消编辑</button>
          <button type="button" className="nf-button nf-primary" disabled={busy || !draft.trim()} onClick={() => void changeMemory("save")}>
            {memory.status === "pending" ? "保存并启用" : "保存记忆"}
          </button>
        </> : <>
          <button type="button" className="nf-button nf-danger" disabled={busy} onClick={() => void changeMemory("review-delete")}>删除记忆</button>
          <button ref={editButtonRef} type="button" className="nf-button" disabled={busy} onClick={() => setDraft(text)}>编辑记忆</button>
        </>}
      </div>}
    </div>;
  }

  return <div ref={listRef} tabIndex={-1} className="nf-memory-settings">
    <div className="nf-setting-row nf-memory-control">
      <div><strong>长期记忆</strong><p>{enabled ? "AI 可以在之后的聊天中使用已保存的记忆。" : "已暂停在聊天中使用记忆，已保存的内容仍可管理。"}</p></div>
      <button type="button" role="switch" className="nf-switch" aria-label="长期记忆" aria-checked={enabled}
        disabled={savingSetting} onClick={() => void toggleMemory()}><span /></button>
    </div>
    {settingError && <p className="nf-notice nf-error" role="alert">{settingError}</p>}
    <div className="nf-memory-list-heading">
      <h3>已记录的内容</h3>
      {(groups.length > 5 || query) && <label className="nf-memory-search">
        <Search size={16} aria-hidden="true" />
        <input type="search" aria-label="搜索记忆" placeholder="搜索记忆" value={query} onChange={event => setQuery(event.target.value)} />
      </label>}
    </div>
    {notice && <p role="status" className="nf-notice">{notice}</p>}
    {listError && <div className="nf-setting-row"><p role="alert">{listError}</p>
      <button className="nf-button" type="button" disabled={loading} onClick={() => void refresh()}>重新加载</button>
    </div>}
    {loading ? <p className="nf-setting-empty" role="status">正在加载记忆…</p>
      : !listError && !visible.length && <p className="nf-setting-empty">{filter ? "找不到匹配的记忆" : "还没有保存的记忆"}</p>}
    {!loading && Object.entries(sectionLabels).map(([section, label]) => {
      const items = visible.filter(item => item.section === section);
      if (!items.length) return null;
      return <div key={section} className="nf-memory-group" data-group={section}>
        <h4>{section === "active" && !enabled ? "已保存" : label}</h4>
        {section === "review" && <p>以下称呼记录互相矛盾。打开一条即可核对原文和处理。</p>}
        <ul>{items.map(item => {
          const id = item.records[0].id;
          return <li key={id}><button type="button" data-memory-id={id}
            onClick={() => { returnTo.current = id; selectRecord(id); setNotice(""); }}>
            <span>{item.title}</span>
            {item.records.length > 1 && <small>{item.records.length} 条记录</small>}
            <ChevronRight size={16} aria-hidden="true" />
          </button></li>;
        })}</ul>
      </div>;
    })}
  </div>;
}

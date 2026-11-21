import { useEffect, useMemo, useRef, useState } from "react";
import { memoryConsole } from "../services/memoryConsole";
import { deleteMemory, listMemories, updateMemory, type UserMemoryRecord } from "../services/memories";

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

const topicNames: Record<string, string> = {
  identity: "关于你", personal_info: "关于你", writing_style: "写作习惯", workflow: "做事习惯",
  preference: "个人偏好", constraint: "个人偏好", interest: "兴趣", goal: "目标",
  project: "项目", skill: "学习与技能", episode: "近期经历",
};

function summaryText(record: UserMemoryRecord) {
  const content = readableMemory(record);
  const text = content.replace(/^用户(?=[\u4e00-\u9fff])/, "你");
  return /[。！？.!?…]$/.test(text) ? text : text + "。";
}

// A source is the smallest independently deletable unit in Hindsight.
function buildSections(memories: UserMemoryRecord[]) {
  const paragraphs = new Map<string, UserMemoryRecord[]>();
  const claims = memories.filter(m => m.status === "active" && (m.scope || "global") === "global")
    .map(m => nameClaim(readableMemory(m))).filter(claim => claim !== null);
  const names = new Set(claims.filter(c => !c.rejected).map(c => c.name));
  const conflicting = names.size > 1 || claims.some(c => c.rejected && names.has(c.name));
  for (const memory of memories) {
    const key = memory.sourceRef ? "source:" + memory.sourceRef : "record:" + memory.id;
    const paragraph = paragraphs.get(key);
    if (paragraph) paragraph.push(memory);
    else paragraphs.set(key, [memory]);
  }
  const sections = new Map<string, UserMemoryRecord[][]>();
  for (const records of paragraphs.values()) {
    const first = records[0];
    const hasConflict = conflicting && records.some(m => m.status === "active" && (m.scope || "global") === "global" && nameClaim(readableMemory(m)));
    const title = hasConflict ? "待核对的称呼" : records.some(m => m.status !== first.status) ? "不同状态的记忆"
      : first.status === "active" ? topicNames[first.memoryType] || "其他记忆"
      : first.status === "pending" ? "待确认" : first.status === "archived" ? "已停用" : "其他记录";
    const section = sections.get(title);
    if (section) section.push(records);
    else sections.set(title, [records]);
  }
  return sections;
}

export default function MemorySettings() {
  const [records, setRecords] = useState<UserMemoryRecord[]>([]);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState("");
  const [reload, setReload] = useState(0);
  const [manage, setManage] = useState(false);
  const [selected, setSelected] = useState<UserMemoryRecord[]>([]);
  const [index, setIndex] = useState(0);
  const [draft, setDraft] = useState("");
  const [impact, setImpact] = useState<UserMemoryRecord[] | null>(null);
  const [busy, setBusy] = useState(false);
  const [notice, setNotice] = useState("");
  const editRef = useRef<HTMLTextAreaElement>(null);
  const manageRef = useRef<HTMLButtonElement>(null);
  const cancelRef = useRef<HTMLButtonElement>(null);
  const record = selected[index];
  useEffect(() => {
    let active = true;
    setLoading(true);
    setError("");
    listMemories(false, { allRecords: true }).then(result => { if (active) setRecords(result.filter(m => m.status !== "deleted")); })
      .catch(e => { if (active) setError(e instanceof Error ? e.message : "记忆加载失败"); })
      .finally(() => { if (active) setLoading(false); });
    return () => { active = false; };
  }, [reload]);
  useEffect(() => { if (record) editRef.current?.focus(); }, [record]);
  useEffect(() => { if (impact) cancelRef.current?.focus(); }, [impact]);
  const sections = useMemo(() => buildSections(records), [records]);

  function choose(paragraph: UserMemoryRecord[], position = 0) {
    setSelected(paragraph);
    setIndex(position);
    setDraft(readableMemory(paragraph[position]));
    setImpact(null);
    setError("");
    setNotice("");
  }
  function finish() {
    setSelected([]);
    setIndex(0);
    setImpact(null);
    setManage(false);
    requestAnimationFrame(() => manageRef.current?.focus());
  }
  async function change(action: "save" | "inspect" | "delete") {
    if (!record || busy) return;
    setBusy(true);
    setError("");
    try {
      if (action === "inspect") {
        const related = record.sourceRef
          ? (await memoryConsole<{memories: UserMemoryRecord[]}>(`/impact/${encodeURIComponent(record.id)}`)).memories
          : [record];
        setImpact(related);
        return;
      }
      if (action === "save") {
        const updated = await updateMemory(record.id, {
          content: draft.trim(), ...(record.status === "pending" ? { status: "active" } : {}), reason: "profile_memory_edit",
        });
        setRecords(previous => previous.map(m => m.id === record.id ? updated : m));
        setNotice("已保存");
      } else {
        if (!impact) return;
        await deleteMemory(record.id, Boolean(record.sourceRef));
        setRecords(previous => previous.filter(m => m.id !== record.id && !(record.sourceRef && m.sourceRef === record.sourceRef)));
        setNotice("已删除这段记忆");
      }
      finish();
    } catch (failure) {
      setError(failure instanceof Error ? failure.message : "记忆操作失败");
    } finally {
      setBusy(false);
    }
  }
  return <div className="nf-summary">
    {loading && <p role="status">正在加载记忆…</p>}
    {error && <p className="nf-error" role="alert">{error}</p>}
    {notice && <p className="nf-notice" role="status">{notice}</p>}
    {!loading && !records.length && (error
      ? <button type="button" className="nf-button" onClick={() => setReload(n => n + 1)}>重新加载</button>
      : <p>还没有保存的记忆</p>)}
    {record ? <>
      {impact ? <>
        <h3>删除这段记忆？</h3>
        <p>将删除{record.sourceRef ? "同一来源的 " : " "}{impact.length} 条记忆，原聊天和笔记会保留。</p>
        {impact.map(m => <p key={m.id}>{readableMemory(m)}</p>)}
        <div className="nf-record-actions">
          <button type="button" ref={cancelRef} className="nf-button" disabled={busy} onClick={() => {setImpact(null);requestAnimationFrame(() => editRef.current?.focus());}}>取消</button>
          <button type="button" className="nf-button nf-danger" disabled={busy} onClick={() => void change("delete")}>确认删除</button>
        </div>
      </> : <>
        {selected.length > 1 && <label className="nf-summary-field">段落中的记忆
          <select className="nf-input" value={index} disabled={busy || draft !== readableMemory(record)} onChange={e => choose(selected, Number(e.target.value))}>
            {selected.map((m, i) => <option key={m.id} value={i}>{readableMemory(m)}</option>)}
          </select>
        </label>}
        <label className="nf-summary-field">记忆内容<textarea ref={editRef} className="nf-input" value={draft} maxLength={2000} disabled={busy} onChange={e => setDraft(e.target.value)} /></label>
        <div className="nf-record-actions">
          <button type="button" className="nf-button nf-danger" disabled={busy} onClick={() => void change("inspect")}>删除这段</button>
          <button type="button" className="nf-button" disabled={busy} onClick={finish}>取消编辑</button>
          <button type="button" className="nf-button nf-primary" disabled={busy || !draft.trim()} onClick={() => void change("save")}>{record.status === "pending" ? "保存并启用" : "保存"}</button>
        </div>
      </>}
    </> : !loading && records.length > 0 && <>
      <div className="nf-summary-toolbar">
        <span>共 {records.length} 条记忆</span>
        <button ref={manageRef} className="nf-summary-edit" type="button" aria-pressed={manage}
          onClick={() => {setManage(!manage);setNotice("");}}>{manage ? "取消选择" : "编辑"}</button>
      </div>
      {manage && <p className="nf-notice">选择要编辑或删除的段落</p>}
      <article aria-label="记忆摘要">{Array.from(sections, ([title, paragraphs]) =>
        <section key={title}><h3>{title}</h3>
          {manage ? paragraphs.map(paragraph => <button type="button" key={paragraph[0].id} className="nf-paragraph" onClick={() => choose(paragraph)}>
            {paragraph.map(summaryText).join("")}
          </button>) : <p>{Array.from(new Set(paragraphs.flat().map(summaryText))).join("")}</p>}
        </section>
      )}</article>
    </>}
  </div>;
}

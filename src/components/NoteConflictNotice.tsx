import { useEffect, useId, useRef, useState } from "react";
import type { NoteSaveConflict } from "../types";
import { diffMarkdownLines } from "../utils/markdownDiff";
import "./note-conflict.css";

function ConflictComparison({ conflict, onClose }: { conflict: NoteSaveConflict; onClose: () => void }) {
  const dialog = useRef<HTMLDialogElement>(null);
  const title = useId();
  const { rows, simplified } = diffMarkdownLines(conflict.remoteContent, conflict.localContent);
  const changes = rows.filter(row => row.kind !== "equal");
  useEffect(() => { dialog.current?.showModal(); }, []);
  return (
    <dialog ref={dialog} className="panel-surface nf-note-conflict-dialog" aria-labelledby={title} onClose={onClose}>
      <header>
        <h2 id={title}>两版笔记差异</h2>
        <button type="button" className="ui-button ui-button-secondary" onClick={() => dialog.current?.close()}>关闭</button>
      </header>
      <div className="nf-note-conflict-legend">
        <span data-kind="remove">− 另一处保存的版本</span>
        <span data-kind="add">+ 我的副本</span>
      </div>
      {simplified && <p>笔记较长，按变化的区域显示。</p>}
      {changes.length === 0 ? <p>两版没有可显示的文字差异。</p> : (
        <div className="nf-note-conflict-diff" tabIndex={0} aria-label="有差异的正文">
          {changes.map((row, index) => (
            <div key={index} data-kind={row.kind}>
              <span className="nf-note-conflict-line" aria-label={row.kind === "add" ? "我的副本" : "另一处保存的版本"}>
                {row.kind === "add" ? "+" : "−"} {row.newLine ?? row.oldLine}
              </span>
              <span>{row.text || "\u00a0"}</span>
            </div>
          ))}
        </div>
      )}
    </dialog>
  );
}

export default function NoteConflictNotice({ conflict, onOpenCopy }: { conflict: NoteSaveConflict; onOpenCopy: () => void }) {
  const [comparing, setComparing] = useState(false);
  const compareButton = useRef<HTMLButtonElement>(null);
  return (
    <div className="nf-note-feedback nf-operation-feedback nf-note-conflict">
      <div className="nf-operation-message" role="status">
        <strong>这篇笔记已在另一处更新</strong>
        <span>你的修改已保存为副本。</span>
      </div>
      <div className="nf-note-conflict-actions">
        <button type="button" ref={compareButton} onClick={() => setComparing(true)}>查看两版差异</button>
        <button type="button" onClick={onOpenCopy}>打开我的副本</button>
      </div>
      {comparing && <ConflictComparison conflict={conflict} onClose={() => {
        setComparing(false);
        compareButton.current?.focus();
      }} />}
    </div>
  );
}

import { lazy, Suspense, useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import {
  ArrowUp,
  FileText,
  Loader2,
  Sparkles,
  Undo2,
} from "lucide-react";
import { useChatSlice, useEditorSlice } from "../store/selectors";
import {
  listEditPreviewRevisions,
  type NoteEditPreviewRevisionRecord,
  type NoteEditPreviewRecord,
} from "../services/edits";
import { diffMarkdownLines } from "../utils/markdownDiff";
import { compareMarkdownBlocks, markdownReferenceContext } from "../utils/markdownReview";
import { shouldSubmitChatInput } from "../utils/inputComposition";
import { findFileById } from "../workspaceTree";
import PreviewMarkdown from "./PreviewMarkdown";
import "./document-prose.css";
import "./edit-preview.css";

const EditablePreviewBody = lazy(() => import("./EditablePreviewBody"));

function findCurrentRevision(revisions: NoteEditPreviewRevisionRecord[], preview: NoteEditPreviewRecord) {
  if (preview.currentRevisionId) return revisions.findIndex(revision => revision.id === preview.currentRevisionId);
  const matches = revisions.filter(revision => revision.newContent === preview.newContent &&
    revision.instruction === preview.instruction &&
    revision.changeSummary.length === preview.changeSummary.length &&
    revision.changeSummary.every((item, index) => item === preview.changeSummary[index]));
  return matches.length === 1 ? revisions.indexOf(matches[0]) : -1;
}

function findPreviousRevision(revisions: NoteEditPreviewRevisionRecord[], currentIndex: number) {
  const current = revisions[currentIndex];
  if (!current || current.source === "generated") return -1;
  if (current.previousRevisionId) return revisions.findIndex(revision => revision.id === current.previousRevisionId);
  // Older history has no links. The stored AI instruction used to be extended
  // on each adjustment. Use that lineage only when it identifies a single base.
  const suffix = current.instruction.lastIndexOf("\n\n继续调整：");
  if (current.source === "revision" && suffix < 0) return -1;
  const baseInstruction = current.source === "revision" ? current.instruction.slice(0, suffix) : current.instruction;
  const matches = revisions.slice(0, currentIndex).filter(revision => revision.instruction === baseInstruction);
  return matches.length === 1 ? revisions.indexOf(matches[0]) : -1;
}

function findAiUndoTarget(revisions: NoteEditPreviewRevisionRecord[], currentIndex: number) {
  let index = currentIndex;
  while (index >= 0 && revisions[index].source !== "generated") {
    const previous = findPreviousRevision(revisions, index);
    if (previous < 0 || previous >= index) return null;
    if (revisions[index].source === "revision") return revisions[previous];
    if (revisions[index].source !== "manual") return null;
    index = previous;
  }
  return null;
}

export default function EditPreviewWorkspace() {
  const {
    activeEditPreview,
    editPreviewDraft,
    updateEditPreviewDraft,
    flushEditPreviewDraft,
    treeData,
    editPreviewError,
    applyEditPreviewRequest,
    cancelEditPreviewRequest,
    reviseEditPreviewRequest,
    restoreEditPreviewRevisionRequest,
    closeEditPreview,
  } = useEditorSlice();
  const { chatLoading } = useChatSlice();
  const [revisionText, setRevisionText] = useState("");
  const [failedInstruction, setFailedInstruction] = useState("");
  const [viewMode, setViewMode] = useState<"result" | "compare" | "diff">("result");
  const [revisions, setRevisions] = useState<NoteEditPreviewRevisionRecord[]>([]);
  const [revisionError, setRevisionError] = useState("");
  const [operation, setOperation] = useState<"apply" | "cancel" | "revise" | "restore" | null>(null);
  const [revisionLoading, setRevisionLoading] = useState(false);
  const [revisionOwnerId, setRevisionOwnerId] = useState("");
  const [revisionReload, setRevisionReload] = useState(0);
  const [historyOpen, setHistoryOpen] = useState(false);
  const [undoTargetId, setUndoTargetId] = useState<string | null>(null);
  const [showChanges, setShowChanges] = useState(false);
  const [actionError, setActionError] = useState("");
  const revisionInputRef = useRef<HTMLTextAreaElement>(null);
  const revisionComposing = useRef(false);
  const historyTriggerRef = useRef<HTMLButtonElement>(null);
  const historyAnchorRef = useRef<HTMLDivElement>(null);
  const historyCloseRef = useRef<HTMLButtonElement>(null);
  const diffTriggerRef = useRef<HTMLButtonElement>(null);
  const undoRef = useRef<HTMLButtonElement>(null);
  const keepEditingRef = useRef<HTMLButtonElement>(null);
  const workspaceRef = useRef<HTMLElement>(null);
  const scrollRef = useRef<HTMLDivElement>(null);
  const revisionLoadOwner = useRef("");
  const busy = chatLoading || operation !== null;
  const draftContent = editPreviewDraft?.content ?? activeEditPreview?.newContent ?? "";
  const manualDirty = !!editPreviewDraft && editPreviewDraft.content !== editPreviewDraft.savedContent;
  const oldContent = activeEditPreview?.oldContent ?? "";
  const targetType = activeEditPreview?.targetType;
  const splitTarget = targetType === "selection" || targetType === "section" || targetType === "note";
  const splitComparison = viewMode === "compare" && splitTarget;
  const changedBlocks = useMemo(
    () => splitComparison && showChanges ? compareMarkdownBlocks(oldContent, draftContent) : null,
    [splitComparison, showChanges, oldContent, draftContent]
  );
  const highlighting = splitComparison && showChanges;
  const originalReferences = useMemo(
    () => highlighting ? markdownReferenceContext(oldContent) : "",
    [highlighting, oldContent]
  );
  const diff = useMemo(
    () => viewMode === "diff" ? diffMarkdownLines(activeEditPreview?.oldContent ?? "", draftContent) : null,
    [viewMode, draftContent, activeEditPreview?.oldContent]
  );
  useEffect(() => {
    setViewMode("result"); setHistoryOpen(false); setUndoTargetId(null); setShowChanges(false); setActionError("");
    setFailedInstruction("");
  }, [activeEditPreview?.id]);

  useEffect(() => {
    if (!manualDirty || busy || editPreviewDraft?.status === "error" || !activeEditPreview) return;
    const id = activeEditPreview.id;
    const timer = window.setTimeout(() => { void flushEditPreviewDraft(id).catch(() => {}); }, 650);
    return () => window.clearTimeout(timer);
  }, [manualDirty, draftContent, busy, editPreviewDraft?.status, activeEditPreview?.id, flushEditPreviewDraft]);

  useEffect(() => {
    const id = activeEditPreview?.id;
    return () => { if (id) void flushEditPreviewDraft(id).catch(() => {}); };
  }, [activeEditPreview?.id, flushEditPreviewDraft]);

  useEffect(() => {
    if (!historyOpen && !undoTargetId) return;
    const dismiss = (event: Event) => {
      if (event.target instanceof Node && !historyAnchorRef.current?.contains(event.target)) {
        setHistoryOpen(false);
        setUndoTargetId(null);
      }
    };
    document.addEventListener("pointerdown", dismiss);
    document.addEventListener("focusin", dismiss);
    return () => {
      document.removeEventListener("pointerdown", dismiss);
      document.removeEventListener("focusin", dismiss);
    };
  }, [historyOpen, undoTargetId]);

  useLayoutEffect(() => {
    const input = revisionInputRef.current;
    if (!input) return;
    const resize = () => {
      input.style.height = "0px";
      const maximum = parseFloat(window.getComputedStyle(input).maxHeight) || 88;
      const height = input.scrollHeight;
      input.style.height = `${Math.min(height, maximum)}px`;
      input.style.overflowY = height > maximum ? "auto" : "hidden";
    };
    resize();
    let width = input.clientWidth;
    const observer = typeof ResizeObserver === "undefined" ? null : new ResizeObserver(() => {
      if (input.clientWidth === width) return;
      width = input.clientWidth;
      resize();
    });
    observer?.observe(input);
    return () => observer?.disconnect();
  }, [revisionText, activeEditPreview?.id]);

  useEffect(() => {
    if (!activeEditPreview?.id) {
      setRevisions([]);
      revisionLoadOwner.current = "";
      return;
    }
    let alive = true;
    if (revisionLoadOwner.current !== activeEditPreview.id) {
      setRevisions([]);
      setRevisionOwnerId("");
      revisionLoadOwner.current = activeEditPreview.id;
    }
    setRevisionLoading(true);
    setRevisionError("");
    void listEditPreviewRevisions(activeEditPreview.id)
      .then((items) => { if (alive) { setRevisions(items); setRevisionOwnerId(activeEditPreview.id); } })
      .catch(() => {
        if (alive) setRevisionError("暂时无法读取修订记录，当前预览已保留。");
      })
      .finally(() => { if (alive) setRevisionLoading(false); });
    return () => { alive = false; };
  }, [activeEditPreview?.id, activeEditPreview?.newContent, activeEditPreview?.currentRevisionId, revisionReload]);
  const revisionsReady = !revisionLoading && revisionOwnerId === activeEditPreview?.id;

  const ownedRevisions = revisionOwnerId === activeEditPreview?.id;
  const currentRevisionIndex = useMemo(
    () => activeEditPreview && ownedRevisions ? findCurrentRevision(revisions, activeEditPreview) : -1,
    [ownedRevisions, revisions, activeEditPreview]
  );
  const hasManualChanges = manualDirty || revisions[currentRevisionIndex]?.source === "manual";
  const undoRevision = useMemo(() => findAiUndoTarget(revisions, currentRevisionIndex), [revisions, currentRevisionIndex]);

  const restoreRevision = useCallback(async (revisionId: string, source: "history" | "undo") => {
    if (!activeEditPreview || busy || !ownedRevisions ||
        (source === "history" && !revisionsReady) || !revisions.some(revision => revision.id === revisionId)) return;
    const initiator = document.activeElement;
    const previewId = activeEditPreview.id;
    const stillCurrent = () => workspaceRef.current?.dataset.previewId === previewId;
    setOperation("restore");
    setActionError("");
    try {
      await flushEditPreviewDraft(previewId);
      if (!stillCurrent()) return;
      await restoreEditPreviewRevisionRequest(revisionId);
      if (stillCurrent()) {
        setHistoryOpen(false);
        setUndoTargetId(null);
      }
    } catch (error) {
      if (stillCurrent()) {
        setActionError(error instanceof Error ? error.message : "恢复失败，修改稿已保留。");
        setUndoTargetId(null);
      }
    } finally {
      setOperation(null);
      requestAnimationFrame(() => {
        if (stillCurrent() && (document.activeElement === document.body || document.activeElement === initiator)) {
          (source === "history" ? historyTriggerRef.current : undoRef.current ?? historyTriggerRef.current)?.focus({ preventScroll: true });
        }
      });
    }
  }, [activeEditPreview, busy, ownedRevisions, revisionsReady, revisions, flushEditPreviewDraft, restoreEditPreviewRevisionRequest]);

  const handleRevise = useCallback(async (input: string, clearInput: boolean) => {
    if (!input.trim() || !activeEditPreview || busy) return;
    const previewId = activeEditPreview.id;
    setFailedInstruction("");
    setActionError("");
    if (clearInput) setRevisionText("");
    setOperation("revise");
    try {
      const success = await reviseEditPreviewRequest(input.trim());
      if (!success && workspaceRef.current?.dataset.previewId === previewId) {
        if (!revisionComposing.current) setRevisionText(current => current === "" ? input : current);
        setFailedInstruction(input);
      }
    } finally {
      setOperation(null);
    }
  }, [activeEditPreview, busy, reviseEditPreviewRequest]);

  const handleKeyDown = useCallback(
    (event: React.KeyboardEvent<HTMLTextAreaElement>) => {
      if (!shouldSubmitChatInput({
        key: event.key,
        shiftKey: event.shiftKey,
        isComposing: event.nativeEvent.isComposing,
        keyCode: event.nativeEvent.keyCode,
      }, revisionComposing.current)) return;
      event.preventDefault();
      void handleRevise(revisionText, true);
    },
    [handleRevise, revisionText]
  );

  const closeHistory = () => {
    setHistoryOpen(false);
    historyTriggerRef.current?.focus({ preventScroll: true });
  };

  const closePopoverOnEscape = (event: React.KeyboardEvent<HTMLElement>) => {
    if ((!historyOpen && !undoTargetId) || event.key !== "Escape" || event.nativeEvent.isComposing || event.nativeEvent.keyCode === 229) return;
    event.preventDefault();
    event.stopPropagation();
    if (undoTargetId) {
      setUndoTargetId(null);
      undoRef.current?.focus({ preventScroll: true });
    } else closeHistory();
  };

  const handleUndo = () => {
    if (!undoRevision || busy) return;
    setHistoryOpen(false);
    if (hasManualChanges) {
      setUndoTargetId(undoRevision.id);
      requestAnimationFrame(() => keepEditingRef.current?.focus({ preventScroll: true }));
    } else void restoreRevision(undoRevision.id, "undo");
  };

  const changeView = (mode: typeof viewMode) => {
    if (mode === viewMode) return;
    setViewMode(mode);
    scrollRef.current?.scrollTo({ top: 0, behavior: "instant" });
  };

  const toggleDetailedDiff = () => {
    const comparisonMode = targetType === "delete" ? "result" : "compare";
    changeView(viewMode === "diff" ? comparisonMode : "diff");
    requestAnimationFrame(() => diffTriggerRef.current?.focus({ preventScroll: true }));
  };

  if (!activeEditPreview) {
    return (
      <main className="flex min-w-0 flex-1 flex-col border-x border-jelly-border bg-jelly-bg">
        <div className="flex h-full items-center justify-center px-8">
          <div className="max-w-sm text-center">
            <Sparkles
              size={24}
              strokeWidth={1.4}
              className="mx-auto mb-3 text-jelly-text-muted"
            />
            <h2 className="text-base font-semibold text-jelly-text">没有正在确认的修改</h2>
            <p className="mt-2 text-sm leading-relaxed text-jelly-text-muted">
              从右侧 AI 助手发出修改指令后，这里会显示修改稿。
            </p>
            <button
              type="button"
              onClick={closeEditPreview}
              className="mt-4 inline-flex h-9 items-center gap-2 rounded-md border border-jelly-border bg-white px-3 text-sm font-medium text-jelly-text-soft transition-colors hover:border-jelly-blue/25 hover:bg-jelly-blue-pale hover:text-jelly-blue-deep"
            >
              <FileText size={15} strokeWidth={1.8} />
              返回笔记
            </button>
          </div>
        </div>
      </main>
    );
  }

  const operationLabel = operation === "apply" ? "正在应用" : operation === "cancel" ? "正在放弃"
    : operation === "revise" ? "正在改写" : operation === "restore" ? "正在恢复" : "AI 正在处理";
  const feedback = busy ? operationLabel
    : editPreviewDraft?.status === "saving" ? "正在保存修改稿"
    : manualDirty && editPreviewDraft?.status !== "error" ? "修改稿待保存" : "";
  const failure = editPreviewError || editPreviewDraft?.error || actionError;

  return (
    <main ref={workspaceRef} className="edit-preview-workspace document-canvas nf-prose-refined flex min-w-0 flex-1 flex-col"
      data-preview-id={activeEditPreview.id} data-view-mode={viewMode}
      data-compare-layout={splitComparison ? "split" : undefined}
      aria-label="修改预览">
      <header className="nf-edit-header">
        <div className="nf-edit-identity">
          <h1>修改预览</h1>
          <span className="nf-edit-feedback" role="status">{feedback}</span>
        </div>
        <div className="nf-edit-actions">
          <button type="button" className="nf-edit-button nf-edit-discard" disabled={busy}
            onClick={() => { if (busy) return; setOperation("cancel"); void cancelEditPreviewRequest().finally(() => setOperation(null)); }}>放弃</button>
          <button type="button" className="nf-edit-button nf-edit-primary" disabled={busy}
            onClick={() => { if (busy) return; setOperation("apply"); void applyEditPreviewRequest().finally(() => setOperation(null)); }}>
            {operation === "apply" && <Loader2 size={14} className="animate-spin" aria-hidden="true" />}应用到笔记
          </button>
        </div>
      </header>

      <div ref={scrollRef} className="nf-edit-scroll">
        <div className="nf-edit-tools">
          {targetType !== "delete" ? <div className="nf-edit-view-switch" role="group" aria-label="查看方式">
            <button type="button" className="nf-edit-button" aria-pressed={viewMode === "result"} onClick={() => changeView("result")}>修改稿</button>
            <button type="button" className="nf-edit-button" aria-pressed={viewMode !== "result"} onClick={() => changeView("compare")}>原文对照</button>
          </div> : <button ref={diffTriggerRef} type="button" className="nf-edit-button" onClick={toggleDetailedDiff}>
            {viewMode === "diff" ? "返回删除预览" : "逐行差异"}
          </button>}
          <div ref={historyAnchorRef} className="nf-edit-history" onKeyDown={closePopoverOnEscape}>
            {targetType !== "delete" && undoRevision && <button ref={undoRef} type="button"
              className="nf-edit-button nf-edit-undo" aria-label="撤销上次 AI 修改" title="撤销上次 AI 修改"
              aria-expanded={undoTargetId !== null} aria-controls={undoTargetId ? "edit-preview-undo" : undefined}
              disabled={busy} onClick={handleUndo}><Undo2 size={16} aria-hidden="true" /></button>}
            {undoTargetId && <section id="edit-preview-undo" className="nf-edit-auxiliary nf-edit-confirm" aria-labelledby="edit-preview-undo-title">
              <h2 id="edit-preview-undo-title">撤销上次 AI 修改？</h2>
              <p>将回到 AI 改写前。你手动改过的这一版会保留在版本记录里。</p>
              <div className="nf-edit-confirm-actions">
                <button ref={keepEditingRef} type="button" className="nf-edit-button" onClick={() => {
                  setUndoTargetId(null);
                  undoRef.current?.focus({ preventScroll: true });
                }}>继续编辑</button>
                <button type="button" className="nf-edit-button nf-edit-primary" disabled={busy}
                  onClick={() => void restoreRevision(undoTargetId, "undo")}>撤销</button>
              </div>
            </section>}
            <button ref={historyTriggerRef} type="button" className="nf-edit-button nf-edit-history-trigger" aria-expanded={historyOpen}
              aria-controls={historyOpen ? "edit-preview-history" : undefined}
              onClick={() => {
                if (historyOpen) closeHistory();
                else {
                  setUndoTargetId(null);
                  setHistoryOpen(true);
                  requestAnimationFrame(() => historyCloseRef.current?.focus({ preventScroll: true }));
                }
              }}>版本记录</button>
            {historyOpen && <section id="edit-preview-history" className="nf-edit-auxiliary" aria-label="预览版本">
              <div className="nf-edit-history-heading"><h2>版本记录</h2><button ref={historyCloseRef} type="button" className="nf-edit-button" aria-label="关闭版本记录" onClick={closeHistory}>收起</button></div>
              {revisionError ? <div role="alert" className="nf-edit-history-error">{revisionError}<button type="button" className="nf-edit-button" disabled={busy} onClick={() => setRevisionReload(value => value + 1)}>重试读取</button></div> : revisionLoading || revisionOwnerId !== activeEditPreview.id ? <p role="status">正在读取版本…</p> : revisions.length === 0 ? <p>暂无可恢复的预览版本。</p> : <div className="nf-edit-revisions">
                {revisions.map((_, offset) => {
                  const index = revisions.length - 1 - offset;
                  const revision = revisions[index];
                  const current = index === currentRevisionIndex;
                  const name = index === 0 ? "初始稿" : revision.source === "manual" ? `手动修改 ${index}` : `第${["", "一", "二", "三", "四", "五", "六", "七", "八", "九"][index] || index}次调整`;
                  const date = revision.createdAt ? new Date(revision.createdAt) : null;
                  const time = date && !Number.isNaN(date.getTime()) ? date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" }) : "";
                  return <button type="button" className="nf-edit-revision" key={revision.id} aria-label={current ? name + "，当前预览" : "恢复" + name} aria-current={current ? "true" : undefined} disabled={current || busy || !revisionsReady} onClick={() => void restoreRevision(revision.id, "history")}>
                    <span className="nf-edit-revision-name">{name}{time && <span>{time}</span>}</span><span className="nf-edit-revision-action">{current ? "当前" : "恢复"}</span>
                  </button>;
                })}
              </div>}
            </section>}
          </div>
        </div>
        <div className="nf-edit-frame">
          {failure && <div role="alert" className="nf-edit-error">{failure}{manualDirty && <button type="button" className="nf-edit-button" disabled={busy} onClick={() => { setActionError(""); void flushEditPreviewDraft(activeEditPreview.id).catch(() => {}); }}>重试保存预览</button>}</div>}

          {viewMode === "compare" && !splitTarget && <div className="nf-edit-insert-diff">
            <button ref={diffTriggerRef} type="button" className="nf-edit-button" onClick={toggleDetailedDiff}>逐行差异</button>
          </div>}
          <section className="edit-preview-result" aria-label="修改后正文" hidden={viewMode === "diff"}>
            {activeEditPreview.targetType === "delete" ? (
              <div className="nf-edit-deletion">
                <span className="nf-edit-change-label">将删除</span>
                <PreviewMarkdown content={activeEditPreview.oldContent} />
              </div>
            ) : (
              <div className="nf-edit-review" role={splitComparison ? "region" : undefined}
                aria-label={splitComparison ? "原文对照" : undefined}>
                {splitComparison && <section className="nf-edit-pane nf-edit-pane-original" aria-label="原文">
                  <div className="nf-edit-pane-heading"><span className="nf-edit-pane-label">原文</span></div>
                  {changedBlocks ? changedBlocks.filter(block => block.oldContent.trim()).map(block => (
                    <div key={block.id} className="nf-edit-original-block" data-changed={block.kind !== "equal" || undefined}>
                      <PreviewMarkdown content={block.oldContent} context={originalReferences} />
                    </div>
                  )) : <PreviewMarkdown content={oldContent} />}
                </section>}
                <section key="revision" className="nf-edit-pane nf-edit-pane-revised"
                  aria-label={splitComparison ? "修改稿" : undefined}>
                  {splitComparison && <div className="nf-edit-pane-heading">
                    <span className="nf-edit-pane-label">修改稿</span>
                    <div className="nf-edit-compare-actions">
                      {showChanges && <button ref={diffTriggerRef} type="button" className="nf-edit-button" onClick={toggleDetailedDiff}>逐行差异</button>}
                      <button type="button" className="nf-edit-button" aria-pressed={showChanges} onClick={() => setShowChanges(current => !current)}>
                        {showChanges ? "隐藏改动" : "显示改动"}
                      </button>
                    </div>
                  </div>}
                  <Suspense fallback={<PreviewMarkdown content={draftContent} />}>
                    <EditablePreviewBody key={activeEditPreview.id} previewId={activeEditPreview.id}
                      content={draftContent} oldContent={activeEditPreview.oldContent}
                      showComparison={viewMode === "compare" && !splitComparison} highlights={changedBlocks} disabled={busy}
                      documentTitle={activeEditPreview.targetType === "note" ? findFileById(treeData, activeEditPreview.noteId)?.name.replace(/\.md$/i, "") : ""}
                      onChange={updateEditPreviewDraft}
                      onBlur={() => { void flushEditPreviewDraft(activeEditPreview.id).catch(() => {}); }} />
                  </Suspense>
                </section>
              </div>
            )}
          </section>
          {viewMode === "diff" && diff ? <section className="nf-edit-diff" aria-label="详细差异正文">
            <div className="nf-edit-diff-summary">
              <span>+{diff.added} 行</span><span>−{diff.removed} 行</span>{diff.simplified && <span>长文已使用精简对比</span>}
              {targetType !== "delete" && <button ref={diffTriggerRef} type="button" className="nf-edit-button" onClick={toggleDetailedDiff}>返回原文对照</button>}
            </div>
            <div className="nf-edit-diff-scroll" role="region" tabIndex={0} aria-label="左右滚动查看完整差异">
              <div className="nf-edit-diff-row nf-edit-diff-heading"><span>旧</span><span>新</span><span /><span>内容</span></div>
              {diff.rows.map((row, index) => <div key={row.kind + "-" + row.oldLine + "-" + row.newLine + "-" + index} className="nf-edit-diff-row" data-kind={row.kind}>
                <span>{row.oldLine ?? ""}</span><span>{row.newLine ?? ""}</span><span>{row.kind === "add" ? "+" : row.kind === "remove" ? "−" : ""}</span><span>{row.text || " "}</span>
              </div>)}
            </div>
          </section> : null}
        </div>
      </div>

      {activeEditPreview.targetType !== "delete" && <footer className="nf-edit-footer">
        <div className="nf-edit-composer">
          <textarea ref={revisionInputRef} rows={1} value={revisionText} onChange={event => setRevisionText(event.target.value)}
            onCompositionStart={() => { revisionComposing.current = true; }}
            onCompositionEnd={() => { revisionComposing.current = false; }}
            onBlur={() => { revisionComposing.current = false; }}
            onKeyDown={handleKeyDown} placeholder="继续修改…" aria-label="继续修改预览" />
          <button type="button" className="nf-edit-button nf-edit-adjust" aria-label="继续修改" title="继续修改"
            aria-busy={operation === "revise"} onClick={() => void handleRevise(revisionText, true)} disabled={!revisionText.trim() || busy}>
            {operation === "revise"
              ? <Loader2 size={16} className="animate-spin" aria-hidden="true" />
              : <ArrowUp size={16} aria-hidden="true" />}
          </button>
        </div>
        {failedInstruction && revisionText !== failedInstruction && <div className="nf-edit-retry-row">
          <button type="button" className="nf-edit-button" disabled={busy}
            onClick={() => void handleRevise(failedInstruction, false)}>重试上一条</button>
        </div>}
      </footer>}
    </main>
  );
}

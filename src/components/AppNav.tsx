import { useEffect, useId, useRef, useState } from "react";
import "./menu-ui.css";
import { memoryConsole } from '../services/memoryConsole';
import {
  Check,
  ChevronRight,
  ChevronDown,
  ChevronLeft,
  Download,
  FileText,
  RotateCcw,
  LogOut,
  Pencil,
  Settings,
  Trash,
  Trash2,
  UserRound,
  X,
} from "lucide-react";
import { syncKnowledgeBeforeBackup } from "../store";
import { exportKnowledgeBackup, importKnowledgeBackup } from "../services/knowledgeBackup";
import { backupAsTree, parseKnowledgeBackup } from "../utils/knowledgeBackup";
import type { KnowledgeBackup } from "../utils/knowledgeBackup";
import { useWorkspaceSlice } from "../store/selectors";
import {
  deleteMemory,
  isMemoryEnabled,
  loadMemorySettings,
  listMemories,
  setMemoryEnabled,
  updateMemory,
  type UserMemoryRecord,
} from "../services/memories";
import {
  createKnowledgeZipBlob,
  downloadBlob,
  makeKnowledgeBackupName,
  makeKnowledgeJsonName,
} from "../utils/exportKnowledgeZip";
import {
  confirmEmailChange,
  requestEmailChange,
  type AuthSession,
} from "../services/auth";
import OverlayDialog, { useSurfacePresence } from "./OverlayDialog";
import "./account-menu.css";

export type AppView = "knowledge";
type ThemeMode = "light" | "dark";
const settingsLabels = { home: "设置", email: "登录邮箱", memory: "记忆列表", import: "导入笔记", export: "导出", trash: "回收站" } as const;
type SettingsPage = keyof typeof settingsLabels;
type SettingsDetail = Exclude<SettingsPage, "home" | "trash">;

export interface AccountMenuProps {
  themeMode: ThemeMode;
  onThemeModeChange: (mode: ThemeMode) => void;
  userEmail: string;
  userEmailVerified?: boolean;
  userName?: string;
  onEmailChanged?: (session: AuthSession) => void;
  onSignOut: () => void | Promise<void>;
  compact?: boolean;
  active?: boolean;
  trashCount?: number;
}

export default function AccountMenu({
  themeMode,
  onThemeModeChange,
  userEmail,
  userEmailVerified = false,
  userName,
  onEmailChanged,
  onSignOut,
  compact = false,
  active = true,
  trashCount = 0,
}: AccountMenuProps) {
  const [accountOpen, setAccountOpen] = useState(false);
  useEffect(() => { if (!active) setAccountOpen(false); }, [active]);
  const [settingsOpen, setSettingsOpen] = useState(false);
  const [settingsPage, setSettingsPage] = useState<SettingsPage>("home");
  const settingsReturnRow = useRef<SettingsDetail | null>(null);
  const settingsBackRef = useRef<HTMLButtonElement>(null);
  const accountPresent = useSurfacePresence(accountOpen);
  const [memoryEnabledState, setMemoryEnabledState] = useState(isMemoryEnabled);
  const [memorySettingLoading, setMemorySettingLoading] = useState(false);
  const [memorySettingError, setMemorySettingError] = useState("");
  const [memories, setMemories] = useState<UserMemoryRecord[]>([]);
  const [memoryLoading, setMemoryLoading] = useState(false);
  const [memoryUpdating, setMemoryUpdating] = useState(false);
  const [memoryError, setMemoryError] = useState("");
  const memoryLoadSequence = useRef(0);
  const [emailOpen, setEmailOpen] = useState(false);
  const [emailDraft, setEmailDraft] = useState(userEmail);
  const [emailCode, setEmailCode] = useState("");
  const [emailCodeSent, setEmailCodeSent] = useState(false);
  const [emailLoading, setEmailLoading] = useState(false);
  const [emailError, setEmailError] = useState("");
  const [emailMessage, setEmailMessage] = useState("");
  const [editingMemoryId, setEditingMemoryId] = useState<string | null>(null);
  const [editingContent, setEditingContent] = useState("");
  const [importStatus, setImportStatus] = useState("");
  const [exportStatus, setExportStatus] = useState("");
  const [dataBusy, setDataBusy] = useState(false);
  const dataOwner = useRef(userEmail);
  const pendingImport = useRef<{backup: KnowledgeBackup; requestId:string} | null>(null);
  dataOwner.current = userEmail;
  useEffect(() => {
    pendingImport.current = null;
    setDataBusy(false);
    setImportStatus("");
    setExportStatus("");
  }, [userEmail]);
  const accountRef = useRef<HTMLDivElement>(null);
  const accountButtonRef = useRef<HTMLButtonElement>(null);
  const accountPanelRef = useRef<HTMLDivElement>(null);
  const accountPanelId = useId();
  const importInputRef = useRef<HTMLInputElement>(null);
  const emailLocalPart = userEmail.split("@")[0];
  const displayName = userName?.trim();
  const accountPrimary = displayName && !displayName.includes("@") &&
    !(displayName === emailLocalPart && /^\d+$/.test(emailLocalPart)) ? displayName : "我的账号";
  const avatarText = Array.from(accountPrimary)[0].toLocaleUpperCase();
  const avatar = accountPrimary !== "我的账号" && /\p{L}/u.test(avatarText) ? avatarText : <UserRound size={17} strokeWidth={1.6} aria-hidden="true" />;
  const settingsScrollRef = useRef<HTMLDivElement>(null);
  const { reloadWorkspace, deletedNotes, restoreDeletedNote } = useWorkspaceSlice();
  const isDark = themeMode === "dark";

  useEffect(() => {
    if (settingsScrollRef.current) settingsScrollRef.current.scrollTop = 0;
    const row = settingsReturnRow.current;
    if (!settingsOpen || !row) return;
    if (settingsPage === "home") {
      settingsScrollRef.current?.querySelector<HTMLButtonElement>(`[data-settings-detail="${row}"]`)?.focus();
      settingsReturnRow.current = null;
    } else {
      settingsBackRef.current?.focus({ preventScroll: true });
    }
  }, [settingsPage, settingsOpen]);

  useEffect(() => {
    if (!accountOpen) return;

    accountPanelRef.current?.querySelector<HTMLButtonElement>("button")?.focus({ preventScroll: true });

    const handleOutsideInteraction = (event: Event) => {
      // Browser activation can focus the page itself without navigating away from the disclosure.
      if (event.type === "focusin" && (event.target === document.body || event.target === document.documentElement)) return;
      if (!accountRef.current?.contains(event.target as Node)) {
        setAccountOpen(false);
      }
    };
    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === "Escape" && !event.isComposing) {
        event.preventDefault();
        setAccountOpen(false);
        accountButtonRef.current?.focus({ preventScroll: true });
      }
    };

    document.addEventListener("pointerdown", handleOutsideInteraction);
    document.addEventListener("focusin", handleOutsideInteraction);
    document.addEventListener("keydown", handleKeyDown);
    return () => {
      document.removeEventListener("pointerdown", handleOutsideInteraction);
      document.removeEventListener("focusin", handleOutsideInteraction);
      document.removeEventListener("keydown", handleKeyDown);
    };
  }, [accountOpen]);

  useEffect(() => {
    if (!emailOpen) setEmailDraft(userEmail);
  }, [emailOpen, userEmail]);

  useEffect(() => {
    let cancelled = false;
    setMemorySettingLoading(true);
    loadMemorySettings()
      .then((settings) => {
        if (!cancelled) setMemoryEnabledState(settings.memoryEnabled);
      })
      .catch(() => {
        if (!cancelled) setMemoryEnabledState(isMemoryEnabled());
      })
      .finally(() => {
        if (!cancelled) setMemorySettingLoading(false);
      });
    return () => {
      cancelled = true;
    };
  }, []);

  const handleExport = async (format: "zip" | "json") => {
    const owner=userEmail;
    setDataBusy(true);setExportStatus("正在导出…");
    try {
      await syncKnowledgeBeforeBackup();
      if(dataOwner.current!==owner)return;
      const backup=await exportKnowledgeBackup();
      if(dataOwner.current!==owner)return;
      if(format==="zip") {
        const {treeData,fileContents}=backupAsTree(backup);
        downloadBlob(createKnowledgeZipBlob(treeData,fileContents),makeKnowledgeBackupName());
      } else downloadBlob(new Blob([JSON.stringify(backup)],{type:"application/json"}),makeKnowledgeJsonName());
      setExportStatus("已导出");
    } catch(error) {if(dataOwner.current===owner)setExportStatus(error instanceof Error?error.message:"导出失败");}
    finally {if(dataOwner.current===owner)setDataBusy(false);}
  };

  const runImport = async () => {
    if(!pendingImport.current)return;
    const pending=pendingImport.current;
    const owner=userEmail;
    const result=await importKnowledgeBackup(pending.backup,pending.requestId);
    if(dataOwner.current!==owner||pendingImport.current!==pending)return;
    pendingImport.current=null;
    reloadWorkspace(result.selectedNoteId);
    setImportStatus(`已导入 ${result.notesCreated} 篇笔记`);
  };

  const handleImportKnowledge = async (files: FileList | null) => {
    if(!files?.length||dataBusy)return;
    const owner=userEmail;
    setDataBusy(true);setImportStatus("正在解析导入文件…");
    try {
      // One archive per transaction keeps retry identity and failures unambiguous.
      const backup=await parseKnowledgeBackup(files[0]);
      if(dataOwner.current!==owner)return;
      pendingImport.current={backup,requestId:crypto.randomUUID()};
      setImportStatus("正在保存导入数据…");
      await runImport();
    } catch(error) {if(dataOwner.current===owner)setImportStatus(error instanceof Error?error.message:"导入失败");}
    finally {if(dataOwner.current===owner){setDataBusy(false);if(importInputRef.current)importInputRef.current.value="";}}
  };

  const retryImport = async () => {
    const owner=userEmail;
    setDataBusy(true);setImportStatus("正在重试导入…");
    try {await runImport();}
    catch(error){if(dataOwner.current===owner)setImportStatus(error instanceof Error?error.message:"导入失败");}
    finally {if(dataOwner.current===owner)setDataBusy(false);}
  };

  const reloadMemories = async () => {
    if (memoryUpdating) return;
    const request = ++memoryLoadSequence.current;
    setMemoryLoading(true);
    setMemoryError("");
    try {
      const records = await listMemories(false);
      if (request === memoryLoadSequence.current) setMemories(records.filter((memory) => memory.status !== "deleted"));
    } catch (error) {
      if (request === memoryLoadSequence.current) setMemoryError(error instanceof Error ? error.message : "记忆加载失败");
    } finally {
      if (request === memoryLoadSequence.current) setMemoryLoading(false);
    }
  };

  useEffect(() => {
    if (settingsOpen && settingsPage === "memory") {
      void reloadMemories();
    }
  }, [settingsOpen, settingsPage]);

  const handleRequestEmailCode = async () => {
    setEmailError("");
    setEmailMessage("");
    setEmailLoading(true);
    try {
      const result = await requestEmailChange(emailDraft.trim());
      setEmailCodeSent(true);
      setEmailMessage(result.message);
      if (result.developmentCode) {
        setEmailCode(result.developmentCode);
        setEmailMessage("开发环境验证码已自动填入。");
      }
    } catch (error) {
      setEmailError(error instanceof Error ? error.message : "验证码发送失败");
    } finally {
      setEmailLoading(false);
    }
  };

  const handleConfirmEmail = async () => {
    setEmailError("");
    setEmailMessage("");
    setEmailLoading(true);
    try {
      const nextSession = await confirmEmailChange(emailDraft.trim(), emailCode.trim());
      onEmailChanged?.(nextSession);
      setEmailCodeSent(false);
      setEmailCode("");
      setEmailOpen(false);
    } catch (error) {
      setEmailError(error instanceof Error ? error.message : "邮箱验证失败");
    } finally {
      setEmailLoading(false);
    }
  };

  const handleMemoryEnabledChange = async () => {
    const next = !memoryEnabledState;
    setMemoryEnabledState(next);
    setMemorySettingError("");
    setMemorySettingLoading(true);
    try {
      const settings = await setMemoryEnabled(next);
      setMemoryEnabledState(settings.memoryEnabled);
    } catch (error) {
      setMemoryEnabledState(!next);
      setMemorySettingError(error instanceof Error ? error.message : "记忆设置保存失败");
    } finally {
      setMemorySettingLoading(false);
    }
  };

  const startEditMemory = (memory: UserMemoryRecord) => {
    setEditingMemoryId(memory.id);
    setEditingContent(memory.content);
  };

  const saveEditMemory = async (memory: UserMemoryRecord) => {
    const content = editingContent.trim();
    if (!content || memoryUpdating) return;
    setMemoryUpdating(true);
    setMemoryError("");
    try {
      const updated = await updateMemory(memory.id, {
        content,
        ...(memory.status === "pending" ? { status: "active" } : {}),
        reason: "profile_memory_edit",
      });
      setMemories((current) => current.map((item) => (item.id === memory.id ? updated : item)));
      setEditingMemoryId(null);
      setEditingContent("");
    } catch (error) {
      setMemoryError(error instanceof Error ? error.message : "记忆保存失败");
    } finally {
      setMemoryUpdating(false);
    }
  };

  const removeMemory = async (memory: UserMemoryRecord) => {
    if (memoryUpdating) return;
    setMemoryUpdating(true);
    setMemoryError("");
    try {
      if (Boolean(memory.sourceRef)) {
        const impact = await memoryConsole<{ memories: UserMemoryRecord[] }>('/impact/' + encodeURIComponent(memory.id));
        if (!window.confirm(`删除此来源及关联的 ${impact.memories.length} 条记忆？\n\n${impact.memories.map((m: UserMemoryRecord) => m.content).join('\n')}\n\n原聊天或笔记不会因此删除。`)) return;
      }
      await deleteMemory(memory.id, Boolean(memory.sourceRef));
      const removed = (item: UserMemoryRecord) =>
        item.id === memory.id || Boolean(memory.sourceRef && item.sourceRef === memory.sourceRef);
      setMemories((current) => current.filter((item) => !removed(item)));
      if (editingMemoryId === memory.id || memories.some((item) => item.id === editingMemoryId && removed(item))) {
        setEditingMemoryId(null);
        setEditingContent("");
      }
    } catch (error) {
      setMemoryError(error instanceof Error ? error.message : "记忆删除失败");
    } finally {
      setMemoryUpdating(false);
    }
  };

  const openSettings = (page: "home" | "trash") => {
    // Capture a stable return target before the menu disappears.
    accountButtonRef.current?.focus({ preventScroll: true });
    setAccountOpen(false);
    settingsReturnRow.current = null;
    setSettingsPage(page);
    setSettingsOpen(true);
  };
  const openSettingsDetail = (page: SettingsDetail) => {
    settingsReturnRow.current = page;
    setSettingsPage(page);
  };
  const onSettingsDetail = settingsPage !== "home" && settingsPage !== "trash";
  const settingsLabel = settingsLabels[settingsPage];

  return (
    <div ref={accountRef} data-compact={compact} data-theme={themeMode} className="nf-settings-system account-menu-root relative shrink-0 overflow-visible w-full">
      <button ref={accountButtonRef} type="button" className="nf-account-trigger"
        onClick={() => setAccountOpen((open) => !open)}
        aria-label={accountPrimary + "的账号菜单"} aria-expanded={accountOpen}
        aria-controls={accountPresent ? accountPanelId : undefined} aria-haspopup="dialog">
        <span className="nf-avatar" aria-hidden="true">{avatar}</span>
        {!compact && <span className="nf-username">{accountPrimary}</span>}
      </button>

      {accountPresent && (
        <div ref={accountPanelRef} id={accountPanelId} role="dialog" aria-label="账号操作"
          className="nf-account-popover" data-open={accountOpen} inert={!accountOpen} aria-hidden={!accountOpen || undefined}>
          <div className="nf-account-heading"><span className="nf-avatar" aria-hidden="true">{avatar}</span><span className="nf-username">{accountPrimary}</span></div>
          <div className="nf-account-separator" aria-hidden="true" />
          <button type="button" className="nf-menu-item" onClick={() => openSettings("home")}><Settings size={17} strokeWidth={1.6} />设置</button>
          <button type="button" className="nf-menu-item" onClick={() => openSettings("trash")}><Trash size={17} strokeWidth={1.6} />回收站{trashCount > 0 && <span className="nf-row-end ml-auto">{trashCount}</span>}</button>
          <div className="nf-account-separator" aria-hidden="true" />
          <button type="button" className="nf-menu-item nf-account-signout" onClick={() => { setAccountOpen(false); void onSignOut(); }}><LogOut size={17} strokeWidth={1.6} />退出登录</button>
        </div>
      )}

      <OverlayDialog open={settingsOpen} onClose={() => setSettingsOpen(false)}
        onEscape={() => onSettingsDetail ? setSettingsPage("home") : setSettingsOpen(false)}
        label={settingsLabel} themeMode={themeMode} className="nf-settings-dialog">
        <div className="nf-settings-content">
          <header className="nf-content-heading" data-detail={onSettingsDetail}>
            {onSettingsDetail && <button ref={settingsBackRef} type="button" className="nf-icon-button" aria-label="返回设置" onClick={() => setSettingsPage("home")}><ChevronLeft size={17} strokeWidth={1.6} /></button>}
            <h2>{settingsLabel}</h2>
            {settingsPage === "memory" && <button type="button" className="nf-settings-refresh" disabled={memoryLoading || memoryUpdating} onClick={() => void reloadMemories()}>刷新</button>}
            <button type="button" className="nf-icon-button nf-settings-close" data-dialog-initial-focus aria-label={settingsPage === "trash" ? "关闭回收站" : "关闭设置"} onClick={() => setSettingsOpen(false)}><X size={17} strokeWidth={1.6} /></button>
          </header>
          <div ref={settingsScrollRef} className="nf-settings-scroll" data-page={settingsPage}>
            <section key={settingsPage} className="nf-settings-page" aria-label={settingsLabel + "内容"}>
              {settingsPage === "home" && <>
                <div className="nf-settings-group">
                  <button type="button" className="nf-row" data-settings-detail="email" onClick={() => openSettingsDetail("email")}>
                    <span>登录邮箱</span><span className="nf-row-end">{userEmailVerified ? "已验证" : "待验证"}<ChevronRight size={15} strokeWidth={1.6} aria-hidden="true" /></span>
                  </button>
                  <div className="nf-row">
                    <span>显示模式</span>
                    <div className="nf-segment" role="group" aria-label="显示模式"><button type="button" aria-pressed={!isDark} onClick={() => onThemeModeChange("light")}>浅色</button><button type="button" aria-pressed={isDark} onClick={() => onThemeModeChange("dark")}>深色</button></div>
                  </div>
                </div>
                <div className="nf-settings-group">
                  <div className="nf-row"><span>长期记忆</span><button type="button" role="switch" className="nf-switch" onClick={() => void handleMemoryEnabledChange()} disabled={memorySettingLoading} aria-checked={memoryEnabledState} aria-label="长期记忆"><span /></button></div>
                  <button type="button" className="nf-row" data-settings-detail="memory" onClick={() => openSettingsDetail("memory")}><span>记忆列表</span><span className="nf-row-end"><ChevronRight size={15} strokeWidth={1.6} aria-hidden="true" /></span></button>
                </div>
                <div className="nf-settings-group">
                  <button type="button" className="nf-row" data-settings-detail="import" onClick={() => openSettingsDetail("import")}><span>导入笔记</span><span className="nf-row-end"><ChevronRight size={15} strokeWidth={1.6} aria-hidden="true" /></span></button>
                  <button type="button" className="nf-row" data-settings-detail="export" onClick={() => openSettingsDetail("export")}><span>导出</span><span className="nf-row-end"><ChevronRight size={15} strokeWidth={1.6} aria-hidden="true" /></span></button>
                </div>
                {memorySettingError && <p className="nf-notice nf-error" role="alert">{memorySettingError}</p>}
              </>}

              {settingsPage === "email" && <div className="nf-settings-detail">
                <div className="nf-settings-current-email"><span>当前邮箱</span><span className="nf-settings-muted">{userEmail}</span></div>
                <button type="button" className="nf-row" aria-expanded={emailOpen} onClick={() => { setEmailOpen((open) => !open); setEmailError(""); setEmailMessage(""); }}>
                  <span>更换邮箱</span><span className="nf-row-end"><ChevronDown size={15} strokeWidth={1.6} aria-hidden="true" /></span>
                </button>
                {emailOpen && <div className="nf-detail">
                  <label htmlFor={accountPanelId + "-email"}>登录邮箱地址</label>
                  <input id={accountPanelId + "-email"} type="email" aria-label="登录邮箱地址" value={emailDraft} onChange={(event) => { setEmailDraft(event.target.value); setEmailCodeSent(false); setEmailCode(""); }} className="nf-input" placeholder="name@example.com" />
                  {emailCodeSent && <><label htmlFor={accountPanelId + "-code"}>邮箱验证码</label><input id={accountPanelId + "-code"} aria-label="邮箱验证码" value={emailCode} onChange={(event) => setEmailCode(event.target.value.replace(/\D/g, "").slice(0, 6))} inputMode="numeric" autoComplete="one-time-code" maxLength={6} className="nf-input" placeholder="输入 6 位验证码" /></>}
                  {emailError && <p className="nf-notice nf-error" role="alert">{emailError}</p>}
                  {emailMessage && <p className="nf-notice" role="status">{emailMessage}</p>}
                  <button type="button" className="nf-button nf-primary" disabled={emailLoading || !emailDraft.trim() || Boolean(emailCodeSent && emailCode.length !== 6)} onClick={() => void (emailCodeSent ? handleConfirmEmail() : handleRequestEmailCode())}>{emailLoading ? "处理中…" : emailCodeSent ? "确认并绑定" : "发送验证码"}</button>
                </div>}
              </div>}

              {settingsPage === "import" && <div className="nf-settings-detail">
                <p className="nf-notice">支持 Markdown、ZIP 和知识库备份。</p>
                <button type="button" className="nf-button" disabled={dataBusy || !!pendingImport.current} onClick={() => importInputRef.current?.click()}>选择文件</button>
                <input ref={importInputRef} type="file" accept=".md,.markdown,.zip,.json,text/markdown,application/zip,application/json" hidden onChange={(event) => void handleImportKnowledge(event.target.files)} />
                {importStatus && <p className="nf-notice" role="status">{importStatus}</p>}
                {pendingImport.current && !dataBusy && <div className="flex gap-2"><button type="button" className="nf-button" onClick={() => void retryImport()}>重试导入</button><button type="button" className="nf-button" onClick={() => {pendingImport.current=null;setImportStatus("已停止重试，请刷新确认服务端结果");}}>停止重试</button></div>}
              </div>}

              {settingsPage === "export" && <>
                <button type="button" className="nf-row nf-settings-format" aria-label="导出 Markdown 文件" aria-describedby={accountPanelId + "-export-markdown"} disabled={dataBusy} onClick={() => void handleExport("zip")}>
                  <span>Markdown<small id={accountPanelId + "-export-markdown"}>导出笔记正文，便于在其他软件里阅读</small></span><Download size={16} strokeWidth={1.6} aria-hidden="true" />
                </button>
                <button type="button" className="nf-row nf-settings-format" aria-label="导出知识库备份" aria-describedby={accountPanelId + "-export-backup"} disabled={dataBusy} onClick={() => void handleExport("json")}>
                  <span>知识库备份<small id={accountPanelId + "-export-backup"}>用于恢复目录、笔记、历史版本和附件。不含聊天与长期记忆。</small></span><Download size={16} strokeWidth={1.6} aria-hidden="true" />
                </button>
                {exportStatus && <p className="nf-notice" role="status">{exportStatus}</p>}
              </>}

              {settingsPage === "trash" && <div className="nf-settings-detail">
                {deletedNotes.length === 0 ? <div className="nf-empty"><Trash size={24} strokeWidth={1.6} /><span>回收站是空的</span></div> : <>
                  <p className="nf-notice">{deletedNotes.length} 篇已删除笔记</p>
                  {deletedNotes.map((note) => <div key={note.id} className="nf-record nf-trash-record">
                    <FileText size={17} strokeWidth={1.6} /><div className="nf-record-main"><p className="nf-record-title">{note.title}</p><p className="nf-record-meta">已移至回收站</p></div>
                    <button type="button" className="nf-button" aria-label={"恢复 " + note.title} onClick={() => restoreDeletedNote(note.id)}><RotateCcw size={14} strokeWidth={1.6} className="inline mr-1" />恢复</button>
                  </div>)}
                </>}
              </div>}

              {settingsPage === "memory" && <div className="nf-settings-detail">
                {memoryError && <p className="nf-notice nf-error" role="alert">{memoryError}</p>}
                  {memoryLoading ? <p className="nf-notice" role="status">正在加载记忆…</p> : memories.length === 0 ? <p className="nf-notice">暂无长期记忆</p> : memories.map((memory) => {
                    const isEditing = editingMemoryId === memory.id;
                    const pending = memory.status === "pending";
                    const status = pending ? "待确认" : memory.status === "archived" ? "已停用" : null;
                    return <div key={memory.id} className="nf-record nf-memory-record" data-editing={isEditing}>
                      <div className="nf-record-main">
                        {status && <p className="nf-record-meta">{status}</p>}
                        {isEditing ? <textarea value={editingContent} aria-label="记忆内容" disabled={memoryUpdating} onChange={(event) => setEditingContent(event.target.value)} className="nf-input" /> : <p className="nf-record-title">{memory.content}</p>}
                      </div>
                      <div className="nf-record-actions">{isEditing ? <>
                        <button type="button" className={pending ? "nf-button" : "nf-icon-button"} disabled={memoryUpdating || !editingContent.trim()}
                          onClick={() => void saveEditMemory(memory)} aria-label={pending ? "保存并启用" : "保存记忆"}>
                          {pending ? "保存并启用" : <Check size={16} strokeWidth={1.6} />}
                        </button>
                        <button type="button" className="nf-icon-button" disabled={memoryUpdating} onClick={() => { setEditingMemoryId(null); setEditingContent(""); }} aria-label="取消编辑"><X size={16} strokeWidth={1.6} /></button>
                      </> : <>
                        <button type="button" className="nf-icon-button" disabled={memoryUpdating} onClick={() => startEditMemory(memory)} aria-label="编辑记忆"><Pencil size={16} strokeWidth={1.6} /></button>
                        <button type="button" className="nf-icon-button" disabled={memoryUpdating} onClick={() => void removeMemory(memory)} aria-label="删除记忆"><Trash2 size={16} strokeWidth={1.6} /></button>
                      </>}</div>
                    </div>;
                  })}
              </div>}
            </section>
          </div>
        </div>
      </OverlayDialog>
    </div>
  );
}

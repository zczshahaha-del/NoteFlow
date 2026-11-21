import { useEffect, useId, useRef, useState } from "react";
import "./menu-ui.css";
import MemorySettings from "./MemorySettings";
import {
  ChevronRight,
  ChevronLeft,
  CircleUserRound,
  Pencil,
  Smile,
  Sun,
  Download,
  FileText,
  RotateCcw,
  LogOut,
  Settings,
  Trash,
  UserRound,
  X,
} from "lucide-react";
import { syncKnowledgeBeforeBackup } from "../store";
import { exportKnowledgeBackup, importKnowledgeBackup } from "../services/knowledgeBackup";
import { backupAsTree, parseKnowledgeBackup } from "../utils/knowledgeBackup";
import type { KnowledgeBackup } from "../utils/knowledgeBackup";
import { useWorkspaceSlice } from "../store/selectors";
import {
  isMemoryEnabled,
  loadMemorySettings,
  setMemoryEnabled,
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
const settingsNav = [
  { page: "email", label: "账号", Icon: CircleUserRound },
  { page: "appearance", label: "外观", Icon: Sun },
  { page: "memory", label: "记忆", Icon: Smile },
  { page: "data", label: "导入与导出", Icon: Download },
] as const;
type SettingsPage = (typeof settingsNav)[number]["page"] | "trash";

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
  const [settingsPage, setSettingsPage] = useState<SettingsPage>("email");
  const [summaryOpen, setSummaryOpen] = useState(false);
  const summaryButtonRef = useRef<HTMLButtonElement>(null);
  const trashButtonRef = useRef<HTMLButtonElement>(null);
  const settingsBackRef = useRef<HTMLButtonElement>(null);
  const accountPresent = useSurfacePresence(accountOpen);
  const [memoryEnabledState, setMemoryEnabledState] = useState(isMemoryEnabled);
  const [memorySettingLoading, setMemorySettingLoading] = useState(false);
  const [memorySettingError, setMemorySettingError] = useState("");
  const [emailOpen, setEmailOpen] = useState(false);
  const [emailDraft, setEmailDraft] = useState(userEmail);
  const [emailCode, setEmailCode] = useState("");
  const [emailCodeSent, setEmailCodeSent] = useState(false);
  const [emailLoading, setEmailLoading] = useState(false);
  const [emailError, setEmailError] = useState("");
  const [emailMessage, setEmailMessage] = useState("");
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
    if (settingsOpen && settingsPage === "trash") settingsBackRef.current?.focus({ preventScroll: true });
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
  }, [userEmail]);

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

  const openSettings = () => {
    // Capture a stable return target before the menu disappears.
    accountButtonRef.current?.focus({ preventScroll: true });
    setAccountOpen(false);
    setSettingsPage("email");
    setSummaryOpen(false);
    setSettingsOpen(true);
  };
  const closeSummary = () => {
    setSummaryOpen(false);
    requestAnimationFrame(() => summaryButtonRef.current?.focus({ preventScroll: true }));
  };
  const returnFromTrash = () => {
    setSettingsPage("data");
    requestAnimationFrame(() => trashButtonRef.current?.focus({ preventScroll: true }));
  };
  const closeSettings = () => {
    if (summaryOpen) closeSummary();
    else setSettingsOpen(false);
  };
  const settingsLabel = settingsNav.find(item => item.page === settingsPage)?.label ?? "回收站";

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
          <button type="button" className="nf-menu-item" onClick={openSettings}><Settings size={17} strokeWidth={1.6} />设置</button>
          <div className="nf-account-separator" aria-hidden="true" />
          <button type="button" className="nf-menu-item nf-account-signout" onClick={() => { setAccountOpen(false); void onSignOut(); }}><LogOut size={17} strokeWidth={1.6} />退出登录</button>
        </div>
      )}

      <OverlayDialog open={settingsOpen} onClose={closeSettings}
        onEscape={settingsPage === "trash" ? returnFromTrash : closeSettings}
        label={summaryOpen ? "记忆摘要" : "设置"} themeMode={themeMode} className="nf-settings-dialog">
        <div className="nf-settings-layout" inert={summaryOpen} aria-hidden={summaryOpen || undefined}>
          <aside className="nf-settings-sidebar">
            <h2>设置</h2>
            <nav aria-label="设置分类">
              {settingsNav.map(({ page, label, Icon }) => (
                <button key={page} type="button" aria-current={(settingsPage === page || (settingsPage === "trash" && page === "data")) ? "page" : undefined}
                  onClick={() => setSettingsPage(page)}>
                  <Icon size={16} strokeWidth={1.65} aria-hidden="true" /><span>{label}</span>
                </button>
              ))}
            </nav>
          </aside>
          <div className="nf-settings-content">
            <header className="nf-content-heading" data-detail={settingsPage === "trash"}>
              {settingsPage === "trash" && <button ref={settingsBackRef} type="button" className="nf-icon-button" aria-label="返回导入与导出" onClick={returnFromTrash}><ChevronLeft size={17} strokeWidth={1.6} /></button>}
              <h2>{settingsLabel}</h2>
              <button type="button" className="nf-icon-button nf-settings-close" data-dialog-initial-focus aria-label="关闭设置" onClick={closeSettings}><X size={17} strokeWidth={1.6} /></button>
            </header>
            <div ref={settingsScrollRef} className="nf-settings-scroll" data-page={settingsPage}>
              <section key={settingsPage} className="nf-settings-page" aria-label={settingsLabel + "内容"}>
                {settingsPage === "appearance" && <div className="nf-settings-line">
                  <h3>显示模式</h3>
                  <div className="nf-segment" role="group" aria-label="显示模式">
                    <button type="button" aria-pressed={!isDark} onClick={() => onThemeModeChange("light")}>浅色</button>
                    <button type="button" aria-pressed={isDark} onClick={() => onThemeModeChange("dark")}>深色</button>
                  </div>
                </div>}
                {settingsPage === "memory" && <>
                  <div className="nf-settings-line">
                    <div><h3>长期记忆</h3><p>让 AI 在之后的对话中参考记住的内容。</p></div>
                    <button type="button" role="switch" className="nf-switch" onClick={() => void handleMemoryEnabledChange()}
                      disabled={memorySettingLoading} aria-checked={memoryEnabledState} aria-label="长期记忆"><span /></button>
                  </div>
                  {memorySettingError && <p className="nf-notice nf-error" role="alert">{memorySettingError}</p>}
                  <div className="nf-settings-line">
                    <div><h3>记忆摘要</h3><p>查看和管理 AI 记住的内容。</p></div>
                    <button ref={summaryButtonRef} type="button" className="nf-button nf-settings-action" aria-haspopup="dialog" onClick={() => setSummaryOpen(true)}>查看摘要</button>
                  </div>
                </>}

                {settingsPage === "email" && <div className="nf-settings-detail">
                  <div className="nf-settings-line">
                    <div><h3>登录邮箱</h3><p>{userEmail} · {userEmailVerified ? "已验证" : "待验证"}</p></div>
                    <button type="button" className="nf-button nf-settings-action" aria-expanded={emailOpen} onClick={() => { setEmailOpen((open) => !open); setEmailError(""); setEmailMessage(""); }}>
                      <Pencil size={15} strokeWidth={1.65} aria-hidden="true" />更换邮箱
                    </button>
                  </div>
                  {emailOpen && <div className="nf-detail">
                    <label htmlFor={accountPanelId + "-email"}>登录邮箱地址</label>
                    <input id={accountPanelId + "-email"} type="email" aria-label="登录邮箱地址" value={emailDraft} onChange={(event) => { setEmailDraft(event.target.value); setEmailCodeSent(false); setEmailCode(""); }} className="nf-input" placeholder="name@example.com" />
                    {emailCodeSent && <><label htmlFor={accountPanelId + "-code"}>邮箱验证码</label><input id={accountPanelId + "-code"} aria-label="邮箱验证码" value={emailCode} onChange={(event) => setEmailCode(event.target.value.replace(/\D/g, "").slice(0, 6))} inputMode="numeric" autoComplete="one-time-code" maxLength={6} className="nf-input" placeholder="输入 6 位验证码" /></>}
                    {emailError && <p className="nf-notice nf-error" role="alert">{emailError}</p>}
                    {emailMessage && <p className="nf-notice" role="status">{emailMessage}</p>}
                    <button type="button" className="nf-button nf-primary" disabled={emailLoading || !emailDraft.trim() || Boolean(emailCodeSent && emailCode.length !== 6)} onClick={() => void (emailCodeSent ? handleConfirmEmail() : handleRequestEmailCode())}>{emailLoading ? "处理中…" : emailCodeSent ? "确认并绑定" : "发送验证码"}</button>
                  </div>}
                </div>}

                {settingsPage === "data" && <>
                  <div className="nf-settings-line">
                    <div><h3>导入笔记</h3><p>支持 Markdown、ZIP 和知识库备份。</p></div>
                    <button type="button" className="nf-button nf-settings-action" disabled={dataBusy || !!pendingImport.current} onClick={() => importInputRef.current?.click()}>选择文件</button>
                    <input ref={importInputRef} type="file" accept=".md,.markdown,.zip,.json,text/markdown,application/zip,application/json" hidden onChange={(event) => void handleImportKnowledge(event.target.files)} />
                  </div>
                  {importStatus && <p className="nf-notice" role="status">{importStatus}</p>}
                  {pendingImport.current && !dataBusy && <div className="flex gap-2"><button type="button" className="nf-button" onClick={() => void retryImport()}>重试导入</button><button type="button" className="nf-button" onClick={() => {pendingImport.current=null;setImportStatus("已停止重试，请刷新确认服务端结果");}}>停止重试</button></div>}
                  <div className="nf-settings-line">
                    <div><h3>导出 Markdown 文件</h3><p id={accountPanelId + "-export-markdown"}>导出笔记正文，便于在其他软件里阅读。</p></div>
                    <button type="button" className="nf-button nf-settings-action" aria-label="导出 Markdown 文件" aria-describedby={accountPanelId + "-export-markdown"} disabled={dataBusy} onClick={() => void handleExport("zip")}>导出</button>
                  </div>
                  <div className="nf-settings-line">
                    <div><h3>导出知识库备份</h3><p id={accountPanelId + "-export-backup"}>用于恢复目录、笔记、历史版本和附件。不含聊天与长期记忆。</p></div>
                    <button type="button" className="nf-button nf-settings-action" aria-label="导出知识库备份" aria-describedby={accountPanelId + "-export-backup"} disabled={dataBusy} onClick={() => void handleExport("json")}>导出</button>
                  </div>
                  {exportStatus && <p className="nf-notice" role="status">{exportStatus}</p>}
                  <button type="button" ref={trashButtonRef} className="nf-row nf-settings-trash-entry" onClick={() => setSettingsPage("trash")}><span>从回收站恢复</span><span className="nf-row-end">{trashCount || ""}<ChevronRight size={15} aria-hidden="true" /></span></button>
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

              </section>
            </div>
          </div>
        </div>
        {summaryOpen && <MemorySettings key={userEmail} onClose={closeSummary} />}
      </OverlayDialog>
    </div>
  );
}

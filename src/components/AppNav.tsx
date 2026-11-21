import { useEffect, useId, useRef, useState } from "react";
import "./menu-ui.css";
import {
  Brain,
  Database,
  Download,
  SlidersHorizontal,
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
import { loadMemorySettings } from "../services/memories";
import MemorySettings from "./MemorySettings";
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
const settingsPages = {
  general: { label: "通用", icon: SlidersHorizontal },
  memory: { label: "AI 记忆", icon: Brain },
  data: { label: "笔记数据", icon: Database },
  trash: { label: "回收站", icon: Trash },
} as const;
type SettingsPage = keyof typeof settingsPages;

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
  const [settingsPage, setSettingsPage] = useState<SettingsPage>("general");
  const accountPresent = useSurfacePresence(accountOpen);
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
    void loadMemorySettings().catch(() => {});
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

  const openSettings = (page: "general" | "trash") => {
    accountButtonRef.current?.focus({ preventScroll: true });
    setAccountOpen(false);
    setSettingsPage(page);
    setSettingsOpen(true);
  };

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
          <button type="button" className="nf-menu-item" onClick={() => openSettings("general")}><Settings size={17} strokeWidth={1.6} />设置</button>
          <button type="button" className="nf-menu-item" onClick={() => openSettings("trash")}><Trash size={17} strokeWidth={1.6} />回收站{trashCount > 0 && <span className="nf-row-end ml-auto">{trashCount}</span>}</button>
          <div className="nf-account-separator" aria-hidden="true" />
          <button type="button" className="nf-menu-item nf-account-signout" onClick={() => { setAccountOpen(false); void onSignOut(); }}><LogOut size={17} strokeWidth={1.6} />退出登录</button>
        </div>
      )}

      <OverlayDialog open={settingsOpen} onClose={() => setSettingsOpen(false)}
        label="设置" themeMode={themeMode} className="nf-settings-dialog">
        <header className="nf-settings-header">
          <h2>设置</h2>
          <button type="button" className="nf-icon-button" data-dialog-initial-focus aria-label="关闭设置"
            onClick={() => setSettingsOpen(false)}><X size={18} strokeWidth={1.7} /></button>
        </header>
        <nav className="nf-settings-nav" aria-label="设置分类">
          {Object.entries(settingsPages).map(([page, { label, icon: Icon }]) =>
            <button type="button" key={page} className="nf-settings-nav-item"
              aria-current={settingsPage === page ? "page" : undefined} onClick={() => setSettingsPage(page as SettingsPage)}>
              <Icon size={17} strokeWidth={1.7} aria-hidden="true" /><span>{label}</span>
            </button>
          )}
        </nav>
        <div ref={settingsScrollRef} className="nf-settings-scroll">
          <section key={settingsPage} className="nf-settings-page" aria-label={settingsPages[settingsPage].label}>
            <h3 className="nf-setting-title">{settingsPages[settingsPage].label}</h3>
            {settingsPage === "general" && <>
              <div className="nf-setting-row">
                <div><strong>登录邮箱</strong><p><span className="nf-setting-email">{userEmail}</span> · {userEmailVerified ? "已验证" : "待验证"}</p></div>
                <button type="button" className="nf-button" aria-expanded={emailOpen}
                  onClick={() => { setEmailOpen(open => !open); setEmailError(""); setEmailMessage(""); }}>更换邮箱</button>
              </div>
              {emailOpen && <div className="nf-detail nf-setting-email-form">
                <label htmlFor={accountPanelId + "-email"}>登录邮箱地址</label>
                <input id={accountPanelId + "-email"} type="email" value={emailDraft}
                  onChange={event => { setEmailDraft(event.target.value); setEmailCodeSent(false); setEmailCode(""); }}
                  className="nf-input" placeholder="name@example.com" />
                {emailCodeSent && <>
                  <label htmlFor={accountPanelId + "-code"}>邮箱验证码</label>
                  <input id={accountPanelId + "-code"} value={emailCode}
                    onChange={event => setEmailCode(event.target.value.replace(/\D/g, "").slice(0, 6))}
                    inputMode="numeric" autoComplete="one-time-code" maxLength={6} className="nf-input" />
                </>}
                {emailError && <p className="nf-notice nf-error" role="alert">{emailError}</p>}
                {emailMessage && <p className="nf-notice" role="status">{emailMessage}</p>}
                <button type="button" className="nf-button nf-primary"
                  disabled={emailLoading || !emailDraft.trim() || (emailCodeSent && emailCode.length !== 6)}
                  onClick={() => void (emailCodeSent ? handleConfirmEmail() : handleRequestEmailCode())}>
                  {emailLoading ? "处理中…" : emailCodeSent ? "确认并绑定" : "发送验证码"}
                </button>
              </div>}
              <div className="nf-setting-row">
                <strong>显示模式</strong>
                <div className="nf-segment" role="group" aria-label="显示模式">
                  <button type="button" aria-pressed={!isDark} onClick={() => onThemeModeChange("light")}>浅色</button>
                  <button type="button" aria-pressed={isDark} onClick={() => onThemeModeChange("dark")}>深色</button>
                </div>
              </div>
            </>}

            {settingsPage === "memory" && <MemorySettings key={userEmail} />}

            {settingsPage === "data" && <>
              <div className="nf-setting-row">
                <div><strong>导入笔记</strong><p>支持 Markdown、ZIP 和知识库备份。</p></div>
                <button type="button" className="nf-button" disabled={dataBusy || !!pendingImport.current}
                  onClick={() => importInputRef.current?.click()}>选择文件</button>
              </div>
              <input ref={importInputRef} type="file" accept=".md,.markdown,.zip,.json,text/markdown,application/zip,application/json"
                hidden onChange={event => void handleImportKnowledge(event.target.files)} />
              {importStatus && <p className="nf-notice" role="status">{importStatus}</p>}
              {pendingImport.current && !dataBusy && <div className="nf-setting-actions">
                <button type="button" className="nf-button" onClick={() => void retryImport()}>重试导入</button>
                <button type="button" className="nf-button" onClick={() => { pendingImport.current = null; setImportStatus("已停止重试，请刷新确认服务端结果"); }}>停止重试</button>
              </div>}
              <div className="nf-setting-row">
                <div><strong>导出 Markdown</strong><p>导出笔记正文，便于在其他软件里阅读。</p></div>
                <button type="button" className="nf-button nf-setting-download" aria-label="导出 Markdown 文件"
                  disabled={dataBusy} onClick={() => void handleExport("zip")}><Download size={16} />导出</button>
              </div>
              <div className="nf-setting-row">
                <div><strong>导出知识库备份</strong><p>用于恢复目录、笔记、历史版本和附件。不含聊天与长期记忆。</p></div>
                <button type="button" className="nf-button nf-setting-download" aria-label="导出知识库备份"
                  disabled={dataBusy} onClick={() => void handleExport("json")}><Download size={16} />导出</button>
              </div>
              {exportStatus && <p className="nf-notice" role="status">{exportStatus}</p>}
            </>}

            {settingsPage === "trash" && (
              deletedNotes.length === 0 ? <div className="nf-empty"><Trash size={24} strokeWidth={1.6} /><span>回收站是空的</span></div>
                : <ul className="nf-settings-trash">{deletedNotes.map(note => <li key={note.id} className="nf-setting-row">
                  <FileText size={18} strokeWidth={1.6} aria-hidden="true" />
                  <strong>{note.title}</strong>
                  <button type="button" className="nf-button nf-setting-download" aria-label={"恢复 " + note.title}
                    onClick={() => restoreDeletedNote(note.id)}><RotateCcw size={14} strokeWidth={1.6} />恢复</button>
                </li>)}</ul>
            )}
          </section>
        </div>
      </OverlayDialog>
    </div>
  );
}

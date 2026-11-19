// Real editor and store in two browser tabs with a shared, isolated fake API.
import assert from "node:assert/strict";
import { readFileSync, readdirSync } from "node:fs";
import { fileURLToPath } from "node:url";
import { build } from "vite";

const { chromium } = await import(process.env.NOTEFLOW_PLAYWRIGHT_MODULE || "playwright");
const path = name => fileURLToPath(new URL("../src/" + name, import.meta.url));
const editorSource = path("components/NoteEditor.tsx");
const assets = new URL("../dist/assets/", import.meta.url);
const css = readdirSync(assets)
  .filter(name => /^(index-|NoteEditor-).*\.css$/.test(name))
  .map(name => readFileSync(new URL(name, assets), "utf8")).join("\n");
const entry = `
import React from "react";
import { createRoot } from "react-dom/client";
import NoteEditor from ${JSON.stringify(editorSource)};
import { AppProvider, useAppStore } from ${JSON.stringify(path("store/index.tsx"))};
import { queueOfflineNoteEdit, listOfflineNoteEdits, removeOfflineNoteEdit } from ${JSON.stringify(path("services/offlineQueue.ts"))};
window.testState = { editor: null, store: useAppStore, queueOfflineNoteEdit, listOfflineNoteEdits, removeOfflineNoteEdit };
createRoot(document.getElementById("root")).render(
  React.createElement(AppProvider, { userId: "two-tab-test" }, React.createElement(NoteEditor))
);`;
const bundle = await build({
  configFile: false, logLevel: "silent",
  plugins: [{
    name: "two-tab-note-fixture", enforce: "pre",
    resolveId(id) { if (id === "virtual:conflict") return "\0conflict"; },
    load(id) {
      if (id === "\0conflict") return entry;
      if (id === editorSource) return readFileSync(id, "utf8").replace(
        "      extensions,", "      extensions, onCreate: ({ editor }) => { window.testState.editor = editor; },",
      );
    },
  }],
  build: { write: false, rolldownOptions: { input: "virtual:conflict", output: { codeSplitting: false } } },
});
const code = bundle.output.find(item => item.type === "chunk").code;
const html = `<html lang="zh-CN"><meta name="viewport" content="width=device-width, initial-scale=1">
<style>${css} html,body,#root{height:100%;margin:0}#root{display:flex}</style>
<div id="root"></div><script type="module" src="/bundle.js"></script></html>`;

let revision = 0;
const record = (id, title, content) => ({
  id, title, content, contentHash: "version-" + (++revision), categoryId: null, summary: null,
  tags: [], isPinned: false, isFavorite: false, indexStatus: "indexed",
  createdAt: null, updatedAt: new Date(1_700_000_000_000 + revision * 1000).toISOString(), deletedAt: null,
});
let original = record("note", "双页笔记", "原始内容");
const unopened = record("unopened", "尚未打开的笔记", "单独加载的正文");
let unopenedReads = 0;
const copies = new Map();
const savedKeys = new Map();
const errors = [];
const puts = [];
let holdRead = false;
let releaseRead;
let shouldFailCopy = true;
let offlineApi = false;
const browser = await chromium.launch({ executablePath: process.env.NOTEFLOW_BROWSER_EXECUTABLE || undefined, headless: true });
try {
  const context = await browser.newContext({ viewport: { width: 1280, height: 850 } });
  const a = await context.newPage();
  const b = await context.newPage();
  for (const page of [a, b]) page.on("pageerror", error => errors.push(String(error)));
  await context.route("**/*", async route => {
    const request = route.request();
    const url = new URL(request.url());
    const reply = (json, status = 200) => route.fulfill({ json, status });
    if (url.hostname !== "noteflow.localhost") { errors.push(url.origin); return route.abort(); }
    if (url.pathname === "/") return route.fulfill({ body: html, contentType: "text/html" });
    if (url.pathname === "/bundle.js") return route.fulfill({ body: code, contentType: "text/javascript" });
    if (offlineApi && url.pathname.startsWith("/api/")) return route.abort("internetdisconnected");
    if (url.pathname === "/api/categories") return reply({ categories: [] });
    if (url.pathname === "/api/chat-sessions") return reply({ sessions: [] });
    if (url.pathname === "/api/knowledge-base") return reply({});
    if (url.pathname.endsWith("/outline")) return reply({ sections: [] });
    if (url.pathname === "/api/notes" && request.method() === "GET") {
      assert.equal(url.searchParams.get("includeContent"), "false");
      const notes = [original, unopened, ...copies.values()].map(({ content, contentHash, ...metadata }) => metadata);
      return reply({ notes });
    }
    if (url.pathname === "/api/notes/unopened") {
      unopenedReads++;
      return reply({ note: unopened });
    }
    if (url.pathname === "/api/notes" && request.method() === "POST") {
      if (shouldFailCopy) { shouldFailCopy = false; return reply({ error: "模拟副本保存失败" }, 503); }
      const payload = request.postDataJSON();
      if (savedKeys.has(payload.idempotencyKey)) return reply({ note: savedKeys.get(payload.idempotencyKey) });
      const copy = record("copy-" + (copies.size + 1), payload.title, payload.content);
      copies.set(copy.id, copy);
      savedKeys.set(payload.idempotencyKey, copy);
      return reply({ note: copy });
    }
    if (url.pathname === "/api/notes/note" && request.method() === "PUT") {
      const payload = request.postDataJSON();
      puts.push(payload);
      if (original.contentHash !== payload.expectedContentHash) {
        return reply({ error: { code: "NOTE_VERSION_CONFLICT", message: "此版本已经过期" } }, 409);
      }
      original = record("note", original.title, payload.content);
      return reply({ note: original });
    }
    if (url.pathname === "/api/notes/note") {
      const snapshot = original;
      if (holdRead && request.frame().page() === b) {
        holdRead = false;
        await new Promise(resolve => { releaseRead = resolve; });
      }
      return reply({ note: snapshot });
    }
    if (url.pathname.startsWith("/api/notes/copy-")) return reply({ note: copies.get(url.pathname.split("/").at(-1)) });
    errors.push(request.method() + " " + url.pathname);
    return reply({ error: "unexpected fixture request" }, 404);
  });
  const enter = async page => {
    await page.goto("http://noteflow.localhost/");
    await page.waitForFunction(() => window.testState?.store.getState().serverSyncReady);
    await page.evaluate(() => window.testState.store.getState().setSelectedFileId("note"));
    await page.getByTestId("tiptap-editor-content").getByText("原始内容").waitFor();
  };
  await enter(a);
  await enter(b);
  assert.equal(unopenedReads, 0, "Loading the library should not request unopened text");
  assert.equal(await b.evaluate(() => window.testState.store.getState().fileContents.unopened), undefined);
  await a.evaluate(() => window.testState.store.getState().setSelectedFileId("unopened"));
  await a.getByTestId("tiptap-editor-content").getByText("单独加载的正文").waitFor();
  assert.equal(unopenedReads, 1);
  await a.evaluate(() => window.testState.store.getState().setSelectedFileId("note"));
  await a.getByTestId("tiptap-editor-content").getByText("原始内容").waitFor();
  const edit = (page, content) => page.evaluate(value => {
    window.testState.editor.commands.setContent(value, { contentType: "markdown" });
  }, content);
  const returnToTab = async page => {
    await page.bringToFront();
    // Headless Chromium may not dispatch window focus when a target is raised.
    await page.evaluate(() => window.dispatchEvent(new Event("focus")));
  };
  const text = page => page.getByTestId("tiptap-editor-content");
  const saved = page => page.waitForFunction(() => window.testState.store.getState().noteSaveState.status === "saved");

  await a.bringToFront();
  await edit(a, "A 页第一次修改");
  await saved(a);
  const firstHash = original.contentHash;
  await returnToTab(b);
  await text(b).getByText("A 页第一次修改").waitFor();
  assert.equal(puts.length, 1, "Clean refresh must not send a save");

  await a.bringToFront();
  await edit(a, "A 页第二次修改");
  await saved(a);
  holdRead = true;
  await returnToTab(b);
  await expectReply();
  await edit(b, "B 页正在写的修改");
  releaseRead();
  await text(b).getByText("B 页正在写的修改").waitFor();
  await b.getByText("另一处已保存更新。你的修改仍在当前页面，重试可以保存为副本。").waitFor();
  assert.equal(puts[2].expectedContentHash, firstHash, "Late read cannot silently advance the edit's base");
  assert.equal(copies.size, 0);
  assert.match(await text(b).innerText(), /B 页正在写的修改/);

  await b.getByRole("button", { name: "重试保存", exact: true }).click();
  await b.getByText("你的修改已保存为副本。", { exact: true }).waitFor();
  assert.equal(await b.evaluate(() => window.testState.store.getState().selectedFileId), "note");
  assert.equal(copies.size, 1);
  assert.match(original.content, /A 页第二次修改/);
  await text(b).getByText("A 页第二次修改").waitFor();

  await b.getByRole("button", { name: "查看两版差异" }).click();
  const dialog = b.getByRole("dialog", { name: "两版笔记差异" });
  await dialog.getByText("A 页第二次修改").waitFor();
  await dialog.getByText("B 页正在写的修改").waitFor();
  await b.setViewportSize({ width: 320, height: 650 });
  const bounds = await dialog.boundingBox();
  assert(bounds.x >= 0 && bounds.x + bounds.width <= 320);
  await b.keyboard.press("Escape");
  await dialog.waitFor({ state: "hidden" });
  assert(await b.getByRole("button", { name: "查看两版差异" }).evaluate(node => document.activeElement === node));
  await b.getByRole("button", { name: "打开我的副本" }).click();
  await text(b).getByText("B 页正在写的修改").waitFor();
  assert.deepEqual(await b.evaluate(async () => {
    const { queueOfflineNoteEdit, listOfflineNoteEdits, removeOfflineNoteEdit } = window.testState;
    const edit = { userId: "offline-fixture", noteId: "note", baseUpdatedAt: null, baseContentHash: "base" };
    const older = await queueOfflineNoteEdit({ ...edit, content: "旧离线内容" });
    const newer = await queueOfflineNoteEdit({ ...edit, content: "新离线内容" });
    await removeOfflineNoteEdit(older);
    const remaining = await listOfflineNoteEdits(edit.userId);
    await removeOfflineNoteEdit(newer);
    return remaining.map(item => item.content);
  }), ["新离线内容"], "Finishing an older sync cannot remove newer queued text");
  await text(a).getByText("A 页第二次修改").waitFor();
  offlineApi = true;
  await context.setOffline(true);
  await edit(a, "断网后立即刷新要保留这行");
  await a.reload();
  await text(a).getByText("断网后立即刷新要保留这行").waitFor();
  await a.getByText("正文已留在本机，等待同步").waitFor();
  assert.equal(original.content, "A 页第二次修改");
  offlineApi = false;
  await context.setOffline(false);
  await saved(a);
  assert.equal(original.content, "断网后立即刷新要保留这行");
  assert.equal(puts.filter(put => put.content === original.content).length, 1);
  await a.reload();
  await text(a).getByText("断网后立即刷新要保留这行").waitFor();
  await b.evaluate(() => window.testState.store.getState().setSelectedFileId("note"));
  await text(b).getByText("断网后立即刷新要保留这行").waitFor();
  const initialCopyCount = copies.size;
  const versionA = "A 标签页离线写的版本";
  const versionB = "B 标签页离线写的版本";
  offlineApi = true;
  await context.setOffline(true);
  await edit(a, versionA);
  await edit(b, versionB);
  await Promise.all([a.reload(), b.reload()]);
  assert.deepEqual(await Promise.all([a, b].map(page => page.evaluate(async () => {
    while (!window.testState?.store.getState().hydratedUserId) await new Promise(resolve => setTimeout(resolve, 20));
    return window.testState.store.getState().fileContents.note;
  }))), [versionA, versionB], "Each tab must restore its own offline edit");
  await Promise.all([a.getByText("正文已留在本机，等待同步").waitFor(), b.getByText("正文已留在本机，等待同步").waitFor()]);
  offlineApi = false;
  await context.setOffline(false);
  await Promise.all([saved(a), saved(b)]);
  const newCopies = [...copies.values()].slice(initialCopyCount);
  assert.equal(newCopies.length, 1);
  assert.deepEqual(new Set([original.content, newCopies[0].content]), new Set([versionA, versionB]));
  const conflictTab = original.content === versionA ? b : a;
  await conflictTab.getByText("你的修改已保存为副本。", { exact: true }).waitFor();
  await conflictTab.getByRole("button", { name: "打开我的副本" }).click();
  await text(conflictTab).getByText(newCopies[0].content, { exact: true }).waitFor();
  for (const page of [a, b]) {
    await page.evaluate(() => window.testState.store.getState().setSelectedFileId("note"));
    await text(page).getByText(original.content, { exact: true }).waitFor();
  }
  const scopes = await Promise.all([a, b].map(page => page.evaluate(() => sessionStorage.getItem("noteflow:note-journal-tab"))));
  assert(scopes.every(Boolean) && scopes[0] !== scopes[1]);
  const opening = context.waitForEvent("page");
  await a.evaluate(() => { window.open(location.href, "_blank"); });
  const duplicate = await opening;
  await duplicate.waitForFunction(() => window.testState?.store.getState().serverSyncReady);
  const duplicateScope = await duplicate.evaluate(() => {
    if (!window.opener || !navigator.locks) throw new Error("Expected an opener and Web Locks");
    return sessionStorage.getItem("noteflow:note-journal-tab");
  });
  assert(duplicateScope && !scopes.includes(duplicateScope));
  await duplicate.close();
  assert.deepEqual(await a.evaluate(async tabIds => {
    const { queueOfflineNoteEdit, listOfflineNoteEdits, removeOfflineNoteEdit } = window.testState;
    const base = { userId: "fallback-fixture", noteId: "note", baseUpdatedAt: null };
    await Promise.all(tabIds.map((tabId, index) => queueOfflineNoteEdit({ ...base, tabId, content: `fallback-${index}` })));
    const records = await listOfflineNoteEdits(base.userId);
    await Promise.all(records.map(removeOfflineNoteEdit));
    return records.map(record => record.content).sort();
  }, scopes), ["fallback-0", "fallback-1"]);

  // Test close-before-save and recovery from a new tab with a different scope.
  const copiesBeforeClosing = copies.size;
  const closedVersions = ["A 离线输入后关闭", "B 离线输入后关闭"];
  offlineApi = true;
  await context.setOffline(true);
  await Promise.all([edit(a, closedVersions[0]), edit(b, closedVersions[1])]);
  await Promise.all([a.close(), b.close()]);
  offlineApi = false;
  await context.setOffline(false);
  const reopened = await context.newPage();
  reopened.on("pageerror", error => errors.push(String(error)));
  await reopened.goto("http://noteflow.localhost/");
  await reopened.waitForFunction(count => {
    const state = window.testState?.store.getState();
    return state?.serverSyncReady && state.treeData.filter(node => node.id.startsWith("copy-")).length === count;
  }, copiesBeforeClosing + 1);
  assert.equal(copies.size, copiesBeforeClosing + 1);
  assert.deepEqual(new Set([original.content, [...copies.values()].at(-1).content]), new Set(closedVersions));
  assert.equal(await reopened.evaluate(() => Object.keys(localStorage).filter(key => key.includes(":note-draft-by-tab:")).length), 0);
  assert.deepEqual(errors, []);
  console.log(JSON.stringify({ lazyBodyRead: true, cleanRefresh: true, lateRefreshDuringTyping: true, copyFailureRetry: true, versionsPreserved: true, comparison: true, compactViewport: true, offlineReloadAndSync: true, simultaneousOfflineTabEdits: true, closedTabsRecovered: true, indexedDbTabIsolation: true, duplicatedTabIsolation: true }));

  async function expectReply() {
    for (let i = 0; !releaseRead && i < 80; i++) await new Promise(resolve => setTimeout(resolve, 25));
    assert(releaseRead, "Focusing tab B should request its saved version");
  }
} finally {
  await browser.close();
}

import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { createServer } from "node:http";
import { tmpdir } from "node:os";
import { join } from "node:path";
import { chromium } from "playwright";
import { build, preview } from "vite";

const base = "/noteflow/";
const cookieName = "noteflow_session";
const user = { id: "preview-auth-test", email: "preview@example.com", displayName: "会话测试", emailVerified: true };
let scenario = "renew";
let renewed = 0;
const received = [];

const backend = createServer((request, response) => {
  request.resume();
  const path = new URL(request.url, "http://localhost").pathname;
  const cookie = request.headers.cookie || "";
  received.push(cookie);
  const token = cookie.startsWith(cookieName + "=") ? cookie.slice(cookieName.length + 1) : "";
  const signedIn = ["fresh", "renewed"].includes(token) && scenario !== "rejected";
  const reply = (body, status = 200, sessionToken) => {
    response.writeHead(status, {
      "Content-Type": "application/json",
      ...(sessionToken ? { "Set-Cookie": `${cookieName}=${sessionToken}; HttpOnly; Path=/; SameSite=Lax` } : {}),
    });
    response.end(JSON.stringify(body));
  };
  const reject = () => reply({ detail: "session expired or invalid" }, 401);

  if (path === "/api/auth/login") return reply({ user }, 200, "fresh");
  if (path === "/api/auth/refresh") {
    if (!signedIn) return reject();
    if (scenario === "unavailable") return reply({ detail: "gateway outage" }, 503);
    renewed++;
    return reply({ user }, 200, "renewed");
  }
  if (!signedIn) return reject();
  if (path === "/api/auth/me") return reply({ user });
  if (path === "/api/categories") {
    if (scenario === "unavailable" || (scenario === "renew" && token === "fresh")) return reject();
    return reply({ categories: [] });
  }
  if (path === "/api/notes") return reply({ notes: [] });
  if (path === "/api/knowledge-base") return reply({ treeData: [], fileContents: {}, selectedFileId: null });
  if (path === "/api/settings") return reply({ settings: { memoryEnabled: false } });
  if (path === "/api/chat-sessions") return reply({ sessions: [] });
  if (path === "/api/agent/runs/latest") return reply({ task: null });
  if (path === "/api/agent/checkpoints/latest") return reply({ checkpoint: null });
  return reply({});
});

const outDir = await mkdtemp(join(tmpdir(), "noteflow-auth-session-"));
let site;
let browser;
try {
  await new Promise(resolve => backend.listen(0, "127.0.0.1", resolve));
  process.env.NOTEFLOW_BASE_PATH = base;
  process.env.VITE_API_BASE_URL = "";
  process.env.VITE_API_PROXY_TARGET = `http://127.0.0.1:${backend.address().port}`;
  await build({ logLevel: "silent", build: { outDir } });
  site = await preview({ logLevel: "silent", build: { outDir }, preview: { host: "127.0.0.1", port: 0 } });
  const url = `http://127.0.0.1:${site.httpServer.address().port}${base}`;
  browser = await chromium.launch({
    executablePath: process.env.NOTEFLOW_BROWSER_EXECUTABLE || undefined,
    headless: true,
  });
  const context = await browser.newContext({ viewport: { width: 900, height: 780 }, reducedMotion: "reduce", serviceWorkers: "block" });
  await context.addCookies([
    { name: cookieName, value: "stale-root", domain: "127.0.0.1", path: "/", httpOnly: true },
    { name: cookieName, value: "stale-subpath", domain: "127.0.0.1", path: base, httpOnly: true },
  ]);
  const page = await context.newPage();
  const errors = [];
  page.on("pageerror", error => errors.push(error.name));
  const loginForm = page.getByRole("button", { name: "使用密码登录", exact: true });
  const signIn = async () => {
    await loginForm.click();
    await page.getByRole("textbox", { name: "邮箱", exact: true }).fill(user.email);
    await page.getByPlaceholder("密码", { exact: true }).fill("synthetic-entry-password");
    await page.getByRole("button", { name: "登录", exact: true }).click();
  };
  const retry = page.getByRole("button", { name: "重新读取", exact: true });
  const workbench = page.locator(".workspace-center-layer");
  const waitForCategories = (status) => page.waitForResponse(response =>
    new URL(response.url()).pathname === base + "api/categories" && response.status() === status);

  await page.goto(url);
  const loaded = waitForCategories(200);
  await signIn();
  await loaded;
  assert.equal(renewed, 1);
  await workbench.waitFor();

  scenario = "unavailable";
  await page.reload();
  await retry.waitFor();
  assert.ok(await workbench.isVisible(), "a transient refresh failure should retain the workspace");
  assert.ok(await page.getByText("暂时无法确认登录状态，请重试。", { exact: true }).isVisible());

  scenario = "rejected";
  await retry.click();
  await loginForm.waitFor();
  assert.equal(await workbench.count(), 0);
  assert.equal(await page.evaluate(() => localStorage.getItem("noteflow:cached-auth-session")), null);

  scenario = "normal";
  const reloaded = waitForCategories(200);
  await signIn();
  await reloaded;
  const cookies = await context.cookies();
  assert.ok(cookies.some(cookie => cookie.path === "/" && cookie.name === cookieName && cookie.value === "stale-root"));
  assert.ok(cookies.some(cookie => cookie.path === base && cookie.name !== cookieName && cookie.httpOnly));
  assert.ok(received.every(cookie => !cookie.includes("stale-")));
  assert.deepEqual(errors, []);
  console.log("Subpath cookie isolation, successful refresh, temporary outage, rejected session and re-login passed.");
} finally {
  await browser?.close();
  await site?.close();
  await new Promise(resolve => backend.close(resolve));
  await rm(outDir, { recursive: true, force: true });
}

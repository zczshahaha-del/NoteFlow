import assert from "node:assert/strict";
import fs from "node:fs";
import vm from "node:vm";

function pngSize(path) {
  const bytes = fs.readFileSync(path);
  assert.deepEqual([...bytes.subarray(0, 8)], [137, 80, 78, 71, 13, 10, 26, 10]);
  return {
    width: bytes.readUInt32BE(16),
    height: bytes.readUInt32BE(20),
  };
}

const manifest = JSON.parse(fs.readFileSync("public/manifest.webmanifest", "utf8"));
assert.equal(manifest.name, "知流 NoteFlow");
for (const base of ["/", "/noteflow/"]) {
  const manifestUrl = new URL(`${base}manifest.webmanifest`, "https://example.test");
  assert.equal(new URL(manifest.start_url, manifestUrl).pathname, base);
  assert.equal(new URL(manifest.scope, manifestUrl).pathname, base);
  for (const icon of manifest.icons) {
    assert.ok(new URL(icon.src, manifestUrl).pathname.startsWith(base));
  }
}
assert.equal(manifest.display, "standalone");
assert.equal(manifest.lang, "zh-CN");

const icons = new Map(manifest.icons.map((icon) => [icon.sizes, icon]));
assert.equal(icons.get("192x192")?.type, "image/png");
assert.equal(icons.get("512x512")?.type, "image/png");
assert.deepEqual(pngSize("public/pwa-192.png"), { width: 192, height: 192 });
assert.deepEqual(pngSize("public/pwa-512.png"), { width: 512, height: 512 });
assert.deepEqual(pngSize("public/apple-touch-icon.png"), { width: 180, height: 180 });

const indexHtml = fs.readFileSync("index.html", "utf8");
assert.match(indexHtml, /rel="manifest" href="\/manifest\.webmanifest"/);
assert.match(indexHtml, /rel="apple-touch-icon" href="\/apple-touch-icon\.png"/);
assert.match(indexHtml, /name="theme-color"/);

const serviceWorker = fs.readFileSync("public/sw.js", "utf8");
for (const base of ["/", "/noteflow/"]) {
  const origin = "https://example.test";
  const cachePrefix = `noteflow-shell:${base}:`;
  const otherBase = base === "/" ? "/noteflow/" : "/";
  const listeners = new Map();
  const deleted = [];
  let installed = [];
  const activeCache = {
    addAll: async (assets) => { installed = [...assets]; },
    match: async () => undefined,
    put: async () => {},
  };
  vm.runInNewContext(serviceWorker, {
    URL,
    self: {
      registration: { scope: origin + base },
      addEventListener: (type, listener) => listeners.set(type, listener),
      skipWaiting() {},
      clients: { claim() {} },
    },
    caches: {
      open: async (name) => {
        assert.equal(name, `${cachePrefix}v1`);
        return activeCache;
      },
      keys: async () => ["other-product", "noteflow-shell-v2", `${cachePrefix}old`, `${cachePrefix}v1`, `noteflow-shell:${otherBase}:v1`],
      delete: async (name) => { deleted.push(name); },
    },
    fetch: async () => ({ ok: true, clone() { return this; } }),
  });

  for (const type of ["install", "activate"]) {
    let pending;
    listeners.get(type)({ waitUntil: (promise) => { pending = promise; } });
    await pending;
  }
  for (const resource of ["", "manifest.webmanifest", "pwa-192.png", "pwa-512.png", "apple-touch-icon.png"]) {
    assert.ok(installed.includes(origin + base + resource));
  }
  assert.deepEqual(deleted, base === "/" ? ["noteflow-shell-v2", `${cachePrefix}old`] : [`${cachePrefix}old`]);

  const fetchThroughWorker = (url, method = "GET") => {
    let result;
    listeners.get("fetch")({
      request: { url, method, mode: "navigate" },
      respondWith: (response) => { result = response; },
    });
    return result;
  };
  assert.equal(fetchThroughWorker(origin + base + "api/auth/me"), undefined);
  assert.equal(fetchThroughWorker(origin + base + "api"), undefined);
  assert.equal(fetchThroughWorker(origin + base, "POST"), undefined);
  assert.equal(fetchThroughWorker("https://other.test" + base), undefined);
  if (base !== "/") assert.equal(fetchThroughWorker(origin + "/kejian/"), undefined);
  assert.equal((await fetchThroughWorker(origin + base)).ok, true);
}

console.log(JSON.stringify({
  ok: true,
  display: manifest.display,
  icons: [pngSize("public/pwa-192.png"), pngSize("public/pwa-512.png")],
  offlineShellAssets: true,
}));

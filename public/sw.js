const SCOPE = new URL(self.registration.scope);
const CACHE_PREFIX = `noteflow-shell:${SCOPE.pathname}:`;
const CACHE_NAME = `${CACHE_PREFIX}v1`;
const API_PREFIX = new URL("api/", SCOPE).pathname;
const SHELL_ASSETS = [
  "./",
  "manifest.webmanifest",
  "noteflow_icon.svg",
  "favicon.svg",
  "pwa-192.png",
  "pwa-512.png",
  "apple-touch-icon.png",
].map((asset) => new URL(asset, SCOPE).href);

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL_ASSETS)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) =>
        (key.startsWith(CACHE_PREFIX) && key !== CACHE_NAME) ||
        (SCOPE.pathname === "/" && key === "noteflow-shell-v2")
      ).map((key) => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  const url = new URL(request.url);
  if (request.method !== "GET" || url.origin !== SCOPE.origin ||
    !url.pathname.startsWith(SCOPE.pathname) ||
    url.pathname === API_PREFIX.slice(0, -1) || url.pathname.startsWith(API_PREFIX)) {
    return;
  }

  event.respondWith((async () => {
    const cache = await caches.open(CACHE_NAME);
    if (request.mode === "navigate") {
      try {
        const response = await fetch(request);
        if (response.ok) await cache.put(SCOPE.href, response.clone()).catch(() => undefined);
        return response;
      } catch (error) {
        const fallback = await cache.match(SCOPE.href);
        if (fallback) return fallback;
        throw error;
      }
    }
    const cached = await cache.match(request);
    if (cached) return cached;
    const response = await fetch(request);
    if (response.ok) await cache.put(request, response.clone()).catch(() => undefined);
    return response;
  })());
});

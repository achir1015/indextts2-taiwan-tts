// 最簡單的 Service Worker：讓手機可以「安裝」成 App。
// 不做離線快取（語音一定要連到這台電腦才能產生），只把網頁外殼存起來加快開啟。
const SHELL = "tw-tts-shell-v1";
const FILES = ["/", "/static/icons/icon-192.png", "/static/manifest.webmanifest"];
self.addEventListener("install", e => {
  e.waitUntil(caches.open(SHELL).then(c => c.addAll(FILES)).catch(() => {}));
  self.skipWaiting();
});
self.addEventListener("activate", e => e.waitUntil(self.clients.claim()));
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  // API、音檔一律走網路；只有首頁外殼在斷線時用快取
  if (e.request.method !== "GET" || url.pathname.startsWith("/api/") || url.pathname.startsWith("/outputs/")) return;
  if (url.pathname === "/") {
    e.respondWith(fetch(e.request).catch(() => caches.match("/")));
  }
});

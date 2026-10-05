// 拾光's service worker. Its only job: the page opens without the Pi, so books cached on this device can be read
// (away from home). Browsers run service workers only over https or on localhost — on the LAN's plain http the page
// never registers it. The network always comes first; the cached page and static files are used only when the
// network fails. The API, videos and books never go through it (books are cached by the page, in IndexedDB).
const CACHE = "shiguang-shell-v1";
const SHELL = ["/", "/static/pixel.woff2", "/static/pdfjs/pdf.min.mjs", "/static/pdfjs/pdf.worker.min.mjs"];

self.addEventListener("install", e => {
  e.waitUntil(caches.open(CACHE).then(c => c.addAll(SHELL)).then(() => self.skipWaiting()));
});
self.addEventListener("activate", e => {
  e.waitUntil(caches.keys().then(keys => Promise.all(keys.filter(k => k !== CACHE).map(k => caches.delete(k))))
    .then(() => self.clients.claim()));
});
self.addEventListener("fetch", e => {
  const url = new URL(e.request.url);
  if (e.request.method !== "GET" || url.origin !== location.origin) return;
  const nav = e.request.mode === "navigate";
  if (!nav && !url.pathname.startsWith("/static/")) return;  // as if there were no service worker
  const key = nav ? "/" : url.pathname;
  e.respondWith(fetch(e.request).then(r => {
    if (r.ok && (!nav || url.pathname === "/")) { const copy = r.clone(); caches.open(CACHE).then(c => c.put(key, copy)); }
    return r;
  }).catch(() => caches.match(key).then(r => r || Response.error())));
});

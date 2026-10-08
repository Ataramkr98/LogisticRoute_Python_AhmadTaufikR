const CACHE = "logistics-driver-v2";
const SHELL = ["/driver/today/", "/static/build/app.css", "/static/build/app.js"];
self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)));
  self.skipWaiting();
});
self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(
        keys.filter((key) => key.startsWith("logistics-driver-") && key !== CACHE)
          .map((key) => caches.delete(key))
      ))
      .then(() => self.clients.claim())
  );
});
self.addEventListener("fetch", (event) => {
  if (event.request.method !== "GET") return;
  const url = new URL(event.request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/driver/") || url.pathname.startsWith("/static/")) {
    event.respondWith(fetch(event.request).then((response) => {
      const responseUrl = new URL(response.url);
      const canCache = response.ok && responseUrl.origin === self.location.origin && (
        url.pathname.startsWith("/static/") || responseUrl.pathname.startsWith("/driver/")
      );
      if (canCache) {
        const copy = response.clone();
        caches.open(CACHE).then((cache) => cache.put(event.request, copy));
      }
      return response;
    }).catch(() => caches.match(event.request).then((cached) => {
      if (cached) return cached;
      if (event.request.mode === "navigate" && url.pathname.startsWith("/driver/")) {
        return caches.match("/driver/today/");
      }
      return Response.error();
    })));
  }
});

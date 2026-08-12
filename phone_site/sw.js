/* aiOS Director service worker.

   The app shell is cached so the PWA opens instantly and survives a dead
   network long enough to say so. Director's own API is never cached: stale
   agent state on a phone is worse than an honest error.

   iOS will show a blank "Load Failed" screen if install() rejects. Vercel
   cleanUrls 308s /index.html -> /, and cache.addAll treats that as failure,
   so we cache each file on its own and never fall back a script to HTML. */

const VERSION = "director-v25";
const SHELL = [
  "/",
  "/director.css",
  "/director.js",
  "/code/transcript.js",
  "/code/markdown.js",
  "/code/code.css",
  "/code/code-beautiful.css",
  "/manifest.webmanifest",
  "/icons/aios-icon-192.png",
  "/icons/aios-icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil((async () => {
    const cache = await caches.open(VERSION);
    await Promise.all(SHELL.map((url) => cache.add(url).catch(() => {})));
    await self.skipWaiting();
  })());
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== VERSION).map((key) => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("push", (event) => {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch {
    payload = { title: "aiOS Director", body: event.data ? event.data.text() : "" };
  }
  const title = payload.title || "aiOS Director";
  event.waitUntil(
    self.registration.showNotification(title, {
      body: payload.body || "",
      tag: payload.tag || "director",
      renotify: true,
      icon: "/icons/aios-icon-192.png",
      badge: "/icons/aios-icon-192.png",
      data: { url: payload.url || "/" },
    })
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const target = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((clients) => {
      for (const client of clients) {
        if (client.url.includes(self.location.origin) && "focus" in client) {
          client.postMessage({ type: "notification", url: target });
          return client.focus();
        }
      }
      return self.clients.openWindow(target);
    })
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/")) return;

  const isPage = request.mode === "navigate" || url.pathname === "/" || url.pathname.endsWith(".html");

  event.respondWith((async () => {
    try {
      const response = await fetch(request);
      if (response.ok) {
        const copy = response.clone();
        caches.open(VERSION).then((cache) => cache.put(request, copy)).catch(() => {});
      }
      return response;
    } catch {
      const hit = await caches.match(request);
      if (hit) return hit;
      if (isPage) {
        const page = await caches.match("/");
        if (page) return page;
      }
      return Response.error();
    }
  })());
});

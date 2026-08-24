/* aiOS Director service worker.

   The app shell is cached so the PWA opens instantly and survives a dead
   network long enough to say so. Director's own API is never cached: stale
   agent state on a phone is worse than an honest error.

   iOS will show a blank "Load Failed" screen if install() rejects. Vercel
   cleanUrls 308s /index.html -> /, and cache.addAll treats that as failure,
   so we cache each file on its own and never fall back a script to HTML. */

const VERSION = "director-v47";
const SHELL = [
  "/",
  "/director.css?v=47",
  "/director.js?v=47",
  "/code/transcript.js?v=47",
  "/code/markdown.js",
  "/code/code.css?v=47",
  "/code/code-beautiful.css?v=47",
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
    payload = { title: "Director", body: event.data ? event.data.text() : "" };
  }
  const title = payload.agent || payload.title || "Director";
  event.waitUntil((async () => {
    // A visible Director window is already showing the live message. This is
    // deliberately app-wide rather than thread-specific: being on the home
    // screen should not cause a push banner from a chat in the same app.
    const windows = await self.clients.matchAll({
      type: "window",
      includeUncontrolled: true,
    });
    if (windows.some((client) => client.visibilityState === "visible")) return;
    await self.registration.showNotification(title, {
      body: payload.body || "",
      tag: payload.tag || "director",
      renotify: true,
      icon: "/icons/aios-icon-192.png",
      badge: "/icons/aios-icon-192.png",
      data: { url: payload.url || "/" },
    });
  })());
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

  const cacheKey = isPage ? "/" : request;
  const fresh = fetch(request).then(async (response) => {
    if (response.ok) {
      const cache = await caches.open(VERSION);
      await cache.put(cacheKey, response.clone());
    }
    return response;
  });

  // The installed PWA should paint from its versioned shell immediately.
  // Refresh the same entry in the background so the next launch is current.
  event.waitUntil(fresh.then(() => undefined).catch(() => undefined));
  event.respondWith(
    caches.match(cacheKey).then((hit) => hit || fresh.catch(async () => {
      if (isPage) return (await caches.match("/")) || Response.error();
      return Response.error();
    }))
  );
});

/* aiOS Director service worker.

   The app shell is cached so the PWA opens instantly and survives a dead
   network long enough to say so. Director's own API is never cached: stale
   agent state on a phone is worse than an honest error. */

const VERSION = "director-v3";
const SHELL = [
  "/",
  "/index.html",
  "/director.css",
  "/director.js",
  "/manifest.webmanifest",
  "/icons/aios-icon-192.png",
  "/icons/aios-icon-512.png",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches.open(VERSION).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting())
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches.keys()
      .then((keys) => Promise.all(keys.filter((key) => key !== VERSION).map((key) => caches.delete(key))))
      .then(() => self.clients.claim())
  );
});

/* Push. Director sends {title, body, url, tag}; the tag collapses repeats for
   one conversation so a chatty run does not stack ten banners. */
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
      // Focus the app if it is already open rather than opening a second copy.
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
  // Only ever serve our own origin from cache. Director lives elsewhere.
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/")) return;

  event.respondWith(
    fetch(request)
      .then((response) => {
        const copy = response.clone();
        caches.open(VERSION).then((cache) => cache.put(request, copy)).catch(() => {});
        return response;
      })
      .catch(() => caches.match(request).then((hit) => hit || caches.match("/index.html")))
  );
});

const CACHE = "aios-remote-v36";
const SHELL = [
  "./",
  "phone.css",
  "phone.js",
  "version.json",
  "manifest.webmanifest",
  "icons/aios-icon-180.png",
  "icons/aios-icon-192.png",
  "icons/aios-icon-512.png"
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE).then((cache) => cache.addAll(SHELL)).then(() => self.skipWaiting()));
});

self.addEventListener("activate", (event) => {
  event.waitUntil(caches.keys().then((keys) => Promise.all(keys.filter((key) => key !== CACHE).map((key) => caches.delete(key)))).then(() => self.clients.claim()));
});

self.addEventListener("fetch", (event) => {
  const url = new URL(event.request.url);
  if (event.request.method !== "GET" || url.pathname.startsWith("/api/")) return;
  event.respondWith(fetch(event.request).then((response) => {
    const copy = response.clone();
    caches.open(CACHE).then((cache) => cache.put(event.request, copy));
    return response;
  }).catch(() => caches.match(event.request).then((cached) => cached || caches.match("./"))));
});

/* The relay sends these while the app is closed — on iOS that is the only
 * moment notifications can appear at all, so every push must show one. */
self.addEventListener("push", (event) => {
  let data = {};
  try {
    data = event.data ? event.data.json() : {};
  } catch {
    data = { body: event.data ? event.data.text() : "" };
  }
  const title = data.title || "aiOS Remote";
  event.waitUntil(self.registration.showNotification(title, {
    body: data.body || "",
    tag: data.tag || "aios-remote",
    renotify: true,
    requireInteraction: Boolean(data.requireInteraction),
    icon: "icons/aios-icon-192.png",
    badge: "icons/aios-icon-192.png",
    data: { url: data.url || "./", tag: data.tag || "", machineId: data.machine_id || "" }
  }));
});

/* A rotated subscription is re-registered by the app on next open — it holds
 * the session token this worker does not. Keep the old one from lingering. */
self.addEventListener("pushsubscriptionchange", (event) => {
  event.waitUntil(self.clients.matchAll({ includeUncontrolled: true })
    .then((list) => list.forEach((client) => client.postMessage({ type: "push-subscription-changed" }))));
});

self.addEventListener("notificationclick", (event) => {
  const action = event.action || "open";
  event.notification.close();
  if (action === "dismiss") return;
  const target = event.notification?.data?.url || "./";
  event.waitUntil(clients.matchAll({ type: "window", includeUncontrolled: true }).then((list) => {
    for (const client of list) {
      if ("focus" in client) {
        client.focus();
        if (client.navigate) return client.navigate(target);
        return undefined;
      }
    }
    if (clients.openWindow) return clients.openWindow(target);
    return undefined;
  }));
});

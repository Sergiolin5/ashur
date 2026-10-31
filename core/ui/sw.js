// Ashur's service worker: it only receives notifications and opens the app when one is tapped.
// It caches nothing and never touches the network, so it can not serve stale or foreign pages.
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", (e) => e.waitUntil(self.clients.claim()));

self.addEventListener("push", (event) => {
  let d = {};
  try { d = event.data ? event.data.json() : {}; } catch (e) { d = { body: event.data ? event.data.text() : "" }; }
  event.waitUntil(self.registration.showNotification(d.title || "Ashur", {
    body: d.body || "", tag: d.tag || "ashur", renotify: false,
    icon: "/apple-touch-icon.png", data: { url: d.url || "/" }
  }));
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  const url = (event.notification.data && event.notification.data.url) || "/";
  event.waitUntil(self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((list) => {
    for (const c of list) {
      if ("focus" in c) { c.postMessage({ open: url }); return c.focus(); }
    }
    return self.clients.openWindow(url);
  }));
});

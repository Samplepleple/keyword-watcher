// 背景接收推播：網頁沒打開時，iPhone 也會跳出通知
self.addEventListener("install", () => self.skipWaiting());
self.addEventListener("activate", e => e.waitUntil(self.clients.claim()));

self.addEventListener("push", e => {
  let d = {};
  try { d = e.data ? e.data.json() : {}; } catch { d = { body: e.data && e.data.text() }; }
  e.waitUntil(self.registration.showNotification(d.title || "關鍵字監控", {
    body: d.body || "",
    icon: "icon-192.png",
    data: { url: d.url || "./" },
  }));
});

// 點通知：打開文章（或監控網頁）
self.addEventListener("notificationclick", e => {
  e.notification.close();
  e.waitUntil(self.clients.openWindow(e.notification.data.url));
});

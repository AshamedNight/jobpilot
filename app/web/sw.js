/* jobpilot service worker（闭环 6）
 *
 * 只做两件事：缓存壳子、接收通知点击。
 *
 * 三条刻意的设计：
 * 1. 接口路径一律不缓存：页面上所有数字都必须是真的。缓存一个过期的
 *    used=2 的旧数字比页面打不开更糟 —— 用户会以为没在发。
 * 2. 壳子用 cache-first：断网也能打开，看到的是"最后一次成功加载的界面"。
 * 3. 不做后台推送（Push API 需要 HTTPS + 订阅端点，本地回环用不上）。
 *    通知由页面轮询触发；SW 只负责在通知被点击时把窗口拉回来。
 */

const CACHE = "jobpilot-v2";
const SHELL = [
  "/",
  "/static/index.html",
  "/static/style.css",
  "/static/app.js",
  "/static/icon.svg",
  "/static/manifest.webmanifest",
];

self.addEventListener("install", (event) => {
  event.waitUntil(
    caches
      .open(CACHE)
      .then((cache) => cache.addAll(SHELL))
      .then(() => self.skipWaiting())
      .catch(() => self.skipWaiting()) // 缓存失败也要能用
  );
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(keys.filter((k) => k !== CACHE).map((k) => caches.delete(k))))
      .then(() => self.clients.claim())
  );
});

self.addEventListener("fetch", (event) => {
  const request = event.request;
  if (request.method !== "GET") return;

  const url = new URL(request.url);
  if (url.origin !== self.location.origin) return;
  if (url.pathname.startsWith("/api/")) return; // 实时数据永不缓存

  event.respondWith(
    caches.match(request).then((cached) => cached || fetch(request).catch(() => cached))
  );
});

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(
    self.clients.matchAll({ type: "window", includeUncontrolled: true }).then((clientList) => {
      for (const client of clientList) {
        if ("focus" in client) return client.focus();
      }
      return self.clients.openWindow("/");
    })
  );
});

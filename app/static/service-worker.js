const CACHE_NAME = "selfecho-ai-community-v0.6";
const SHELL = [
  "/static/index.html",
  "/static/styles.css",
  "/static/app.js",
  "/static/manifest.webmanifest",
  "/static/icon.svg",
];

self.addEventListener("install", (event) => {
  event.waitUntil(caches.open(CACHE_NAME).then((cache) => cache.addAll(SHELL)));
  self.skipWaiting();
});

self.addEventListener("activate", (event) => {
  event.waitUntil(
    caches
      .keys()
      .then((keys) => Promise.all(
        keys.filter((key) => key !== CACHE_NAME).map((key) => caches.delete(key)),
      ))
  );
  self.clients.claim();
});

const GENERIC_NOTIFICATION_TITLE = "SelfEcho";
const GENERIC_NOTIFICATION_BODY = "你有一条 SelfEcho 微提醒。";
const DEFAULT_NOTIFICATION_TARGET = "/dashboard";

function safeNotificationTargetPath(value) {
  if (value !== DEFAULT_NOTIFICATION_TARGET) return DEFAULT_NOTIFICATION_TARGET;
  try {
    const target = new URL(value, self.location.origin);
    if (
      target.origin !== self.location.origin ||
      target.pathname !== DEFAULT_NOTIFICATION_TARGET ||
      target.search ||
      target.hash
    ) {
      return DEFAULT_NOTIFICATION_TARGET;
    }
    return target.pathname;
  } catch (_) {
    return DEFAULT_NOTIFICATION_TARGET;
  }
}

function parsePushNotification(event) {
  let payload = {};
  try {
    payload = event.data ? event.data.json() : {};
  } catch (_) {
    payload = {};
  }
  if (!payload || typeof payload !== "object" || Array.isArray(payload)) {
    payload = {};
  }
  return {
    title: GENERIC_NOTIFICATION_TITLE,
    options: {
      body: GENERIC_NOTIFICATION_BODY,
      icon: "/static/icon.svg",
      tag: "selfecho-reminder",
      data: {
        targetPath: safeNotificationTargetPath(payload.target_path),
      },
    },
  };
}

self.addEventListener("push", (event) => {
  const notification = parsePushNotification(event);
  event.waitUntil(
    self.registration.showNotification(notification.title, notification.options),
  );
});

async function openNotificationTarget(targetPath) {
  const safePath = safeNotificationTargetPath(targetPath);
  const targetUrl = new URL(safePath, self.location.origin).href;
  const windows = await self.clients.matchAll({
    type: "window",
    includeUncontrolled: true,
  });
  const existing = windows.find((client) => {
    try {
      return new URL(client.url).origin === self.location.origin;
    } catch (_) {
      return false;
    }
  });
  if (existing) {
    await existing.navigate(targetUrl);
    return existing.focus();
  }
  return self.clients.openWindow(targetUrl);
}

self.addEventListener("notificationclick", (event) => {
  event.notification.close();
  event.waitUntil(
    openNotificationTarget(event.notification.data?.targetPath),
  );
});

self.addEventListener("fetch", (event) => {
  const requestUrl = new URL(event.request.url);
  if (requestUrl.origin !== self.location.origin || requestUrl.pathname.startsWith("/api/")) {
    return;
  }

  if (event.request.mode === "navigate") {
    event.respondWith(
      fetch(event.request).catch(() => caches.match("/static/index.html"))
    );
    return;
  }

  event.respondWith(
    caches.match(event.request).then((cached) => cached || fetch(event.request))
  );
});

import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const testsDirectory = path.dirname(fileURLToPath(import.meta.url));
const serviceWorkerPath = path.join(
  testsDirectory,
  "..",
  "app",
  "static",
  "service-worker.js",
);
const serviceWorkerSource = readFileSync(serviceWorkerPath, "utf8");

function createHarness({ windows = [], cacheKeys = [] } = {}) {
  const listeners = new Map();
  const notifications = [];
  const openedWindows = [];
  const fetches = [];
  const cacheLifecycle = {
    claimed: 0,
    deleted: [],
    opened: [],
    shells: [],
    skipWaiting: 0,
  };
  const self = {
    addEventListener(type, listener) { listeners.set(type, listener); },
    clients: {
      claim() { cacheLifecycle.claimed += 1; },
      async matchAll(options) {
        assert.deepEqual(JSON.parse(JSON.stringify(options)), {
          type: "window",
          includeUncontrolled: true,
        });
        return windows;
      },
      async openWindow(url) {
        openedWindows.push(url);
        return { url };
      },
    },
    location: { origin: "https://selfecho.example" },
    registration: {
      async showNotification(title, options) {
        notifications.push({ title, options });
      },
    },
    skipWaiting() { cacheLifecycle.skipWaiting += 1; },
  };
  const context = vm.createContext({
    caches: {
      async keys() { return cacheKeys; },
      async match() { return null; },
      async delete(cacheName) {
        cacheLifecycle.deleted.push(cacheName);
        return true;
      },
      async open(cacheName) {
        cacheLifecycle.opened.push(cacheName);
        return {
          async addAll(shell) { cacheLifecycle.shells.push([...shell]); },
        };
      },
    },
    fetch: async (request) => {
      fetches.push(request.url || String(request));
      return {};
    },
    Promise,
    self,
    URL,
  });
  vm.runInContext(serviceWorkerSource, context, { filename: serviceWorkerPath });
  return {
    cacheLifecycle,
    fetches,
    listeners,
    notifications,
    openedWindows,
  };
}

async function dispatchLifecycle(harness, eventName) {
  let completion;
  harness.listeners.get(eventName)({
    waitUntil(promise) { completion = Promise.resolve(promise); },
  });
  await completion;
}

async function dispatchPush(harness, payload, { malformed = false } = {}) {
  let completion;
  harness.listeners.get("push")({
    data: payload === null
      ? null
      : {
          json() {
            if (malformed) throw new SyntaxError("malformed synthetic payload");
            return payload;
          },
        },
    waitUntil(promise) { completion = Promise.resolve(promise); },
  });
  await completion;
}

async function dispatchClick(harness, targetPath) {
  let completion;
  let closed = 0;
  harness.listeners.get("notificationclick")({
    notification: {
      data: { targetPath },
      close() { closed += 1; },
    },
    waitUntil(promise) { completion = Promise.resolve(promise); },
  });
  await completion;
  return closed;
}

test("Stage 5 cache revision precaches only shell assets and removes old cache", async () => {
  const currentCache = "selfecho-ai-community-v0.5";
  const previousCache = "selfecho-ai-reminder-v0.1-stage3b-3";
  const harness = createHarness({ cacheKeys: [previousCache, currentCache] });

  await dispatchLifecycle(harness, "install");
  assert.deepEqual(harness.cacheLifecycle.opened, [currentCache]);
  assert.equal(harness.cacheLifecycle.shells[0].includes("/static/app.js"), true);
  assert.equal(harness.cacheLifecycle.shells[0].some((entry) => entry.startsWith("/api/")), false);
  assert.equal(harness.cacheLifecycle.skipWaiting, 1);

  await dispatchLifecycle(harness, "activate");
  assert.deepEqual(harness.cacheLifecycle.deleted, [previousCache]);
  assert.equal(harness.cacheLifecycle.claimed, 1);
});

test("valid Push payload produces only the privacy-minimal generic notification", async () => {
  const harness = createHarness();
  await dispatchPush(harness, {
    type: "reminder",
    title: "SelfEcho",
    body: "你有一条 SelfEcho 微提醒。",
    target_path: "/dashboard",
    item_id: 29,
    reminder_id: 17,
    raw_capture: "must never persist",
    email: "private@example.com",
  });

  assert.deepEqual(JSON.parse(JSON.stringify(harness.notifications)), [{
    title: "SelfEcho",
    options: {
      body: "你有一条 SelfEcho 微提醒。",
      icon: "/static/icon.svg",
      tag: "selfecho-reminder",
      data: { targetPath: "/dashboard" },
    },
  }]);
  const serialized = JSON.stringify(harness.notifications);
  for (const forbidden of ["item_id", "reminder_id", "raw_capture", "private@example.com"]) {
    assert.equal(serialized.includes(forbidden), false);
  }
});

test("malformed, empty, or unexpected payload safely falls back", async () => {
  for (const [payload, malformed] of [
    [{}, true],
    [null, false],
    [["unexpected"], false],
  ]) {
    const harness = createHarness();
    await dispatchPush(harness, payload, { malformed });
    assert.equal(harness.notifications.length, 1);
    assert.equal(harness.notifications[0].title, "SelfEcho");
    assert.equal(harness.notifications[0].options.data.targetPath, "/dashboard");
  }
});

test("notification click focuses and safely navigates an existing same-origin window", async () => {
  const calls = { focus: 0, navigate: [] };
  const existing = {
    url: "https://selfecho.example/account",
    async focus() { calls.focus += 1; },
    async navigate(url) { calls.navigate.push(url); },
  };
  const harness = createHarness({
    windows: [{ url: "https://other.example/" }, existing],
  });

  const closed = await dispatchClick(harness, "/dashboard");

  assert.equal(closed, 1);
  assert.deepEqual(calls.navigate, ["https://selfecho.example/dashboard"]);
  assert.equal(calls.focus, 1);
  assert.deepEqual(harness.openedWindows, []);
});

test("notification click opens Dashboard when no same-origin window exists", async () => {
  const harness = createHarness();

  await dispatchClick(harness, "/dashboard");

  assert.deepEqual(harness.openedWindows, [
    "https://selfecho.example/dashboard",
  ]);
});

test("external, protocol-relative, malformed, and unknown click targets fall back", async () => {
  for (const target of [
    "https://attacker.example/steal",
    "//attacker.example/steal",
    "javascript:alert(1)",
    "/items/29",
    "/dashboard?next=https://attacker.example",
    "/dashboard#fragment",
    null,
  ]) {
    const harness = createHarness();
    await dispatchClick(harness, target);
    assert.deepEqual(harness.openedWindows, [
      "https://selfecho.example/dashboard",
    ]);
  }
});

test("API requests bypass cache and Push handling does not cache or fetch data", async () => {
  const harness = createHarness();
  let responded = false;
  harness.listeners.get("fetch")({
    request: {
      mode: "cors",
      url: "https://selfecho.example/api/items",
    },
    respondWith() { responded = true; },
  });
  assert.equal(responded, false);

  await dispatchPush(harness, { target_path: "/dashboard" });
  assert.deepEqual(harness.fetches, []);
  const pushSection = serviceWorkerSource.split('self.addEventListener("push"')[1]
    .split('self.addEventListener("fetch"')[0];
  for (const forbidden of ["caches.", "localStorage", "indexedDB", "/api/", "fetch("]) {
    assert.equal(pushSection.includes(forbidden), false);
  }
});

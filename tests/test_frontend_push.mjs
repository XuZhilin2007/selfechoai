import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const testsDirectory = path.dirname(fileURLToPath(import.meta.url));
const frontendPath = path.join(testsDirectory, "..", "app", "static", "app.js");
const frontendSource = readFileSync(frontendPath, "utf8");

function response(body = {}, status = 200) {
  return {
    ok: status >= 200 && status < 300,
    status,
    async json() {
      return body;
    },
  };
}

function makeSubscription(endpoint, applicationServerKey, behavior = {}) {
  return {
    endpoint,
    options: { applicationServerKey },
    async unsubscribe() {
      behavior.onUnsubscribe?.(endpoint);
      const result = behavior.unsubscribe?.() ?? true;
      if (result && behavior.clear) behavior.clear(this);
      return result;
    },
    toJSON() {
      if (behavior.malformed) return { endpoint, keys: {} };
      return {
        endpoint,
        keys: {
          p256dh: "synthetic-p256dh",
          auth: "synthetic-auth",
        },
      };
    },
  };
}

function createHarness({
  secureContext = true,
  notificationSupported = true,
  pushManagerSupported = true,
  serviceWorkerSupported = true,
  permission = "default",
  permissionResult = "granted",
  existingSubscription = null,
  unsubscribeResults = [],
  fetchImpl = null,
} = {}) {
  const calls = {
    config: 0,
    permission: 0,
    subscribe: 0,
    sync: [],
    revoke: 0,
    logout: 0,
    unsubscribe: 0,
  };
  const events = [];
  const storage = new Map();
  let currentSubscription = existingSubscription;
  let subscriptionSequence = 0;
  let currentUserId = 1;

  const nextUnsubscribeResult = () => (
    unsubscribeResults.length ? unsubscribeResults.shift() : true
  );
  const bindSubscriptionBehavior = (subscription) => {
    if (!subscription) return null;
    return makeSubscription(
      subscription.endpoint,
      subscription.options.applicationServerKey,
      {
        malformed: subscription.toJSON?.().keys?.p256dh === undefined,
        unsubscribe: nextUnsubscribeResult,
        clear(candidate) {
          if (currentSubscription?.endpoint === candidate.endpoint) {
            currentSubscription = null;
          }
        },
        onUnsubscribe() {
          calls.unsubscribe += 1;
          events.push("browser-unsubscribe");
        },
      },
    );
  };
  currentSubscription = bindSubscriptionBehavior(currentSubscription);

  const pushManager = {
    async getSubscription() {
      return currentSubscription;
    },
    async subscribe(options) {
      calls.subscribe += 1;
      subscriptionSequence += 1;
      currentSubscription = makeSubscription(
        `https://push.example.test/fresh-${subscriptionSequence}`,
        new Uint8Array(options.applicationServerKey),
        {
          unsubscribe: nextUnsubscribeResult,
          clear(candidate) {
            if (currentSubscription?.endpoint === candidate.endpoint) {
              currentSubscription = null;
            }
          },
          onUnsubscribe() {
            calls.unsubscribe += 1;
            events.push("browser-unsubscribe");
          },
        },
      );
      return currentSubscription;
    },
  };
  const registration = { pushManager };
  const serviceWorker = serviceWorkerSupported
    ? {
        ready: Promise.resolve(registration),
        async getRegistration() {
          return registration;
        },
        register() {
          return Promise.resolve(registration);
        },
      }
    : undefined;
  const notification = {
    permission,
    async requestPermission() {
      calls.permission += 1;
      notification.permission = permissionResult;
      return permissionResult;
    },
  };

  const defaultFetch = async (requestPath, options = {}) => {
    if (requestPath === "/api/push/config") {
      calls.config += 1;
      return response({ available: true, vapid_public_key: "AQID" });
    }
    if (requestPath === "/api/push/subscriptions" && options.method === "PUT") {
      calls.sync.push(JSON.parse(options.body));
      return response({ id: calls.sync.length, status: "active" });
    }
    if (
      requestPath === "/api/push/subscriptions/current" &&
      options.method === "DELETE"
    ) {
      calls.revoke += 1;
      events.push("backend-revoke");
      return response({ id: 1, status: "revoked" });
    }
    if (requestPath === "/api/auth/logout" && options.method === "POST") {
      calls.logout += 1;
      events.push("logout");
      return response({}, 204);
    }
    throw new Error(`Unexpected request: ${requestPath}`);
  };

  const genericElement = {
    addEventListener() {},
    classList: { add() {}, contains() { return false; }, remove() {} },
    dataset: {},
    disabled: false,
    focus() {},
    hidden: false,
    innerHTML: "",
    isConnected: true,
    querySelector() { return genericElement; },
    querySelectorAll() { return []; },
    remove() {},
    removeAttribute() {},
    setAttribute() {},
    textContent: "",
    value: "",
  };
  const appElement = {
    focus() {},
    innerHTML: "",
    insertAdjacentHTML() {},
    replaceChildren() {},
  };
  const document = {
    body: { classList: { remove() {} } },
    cookie: "selfecho_csrf=synthetic-csrf",
    addEventListener() {},
    querySelector(selector) {
      if (selector === "#app") return appElement;
      if (selector === "#primary-navigation") return genericElement;
      return genericElement;
    },
    querySelectorAll() { return []; },
  };
  const window = {
    addEventListener() {},
    atob(value) { return Buffer.from(value, "base64").toString("binary"); },
    clearTimeout,
    isSecureContext: secureContext,
    location: {
      origin: "https://selfecho.example",
      pathname: "/account",
      search: "",
    },
    scrollTo() {},
    setTimeout,
  };
  if (notificationSupported) window.Notification = notification;
  if (pushManagerSupported) window.PushManager = function PushManager() {};
  const navigator = serviceWorkerSupported ? { serviceWorker } : {};
  const localStorage = {
    getItem(key) { return storage.get(key) ?? null; },
    removeItem(key) { storage.delete(key); },
    setItem(key, value) { storage.set(key, value); },
  };
  const context = vm.createContext({
    AbortController,
    console: { warn() {}, error() {}, log() {} },
    document,
    fetch: fetchImpl || defaultFetch,
    Headers,
    history: { pushState() {}, replaceState() {} },
    localStorage,
    navigator,
    sessionStorage: { getItem() { return null; }, removeItem() {}, setItem() {} },
    URL,
    URLSearchParams,
    window,
  });
  const instrumented = frontendSource.replace(
    /^void initializeAuthentication\(\);$/m,
    "globalThis.__startupPromise = Promise.resolve();",
  ) + `
    globalThis.__setAuthentication = setAuthentication;
    globalThis.__initializePush = initializePushForAuthenticatedUser;
    globalThis.__enablePush = enableDeviceNotifications;
    globalThis.__disablePush = disableDeviceNotifications;
    globalThis.__performLogout = performLogout;
    globalThis.__resetPush = resetPushDeviceState;
    globalThis.__getPushState = () => ({ ...pushDeviceState });
    globalThis.__waitForPush = async () => {
      while (pushOperationPromise) await pushOperationPromise;
    };
  `;
  vm.runInContext(instrumented, context, { filename: frontendPath });

  return {
    calls,
    context,
    events,
    notification,
    storage,
    currentSubscription: () => currentSubscription,
    setServerUser(userId) { currentUserId = userId; },
    serverUser: () => currentUserId,
  };
}

function user(id) {
  return {
    id,
    email: `user-${id}@example.com`,
    display_name: `User ${id}`,
    timezone: "Asia/Shanghai",
    default_reminder_time: "09:00",
  };
}

function existingSubscription(key = Uint8Array.from([1, 2, 3]), options = {}) {
  return makeSubscription(
    options.endpoint || "https://push.example.test/existing",
    key,
    { malformed: options.malformed },
  );
}

for (const unsupported of [
  { serviceWorkerSupported: false },
  { notificationSupported: false },
  { pushManagerSupported: false },
  { secureContext: false },
]) {
  test(`unsupported capability degrades safely: ${JSON.stringify(unsupported)}`, async () => {
    const harness = createHarness(unsupported);
    harness.context.__setAuthentication("authenticated", user(1));
    await harness.context.__waitForPush();

    assert.equal(harness.context.__getPushState().status, "unsupported");
    assert.equal(harness.calls.config, 0);
    assert.equal(harness.calls.permission, 0);
    assert.equal(harness.calls.subscribe, 0);
  });
}

test("server-disabled Push remains an in-app-only unavailable state", async () => {
  const harness = createHarness({
    fetchImpl: async (requestPath) => {
      assert.equal(requestPath, "/api/push/config");
      harness.calls.config += 1;
      return response({ available: false, vapid_public_key: null });
    },
  });
  harness.context.__setAuthentication("authenticated", user(1));
  await harness.context.__waitForPush();

  assert.equal(harness.context.__getPushState().status, "unavailable");
  assert.equal(harness.calls.permission, 0);
  assert.equal(harness.calls.subscribe, 0);
});

test("permission prompt occurs only after explicit user action", async () => {
  const harness = createHarness({ permission: "default" });
  harness.context.__setAuthentication("authenticated", user(1));
  await harness.context.__waitForPush();

  assert.equal(harness.context.__getPushState().status, "not_enabled");
  assert.equal(harness.calls.permission, 0);
  assert.equal(harness.calls.subscribe, 0);

  await harness.context.__enablePush();
  assert.equal(harness.calls.permission, 1);
  assert.equal(harness.calls.subscribe, 1);
  assert.equal(harness.calls.sync.length, 1);
  assert.equal(harness.context.__getPushState().status, "enabled");
});

test("denied permission is not prompted repeatedly", async () => {
  const harness = createHarness({ permission: "denied" });
  harness.context.__setAuthentication("authenticated", user(1));
  await harness.context.__waitForPush();
  await harness.context.__enablePush();

  assert.equal(harness.context.__getPushState().status, "denied");
  assert.equal(harness.calls.permission, 0);
  assert.equal(harness.calls.subscribe, 0);
});

test("granted existing subscription syncs without resubscribe", async () => {
  const harness = createHarness({
    permission: "granted",
    existingSubscription: existingSubscription(),
  });
  harness.context.__setAuthentication("authenticated", user(1));
  await harness.context.__waitForPush();

  assert.equal(harness.calls.sync.length, 1);
  assert.equal(harness.calls.subscribe, 0);
  assert.equal(harness.context.__getPushState().status, "enabled");
});

test("granted permission without a subscription waits for explicit reconnect", async () => {
  const harness = createHarness({ permission: "granted" });
  harness.context.__setAuthentication("authenticated", user(1));
  await harness.context.__waitForPush();

  assert.equal(harness.calls.subscribe, 0);
  assert.equal(harness.context.__getPushState().status, "reconnect_required");
});

test("malformed browser subscription fails without leaking or persisting values", async () => {
  const harness = createHarness({
    permission: "granted",
    existingSubscription: existingSubscription(undefined, { malformed: true }),
  });
  harness.context.__setAuthentication("authenticated", user(1));
  await harness.context.__waitForPush();

  assert.equal(harness.calls.sync.length, 0);
  assert.equal(harness.context.__getPushState().status, "reconnect_required");
  assert.equal(JSON.stringify([...harness.storage]), "[]");
});

test("VAPID rotation never syncs the old subscription and replaces it explicitly", async () => {
  const harness = createHarness({
    permission: "granted",
    existingSubscription: existingSubscription(Uint8Array.from([9, 9, 9])),
  });
  harness.context.__setAuthentication("authenticated", user(1));
  await harness.context.__waitForPush();

  assert.equal(harness.calls.sync.length, 0);
  assert.equal(harness.calls.unsubscribe, 0);
  assert.equal(harness.context.__getPushState().status, "reconnect_required");

  await harness.context.__enablePush();
  assert.equal(harness.calls.unsubscribe, 1);
  assert.equal(harness.calls.subscribe, 1);
  assert.equal(harness.calls.sync.length, 1);
  assert.equal(harness.context.__getPushState().status, "enabled");
});

test("backend sync failure is recoverable without a second browser subscription", async () => {
  let syncAttempts = 0;
  const harness = createHarness({
    permission: "granted",
    fetchImpl: async (requestPath, options = {}) => {
      if (requestPath === "/api/push/config") {
        return response({ available: true, vapid_public_key: "AQID" });
      }
      if (requestPath === "/api/push/subscriptions" && options.method === "PUT") {
        syncAttempts += 1;
        return syncAttempts === 1
          ? response({ detail: "temporarily unavailable" }, 503)
          : response({ id: 1, status: "active" });
      }
      throw new Error(`Unexpected request: ${requestPath}`);
    },
  });
  harness.context.__setAuthentication("authenticated", user(1));
  await harness.context.__waitForPush();
  await harness.context.__enablePush();
  assert.equal(harness.context.__getPushState().status, "connection_failed");

  await harness.context.__enablePush();
  assert.equal(syncAttempts, 2);
  assert.equal(harness.calls.subscribe, 1);
  assert.equal(harness.context.__getPushState().status, "enabled");
});

test("disable revokes backend, unsubscribes browser, and persists non-secret intent", async () => {
  const harness = createHarness({
    permission: "granted",
    existingSubscription: existingSubscription(),
  });
  harness.context.__setAuthentication("authenticated", user(1));
  await harness.context.__waitForPush();
  await harness.context.__disablePush();

  assert.equal(harness.calls.revoke, 1);
  assert.equal(harness.calls.unsubscribe, 1);
  assert.equal(harness.currentSubscription(), null);
  assert.equal(harness.context.__getPushState().status, "disabled");
  const serializedStorage = JSON.stringify([...harness.storage]);
  assert.match(serializedStorage, /device-notifications-disabled/);
  assert.doesNotMatch(serializedStorage, /push\.example|p256dh|synthetic-auth/);
});

test("browser cleanup failure leaves server disabled and supports a bounded retry", async () => {
  const harness = createHarness({
    permission: "granted",
    existingSubscription: existingSubscription(),
    unsubscribeResults: [false, true],
  });
  harness.context.__setAuthentication("authenticated", user(1));
  await harness.context.__waitForPush();

  await harness.context.__disablePush();
  assert.equal(harness.context.__getPushState().status, "cleanup_required");
  assert.equal(harness.calls.revoke, 1);

  await harness.context.__disablePush();
  assert.equal(harness.context.__getPushState().status, "disabled");
  assert.equal(harness.calls.revoke, 2);
  assert.equal(harness.calls.unsubscribe, 2);
});

test("backend disable failure reconciles on refresh without re-subscribing", async () => {
  let revokeAttempts = 0;
  const harness = createHarness({
    permission: "granted",
    existingSubscription: existingSubscription(),
    fetchImpl: async (requestPath, options = {}) => {
      if (requestPath === "/api/push/config") {
        return response({ available: true, vapid_public_key: "AQID" });
      }
      if (requestPath === "/api/push/subscriptions" && options.method === "PUT") {
        return response({ id: 1, status: "active" });
      }
      if (requestPath === "/api/push/subscriptions/current") {
        revokeAttempts += 1;
        return revokeAttempts === 1
          ? response({ detail: "offline" }, 503)
          : response({ id: 1, status: "revoked" });
      }
      throw new Error(`Unexpected request: ${requestPath}`);
    },
  });
  harness.context.__setAuthentication("authenticated", user(1));
  await harness.context.__waitForPush();
  await harness.context.__disablePush();
  assert.equal(harness.context.__getPushState().status, "cleanup_required");
  assert.equal(harness.currentSubscription(), null);

  harness.context.__resetPush(1);
  await harness.context.__initializePush();
  assert.equal(harness.context.__getPushState().status, "disabled");
  assert.equal(revokeAttempts, 2);
  assert.equal(harness.calls.subscribe, 0);
});

test("normal logout cleanup runs backend revoke, browser unsubscribe, then logout", async () => {
  const harness = createHarness({
    permission: "granted",
    existingSubscription: existingSubscription(),
  });
  harness.context.__setAuthentication("authenticated", user(1));
  await harness.context.__waitForPush();
  harness.events.length = 0;

  await harness.context.__performLogout();

  assert.deepEqual(harness.events, ["backend-revoke", "browser-unsubscribe", "logout"]);
  assert.equal(harness.context.__getPushState().userId, null);
});

test("cleanup failures never prevent the authoritative logout request", async () => {
  const harness = createHarness({
    permission: "granted",
    existingSubscription: existingSubscription(),
    unsubscribeResults: [false],
    fetchImpl: async (requestPath, options = {}) => {
      if (requestPath === "/api/push/config") {
        return response({ available: true, vapid_public_key: "AQID" });
      }
      if (requestPath === "/api/push/subscriptions" && options.method === "PUT") {
        return response({ id: 1, status: "active" });
      }
      if (requestPath === "/api/push/subscriptions/current") {
        return response({ detail: "offline" }, 503);
      }
      if (requestPath === "/api/auth/logout") {
        harness.calls.logout += 1;
        return response({}, 204);
      }
      throw new Error(`Unexpected request: ${requestPath}`);
    },
  });
  harness.context.__setAuthentication("authenticated", user(1));
  await harness.context.__waitForPush();

  await harness.context.__performLogout();

  assert.equal(harness.calls.logout, 1);
  assert.equal(harness.context.__getPushState().userId, null);
});

test("User A to User B recovers one stale endpoint without rewriting A ownership", async () => {
  const owners = new Map();
  let serverUser = 1;
  let oldEndpointCollisionAttempts = 0;
  const harness = createHarness({
    permission: "granted",
    existingSubscription: existingSubscription(),
    unsubscribeResults: [false, true],
    fetchImpl: async (requestPath, options = {}) => {
      if (requestPath === "/api/push/config") {
        return response({ available: true, vapid_public_key: "AQID" });
      }
      if (requestPath === "/api/push/subscriptions" && options.method === "PUT") {
        const endpoint = JSON.parse(options.body).endpoint;
        const owner = owners.get(endpoint);
        if (owner !== undefined && owner !== serverUser) {
          oldEndpointCollisionAttempts += 1;
          return response({ detail: "invalid push subscription" }, 422);
        }
        owners.set(endpoint, serverUser);
        return response({ id: owners.size, status: "active" });
      }
      if (requestPath === "/api/push/subscriptions/current") {
        return response({ id: 1, status: "revoked" });
      }
      if (requestPath === "/api/auth/logout") return response({}, 204);
      throw new Error(`Unexpected request: ${requestPath}`);
    },
  });
  harness.context.__setAuthentication("authenticated", user(1));
  await harness.context.__waitForPush();
  const oldEndpoint = harness.currentSubscription().endpoint;
  assert.equal(owners.get(oldEndpoint), 1);

  await harness.context.__performLogout();
  assert.notEqual(harness.currentSubscription(), null);
  serverUser = 2;
  harness.context.__setAuthentication("authenticated", user(2));
  await harness.context.__waitForPush();

  assert.equal(oldEndpointCollisionAttempts, 1);
  assert.equal(harness.calls.subscribe, 1);
  assert.equal(harness.context.__getPushState().status, "enabled");
  assert.equal(owners.get(oldEndpoint), 1);
  assert.equal(owners.get(harness.currentSubscription().endpoint), 2);
});

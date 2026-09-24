const appElement = document.querySelector("#app");
const primaryNavigation = document.querySelector("#primary-navigation");
let pollTimer = null;
let authRedirectTimer = null;
let authenticationInitializationPromise = null;
let pushOperationPromise = null;
let pushStateGeneration = 0;
let notificationOnboardingHasReminder = false;
let activeCaptureController = null;
let suppressedDashboardClickItemId = null;
// Tracks the touch/pen press currently held on a selectable item so the
// native long-press link menu can be suppressed across dashboard re-renders.
let activeSelectableTouchPress = null;
// Short-lived record of the selectable item whose touch/pen press just ended,
// covering user agents that dispatch contextmenu after the pointer stream.
let recentSelectableTouchPress = null;

const dashboardSelection = {
  active: false,
  status: null,
  page: null,
  ids: new Set(),
  visibleIds: [],
  generation: 0,
};

const AUTH_STATES = Object.freeze({
  LOADING: "loading",
  AUTHENTICATED: "authenticated",
  UNAUTHENTICATED: "unauthenticated",
  NETWORK_ERROR: "network_error",
});
const CSRF_COOKIE_NAME = "selfecho_csrf";
const CAPTURE_DRAFT_KEY = "selfecho.capture-draft";
const CAPTURE_AUTOSAVE_DELAY_MS = 700;
const CAPTURE_STATUS_POLL_DELAY_MS = 800;
const VOICE_CANCEL_DISTANCE_PX = 72;
const VOICE_MAX_RECORDING_MS = 60_000;
const VOICE_COMPLETION_HIGHLIGHT_MS = 700;
const VOICE_COMPLETION_VIBRATION_MS = 25;
const ITEM_LONG_PRESS_DELAY_MS = 520;
const ITEM_LONG_PRESS_MOVE_THRESHOLD_PX = 12;
// Native link contextmenu may be dispatched after pointerup/pointercancel;
// this grace keeps suppression alive briefly after a stationary touch/pen
// selectable press ends. Scroll-cancelled gestures never arm it.
const SELECTABLE_TOUCH_MENU_GRACE_MS = 500;
const MAX_BULK_LIFECYCLE_ITEMS = 100;
const CAPTURE_BROWSER_STATES = Object.freeze({
  BOOTSTRAPPING: "BOOTSTRAPPING",
  OPEN: "OPEN",
  DIRTY: "DIRTY",
  REQUESTING_MIC: "REQUESTING_MIC",
  RECORDING: "RECORDING",
  CANCEL_ARMED: "CANCEL_ARMED",
  FINALIZING: "FINALIZING",
  UPLOADING: "UPLOADING",
  POLLING: "POLLING",
  SAVING: "SAVING",
  REVISION_CONFLICT: "REVISION_CONFLICT",
});
const STARTUP_AUTH_RETRY_DELAYS_MS = Object.freeze([300, 700]);
const NOTIFICATION_DISMISSAL_KEY = "selfecho.notification-onboarding-dismissed";
const NOTIFICATION_DISABLED_KEY = "selfecho.device-notifications-disabled";
const PUSH_CLEANUP_TIMEOUT_MS = 2500;
const authentication = {
  status: AUTH_STATES.LOADING,
  user: null,
};
const pushDeviceState = {
  userId: null,
  initialized: false,
  status: "checking",
  config: null,
  lastErrorCode: null,
};

class ApiError extends Error {
  constructor(message, status) {
    super(message);
    this.name = "ApiError";
    this.status = status;
  }
}

class NetworkError extends Error {
  constructor() {
    super("无法连接服务器，请检查网络后重试。");
    this.name = "NetworkError";
  }
}

const labels = {
  high: "高",
  medium: "中",
  low: "低",
  unknown: "未知",
  active: "进行中",
  completed: "已完成",
  trash: "回收站",
  pending: "等待整理",
  processing: "正在整理",
  succeeded: "已整理",
  failed: "整理失败",
  needs_confirmation: "待设置时间",
  scheduled: "已设置",
  due: "已到时间",
  cancelled: "已关闭",
};

const CAPTURE_FAILURE_DISPLAYS = Object.freeze({
  configuration: {
    title: "当前暂时不可用",
    message: "这条记录暂时没有处理完成，可以稍后重试。",
  },
  network: {
    title: "网络连接失败",
    message: "当前无法完成处理，请检查网络后重试。",
  },
  api: {
    title: "服务暂时出错",
    message: "这条记录暂时没有处理完成，可以稍后重试。",
  },
  invalid_output: {
    title: "没有整理成功",
    message: "这条记录没有整理完成，可以重新尝试。",
  },
  internal: {
    title: "暂时无法整理",
    message: "这条记录处理失败，可以重新尝试；如果问题持续出现，请稍后再试。",
  },
});
const CAPTURE_FAILURE_FALLBACK = Object.freeze({
  title: "暂时没有整理成功",
  message: "这条记录暂时没有处理完成，可以稍后重试。",
});

function captureFailureDisplay(input) {
  return CAPTURE_FAILURE_DISPLAYS[input?.failure_type] || CAPTURE_FAILURE_FALLBACK;
}

const lifecycleViews = {
  active: {
    label: "当前",
    heading: "现在值得关注的事",
    empty: "还没有需要关注的事项。",
  },
  completed: {
    label: "历史",
    heading: "已经完成的事",
    empty: "还没有已完成事项。",
  },
  trash: {
    label: "回收站",
    heading: "回收站里的事",
    empty: "回收站是空的。",
  },
};

const itemTypeLabels = {
  study: "学习",
  purchase_decision: "购买决策",
  decision: "决策",
  project: "项目",
  planning: "规划",
  idea: "想法",
  note: "记录",
  other: "事项",
};

const supplementalLabels = {
  options: "候选",
  candidates: "候选",
  timing_constraint: "时间考虑",
  constraint: "限制",
  constraints: "限制",
  concern: "顾虑",
  concerns: "顾虑",
  risk: "风险",
  risks: "风险",
  progress: "当前进展",
  chapter: "重点章节",
  goal: "目标",
  budget: "预算",
  comparison_notes: "对比考虑",
  use_case: "使用场景",
  preference: "偏好",
  preferences: "偏好",
  intended_use: "用途",
  source: "来源",
  date_context: "时间信息",
  daily_target: "每日目标",
  study_cycle: "复习周期",
  long_context_note: "补充说明",
  reason_paused: "暂停原因",
};

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

async function api(path, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  const headers = new Headers(options.headers || {});
  headers.set("Content-Type", "application/json");
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    const csrfToken = readCookie(CSRF_COOKIE_NAME);
    if (csrfToken) headers.set("X-CSRF-Token", csrfToken);
  }

  let response;
  try {
    response = await fetch(path, {
      ...options,
      method,
      credentials: "same-origin",
      headers,
    });
  } catch (_) {
    throw new NetworkError();
  }
  if (!response.ok) {
    let message = "请求失败，请稍后重试。";
    try {
      const body = await response.json();
      if (typeof body.detail === "string") message = body.detail;
      else if (Array.isArray(body.detail)) message = "提交内容不符合要求，请检查后重试。";
    } catch (_) {
      // Keep the user-facing fallback and never expose an HTML error page.
    }
    const error = new ApiError(message, response.status);
    if (response.status === 401 && !path.startsWith("/api/auth/")) {
      scheduleSessionExpiredRedirect();
    }
    throw error;
  }
  return response.status === 204 ? null : response.json();
}

async function rawApi(path, options = {}) {
  const method = (options.method || "GET").toUpperCase();
  const headers = new Headers(options.headers || {});
  if (!["GET", "HEAD", "OPTIONS"].includes(method)) {
    const csrfToken = readCookie(CSRF_COOKIE_NAME);
    if (csrfToken) headers.set("X-CSRF-Token", csrfToken);
  }
  let response;
  try {
    response = await fetch(path, {
      ...options,
      method,
      credentials: "same-origin",
      headers,
    });
  } catch (_) {
    throw new NetworkError();
  }
  if (!response.ok) {
    let message = "请求失败，请稍后重试。";
    try {
      const body = await response.json();
      if (typeof body.detail === "string") message = body.detail;
      else if (Array.isArray(body.detail)) message = "提交内容不符合要求，请检查后重试。";
    } catch (_) {
      // Never surface an HTML error body.
    }
    const error = new ApiError(message, response.status);
    if (response.status === 401 && !path.startsWith("/api/auth/")) {
      scheduleSessionExpiredRedirect();
    }
    throw error;
  }
  return response.status === 204 ? null : response.json();
}

function readCookie(name) {
  const prefix = `${encodeURIComponent(name)}=`;
  const cookie = document.cookie
    .split(";")
    .map((part) => part.trim())
    .find((part) => part.startsWith(prefix));
  return cookie ? decodeURIComponent(cookie.slice(prefix.length)) : null;
}

function setAuthentication(status, user = authentication.user) {
  if (status === AUTH_STATES.AUTHENTICATED && authRedirectTimer) {
    window.clearTimeout(authRedirectTimer);
    authRedirectTimer = null;
  }
  authentication.status = status;
  authentication.user = user;
  primaryNavigation.hidden = status !== AUTH_STATES.AUTHENTICATED;
  if (status === AUTH_STATES.AUTHENTICATED && user) {
    if (pushDeviceState.userId !== user.id) resetPushDeviceState(user.id);
    void initializePushForAuthenticatedUser();
  } else if (status === AUTH_STATES.UNAUTHENTICATED) {
    resetPushDeviceState(null);
  }
}

function resetPushDeviceState(userId) {
  pushStateGeneration += 1;
  pushOperationPromise = null;
  notificationOnboardingHasReminder = false;
  pushDeviceState.userId = userId;
  pushDeviceState.initialized = false;
  pushDeviceState.status = "checking";
  pushDeviceState.config = null;
  pushDeviceState.lastErrorCode = null;
}

function pushContextIsCurrent(userId, generation) {
  return authentication.status === AUTH_STATES.AUTHENTICATED &&
    authentication.user?.id === userId &&
    pushDeviceState.userId === userId &&
    pushStateGeneration === generation;
}

function systemNotificationsSupported() {
  return window.isSecureContext === true &&
    "Notification" in window &&
    "PushManager" in window &&
    "serviceWorker" in navigator;
}

function updatePushDeviceState(userId, generation, updates) {
  if (!pushContextIsCurrent(userId, generation)) return false;
  Object.assign(pushDeviceState, updates);
  refreshNotificationUi();
  return true;
}

function runPushSingleFlight(operation) {
  if (pushOperationPromise) return pushOperationPromise;
  const running = Promise.resolve().then(operation);
  const tracked = running.finally(() => {
    if (pushOperationPromise === tracked) pushOperationPromise = null;
  });
  pushOperationPromise = tracked;
  return tracked;
}

function pushPreferenceStorageKey(prefix) {
  const userId = authentication.user?.id ?? "anonymous";
  return `${prefix}.${userId}.${window.location.origin}`;
}

function notificationDismissalStorageKey() {
  return pushPreferenceStorageKey(NOTIFICATION_DISMISSAL_KEY);
}

function notificationDisabledStorageKey() {
  return pushPreferenceStorageKey(NOTIFICATION_DISABLED_KEY);
}

function readPushPreference(key) {
  try {
    return localStorage.getItem(key) === "true";
  } catch (_) {
    return false;
  }
}

function writePushPreference(key, value) {
  try {
    if (value) localStorage.setItem(key, "true");
    else localStorage.removeItem(key);
  } catch (_) {
    // Notification preferences remain optional when storage is unavailable.
  }
}

function notificationOnboardingDismissed() {
  return readPushPreference(notificationDismissalStorageKey());
}

function notificationDisableRequested() {
  return readPushPreference(notificationDisabledStorageKey());
}

function browserSubscriptionPayload(subscription) {
  const serialized = subscription?.toJSON?.();
  if (
    typeof serialized?.endpoint !== "string" || !serialized.endpoint ||
    typeof serialized?.keys?.p256dh !== "string" || !serialized.keys.p256dh ||
    typeof serialized?.keys?.auth !== "string" || !serialized.keys.auth
  ) {
    throw new Error("invalid browser push subscription");
  }
  return {
    endpoint: serialized.endpoint,
    keys: {
      p256dh: serialized.keys.p256dh,
      auth: serialized.keys.auth,
    },
  };
}

async function syncBrowserSubscription(subscription, userId, generation) {
  if (!pushContextIsCurrent(userId, generation)) return null;
  return api("/api/push/subscriptions", {
    method: "PUT",
    body: JSON.stringify(browserSubscriptionPayload(subscription)),
  });
}

function vapidPublicKeyBytes(value) {
  if (typeof value !== "string" || !value || value.length > 256) {
    throw new Error("invalid VAPID public key");
  }
  const padding = "=".repeat((4 - (value.length % 4)) % 4);
  const base64 = (value + padding).replaceAll("-", "+").replaceAll("_", "/");
  const decoded = window.atob(base64);
  return Uint8Array.from(decoded, (character) => character.charCodeAt(0));
}

function pushSubscriptionUsesVapidKey(subscription, expectedKey) {
  const applicationServerKey = subscription?.options?.applicationServerKey;
  if (!applicationServerKey) return false;
  let actualKey;
  try {
    actualKey = applicationServerKey instanceof Uint8Array
      ? applicationServerKey
      : new Uint8Array(applicationServerKey);
  } catch (_) {
    return false;
  }
  return actualKey.length === expectedKey.length &&
    actualKey.every((value, index) => value === expectedKey[index]);
}

function pushConnectionFailureCode(stage, error) {
  const errorName = typeof error?.name === "string" &&
    /^[A-Za-z][A-Za-z0-9]{0,31}$/.test(error.name)
    ? error.name
    : "UnknownError";
  return `${stage}:${errorName}`;
}

async function unsubscribeBrowserSubscription(pushManager, subscription) {
  try {
    await subscription.unsubscribe();
    const remaining = await pushManager.getSubscription();
    return remaining === null;
  } catch (_) {
    return false;
  }
}

async function replaceAndSyncSubscriptionOnce(
  registration,
  subscription,
  applicationServerKey,
  userId,
  generation,
) {
  const cleared = await unsubscribeBrowserSubscription(
    registration.pushManager,
    subscription,
  );
  if (!cleared || !pushContextIsCurrent(userId, generation)) {
    const error = new Error("stale subscription remains");
    error.name = "InvalidStateError";
    throw error;
  }
  const replacement = await registration.pushManager.subscribe({
    userVisibleOnly: true,
    applicationServerKey,
  });
  if (!pushContextIsCurrent(userId, generation)) return null;
  await syncBrowserSubscription(replacement, userId, generation);
  return replacement;
}

async function syncWithAccountSwitchRecovery(
  registration,
  subscription,
  applicationServerKey,
  userId,
  generation,
) {
  try {
    await syncBrowserSubscription(subscription, userId, generation);
    return subscription;
  } catch (error) {
    if (!(error instanceof ApiError) || error.status !== 422) throw error;
  }
  return replaceAndSyncSubscriptionOnce(
    registration,
    subscription,
    applicationServerKey,
    userId,
    generation,
  );
}

async function apiWithTimeout(path, options, timeoutMs = PUSH_CLEANUP_TIMEOUT_MS) {
  const controller = new AbortController();
  const timer = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    return await api(path, { ...options, signal: controller.signal });
  } finally {
    window.clearTimeout(timer);
  }
}

async function promiseWithTimeout(promise, timeoutMs = PUSH_CLEANUP_TIMEOUT_MS) {
  let timer;
  const timeout = new Promise((_, reject) => {
    timer = window.setTimeout(() => {
      const error = new Error("browser push operation timed out");
      error.name = "TimeoutError";
      reject(error);
    }, timeoutMs);
  });
  try {
    return await Promise.race([promise, timeout]);
  } finally {
    window.clearTimeout(timer);
  }
}

async function revokeCurrentDeviceOnBackend() {
  try {
    await apiWithTimeout("/api/push/subscriptions/current", { method: "DELETE" });
    return true;
  } catch (error) {
    return error instanceof ApiError && error.status === 404;
  }
}

async function browserPushManagerForCleanup(userId, generation) {
  if (!("serviceWorker" in navigator) || !("PushManager" in window)) return null;
  try {
    const registration = typeof navigator.serviceWorker.getRegistration === "function"
      ? await promiseWithTimeout(navigator.serviceWorker.getRegistration("/"))
      : await promiseWithTimeout(navigator.serviceWorker.ready);
    if (!pushContextIsCurrent(userId, generation)) return null;
    return registration?.pushManager ?? null;
  } catch (_) {
    return null;
  }
}

async function unsubscribeCurrentBrowserDevice(userId, generation) {
  const pushManager = await browserPushManagerForCleanup(userId, generation);
  if (!pushManager) return true;
  try {
    const subscription = await promiseWithTimeout(pushManager.getSubscription());
    if (!pushContextIsCurrent(userId, generation)) return false;
    if (!subscription) return true;
    return await promiseWithTimeout(
      unsubscribeBrowserSubscription(pushManager, subscription),
    );
  } catch (_) {
    return false;
  }
}

async function performDisableDeviceNotifications(userId, generation) {
  const backendRevoked = await revokeCurrentDeviceOnBackend();
  const browserUnsubscribed = await unsubscribeCurrentBrowserDevice(
    userId,
    generation,
  );
  updatePushDeviceState(userId, generation, {
    initialized: true,
    status: backendRevoked && browserUnsubscribed ? "disabled" : "cleanup_required",
    lastErrorCode: backendRevoked && browserUnsubscribed
      ? null
      : `disable:${backendRevoked ? "browser" : browserUnsubscribed ? "backend" : "both"}`,
  });
  return pushDeviceState;
}

async function disableDeviceNotifications() {
  if (pushOperationPromise) await pushOperationPromise;
  if (authentication.status !== AUTH_STATES.AUTHENTICATED || !authentication.user) {
    return pushDeviceState;
  }
  const userId = authentication.user.id;
  writePushPreference(notificationDisabledStorageKey(), true);
  return runPushSingleFlight(async () => {
    const generation = pushStateGeneration;
    updatePushDeviceState(userId, generation, {
      initialized: false,
      status: "disabling",
      lastErrorCode: null,
    });
    return performDisableDeviceNotifications(userId, generation);
  });
}

async function initializePushForAuthenticatedUser() {
  if (authentication.status !== AUTH_STATES.AUTHENTICATED || !authentication.user) {
    return pushDeviceState;
  }
  const userId = authentication.user.id;
  if (pushDeviceState.userId !== userId) resetPushDeviceState(userId);
  if (pushDeviceState.initialized) return pushDeviceState;

  return runPushSingleFlight(async () => {
    const generation = pushStateGeneration;
    if (notificationDisableRequested()) {
      return performDisableDeviceNotifications(userId, generation);
    }
    if (!systemNotificationsSupported()) {
      updatePushDeviceState(userId, generation, {
        initialized: true,
        status: "unsupported",
      });
      return pushDeviceState;
    }
    if (window.Notification.permission === "denied") {
      updatePushDeviceState(userId, generation, {
        initialized: true,
        status: "denied",
      });
      return pushDeviceState;
    }
    try {
      const config = await api("/api/push/config");
      if (!pushContextIsCurrent(userId, generation)) return pushDeviceState;
      if (!config.available || !config.vapid_public_key) {
        updatePushDeviceState(userId, generation, {
          initialized: true,
          status: "unavailable",
          config,
        });
        return pushDeviceState;
      }
      if (window.Notification.permission !== "granted") {
        updatePushDeviceState(userId, generation, {
          initialized: true,
          status: "not_enabled",
          config,
        });
        return pushDeviceState;
      }
      const registration = await navigator.serviceWorker.ready;
      if (!pushContextIsCurrent(userId, generation)) return pushDeviceState;
      const subscription = await registration.pushManager.getSubscription();
      if (!pushContextIsCurrent(userId, generation)) return pushDeviceState;
      if (!subscription) {
        updatePushDeviceState(userId, generation, {
          initialized: true,
          status: "reconnect_required",
          config,
        });
        return pushDeviceState;
      }
      const applicationServerKey = vapidPublicKeyBytes(config.vapid_public_key);
      if (!pushSubscriptionUsesVapidKey(subscription, applicationServerKey)) {
        updatePushDeviceState(userId, generation, {
          initialized: true,
          status: "reconnect_required",
          config,
        });
        return pushDeviceState;
      }
      await syncWithAccountSwitchRecovery(
        registration,
        subscription,
        applicationServerKey,
        userId,
        generation,
      );
      updatePushDeviceState(userId, generation, {
        initialized: true,
        status: "enabled",
        config,
        lastErrorCode: null,
      });
    } catch (error) {
      updatePushDeviceState(userId, generation, {
        initialized: true,
        status: "reconnect_required",
        lastErrorCode: pushConnectionFailureCode("initialize", error),
      });
    }
    return pushDeviceState;
  });
}

async function enableDeviceNotifications() {
  if (pushOperationPromise) await pushOperationPromise;
  if (authentication.status !== AUTH_STATES.AUTHENTICATED || !authentication.user) {
    return pushDeviceState;
  }
  const userId = authentication.user.id;
  writePushPreference(notificationDisabledStorageKey(), false);
  return runPushSingleFlight(async () => {
    const generation = pushStateGeneration;
    if (!systemNotificationsSupported()) {
      updatePushDeviceState(userId, generation, {
        initialized: true,
        status: "unsupported",
      });
      return pushDeviceState;
    }
    if (window.Notification.permission === "denied") {
      updatePushDeviceState(userId, generation, {
        initialized: true,
        status: "denied",
      });
      return pushDeviceState;
    }
    updatePushDeviceState(userId, generation, {
      initialized: false,
      status: "connecting",
      lastErrorCode: null,
    });
    let connectionStage = "config";
    try {
      const config = pushDeviceState.config?.available
        ? pushDeviceState.config
        : await api("/api/push/config");
      if (!pushContextIsCurrent(userId, generation)) return pushDeviceState;
      if (!config.available || !config.vapid_public_key) {
        updatePushDeviceState(userId, generation, {
          initialized: true,
          status: "unavailable",
          config,
        });
        return pushDeviceState;
      }
      connectionStage = "permission";
      let permission = window.Notification.permission;
      if (permission === "default") {
        permission = await window.Notification.requestPermission();
      }
      if (!pushContextIsCurrent(userId, generation)) return pushDeviceState;
      if (permission !== "granted") {
        updatePushDeviceState(userId, generation, {
          initialized: true,
          status: permission === "denied" ? "denied" : "not_enabled",
          config,
        });
        return pushDeviceState;
      }
      connectionStage = "service_worker";
      const registration = await navigator.serviceWorker.ready;
      if (!pushContextIsCurrent(userId, generation)) return pushDeviceState;
      connectionStage = "get_subscription";
      let subscription = await registration.pushManager.getSubscription();
      if (!pushContextIsCurrent(userId, generation)) return pushDeviceState;
      connectionStage = "vapid_key";
      const applicationServerKey = vapidPublicKeyBytes(config.vapid_public_key);
      if (subscription && !pushSubscriptionUsesVapidKey(subscription, applicationServerKey)) {
        connectionStage = "vapid_unsubscribe";
        const cleared = await unsubscribeBrowserSubscription(
          registration.pushManager,
          subscription,
        );
        if (!cleared) {
          const error = new Error("old VAPID subscription remains");
          error.name = "InvalidStateError";
          throw error;
        }
        subscription = null;
      }
      if (!subscription) {
        connectionStage = "subscribe";
        subscription = await registration.pushManager.subscribe({
          userVisibleOnly: true,
          applicationServerKey,
        });
        if (!pushContextIsCurrent(userId, generation)) return pushDeviceState;
      }
      connectionStage = "backend_sync";
      await syncWithAccountSwitchRecovery(
        registration,
        subscription,
        applicationServerKey,
        userId,
        generation,
      );
      if (!pushContextIsCurrent(userId, generation)) return pushDeviceState;
      updatePushDeviceState(userId, generation, {
        initialized: true,
        status: "enabled",
        config,
        lastErrorCode: null,
      });
    } catch (error) {
      updatePushDeviceState(userId, generation, {
        initialized: true,
        status: "connection_failed",
        lastErrorCode: pushConnectionFailureCode(connectionStage, error),
      });
    }
    return pushDeviceState;
  });
}

function dismissNotificationOnboarding() {
  writePushPreference(notificationDismissalStorageKey(), true);
  renderNotificationOnboarding();
}


function renderNotificationOnboarding() {
  const existing = document.querySelector("#notification-onboarding");
  if (existing && typeof existing.remove === "function") existing.remove();
  if (
    !notificationOnboardingHasReminder ||
    pushDeviceState.status !== "not_enabled" ||
    window.Notification?.permission !== "default" ||
    notificationOnboardingDismissed()
  ) {
    return;
  }
  appElement.insertAdjacentHTML("afterbegin", `
    <section id="notification-onboarding" class="notification-onboarding panel">
      <div>
        <p class="eyebrow">此设备通知</p>
        <h2>让 SelfEcho 在需要的时候提醒你</h2>
        <p>通知只用于你创建的提醒。不会发送广告、活动推广或无关通知。</p>
      </div>
      <div class="notification-onboarding-actions">
        <button class="primary-button notification-enable-button" type="button">开启此设备通知</button>
        <button class="text-button notification-dismiss-button" type="button">暂时不用</button>
      </div>
    </section>`);
  document.querySelector(".notification-enable-button").addEventListener(
    "click",
    async (event) => {
      event.currentTarget.disabled = true;
      await enableDeviceNotifications();
      renderNotificationOnboarding();
    },
  );
  document.querySelector(".notification-dismiss-button").addEventListener(
    "click",
    dismissNotificationOnboarding,
  );
}

function renderNotificationDeviceSettings() {
  const status = document.querySelector("#notification-device-status");
  const action = document.querySelector("#notification-device-action");
  if (!status || !action) return;
  const states = {
    checking: ["正在检查此设备…", null],
    connecting: ["正在连接此设备通知…", null],
    disabling: ["正在停用此设备通知…", null],
    unsupported: ["此设备或当前连接不支持系统通知，SelfEcho 内提醒仍然可用。", null],
    unavailable: ["系统通知当前不可用，SelfEcho 内提醒仍然可用。", null],
    denied: ["浏览器已拒绝通知。SelfEcho 内提醒仍然可用；如需开启，请使用浏览器或系统设置。", null],
    enabled: ["此设备通知已经可用。通知只会用于你创建的 Reminder。", "停用此设备通知"],
    disabled: ["此设备通知已停用。SelfEcho 内提醒仍然可用。", "重新开启"],
    not_enabled: ["未开启。SelfEcho 内提醒仍然可用。", "开启此设备通知"],
    reconnect_required: ["需要重新连接此设备通知。SelfEcho 内提醒仍然可用。", "重新连接"],
    connection_failed: ["连接未完成，请稍后重试。SelfEcho 内提醒仍然可用。", "重新连接"],
    cleanup_required: ["服务端已尽力停止发送，但设备清理尚未完全完成。", "重试停用"],
  };
  const [message, actionLabel] = states[pushDeviceState.status] || states.reconnect_required;
  status.textContent = message;
  status.dataset.diagnostic = pushDeviceState.lastErrorCode || "";
  status.dataset.kind = ["connection_failed", "cleanup_required"].includes(
    pushDeviceState.status,
  )
    ? "error"
    : pushDeviceState.status === "enabled"
      ? "success"
      : "";
  action.hidden = !actionLabel;
  action.disabled = ["connecting", "disabling"].includes(pushDeviceState.status);
  if (actionLabel) action.textContent = actionLabel;
}

function refreshNotificationUi() {
  renderNotificationDeviceSettings();
  renderNotificationOnboarding();
}

function reminderIsNotificationEligible(reminder) {
  return reminder && ["needs_confirmation", "scheduled", "due"].includes(
    reminder.status,
  );
}

async function cleanupPushBeforeLogout() {
  if (authentication.status !== AUTH_STATES.AUTHENTICATED || !authentication.user) {
    return;
  }
  const userId = authentication.user.id;
  const generation = pushStateGeneration;
  if (pushOperationPromise) {
    try {
      await promiseWithTimeout(pushOperationPromise);
    } catch (_) {
      // Logout's backend session revocation remains the final safety boundary.
    }
  }
  if (!pushContextIsCurrent(userId, generation)) return;
  await revokeCurrentDeviceOnBackend();
  if (!pushContextIsCurrent(userId, generation)) return;
  await unsubscribeCurrentBrowserDevice(userId, generation);
}

async function performLogout() {
  try {
    await cleanupPushBeforeLogout();
  } catch (_) {
    // Browser cleanup is best effort; server logout remains authoritative.
  }
  try {
    await api("/api/auth/logout", { method: "POST" });
    completeLocalLogout();
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      completeLocalLogout();
      return;
    }
    throw error;
  }
}

function setActiveNavigation() {
  const path = window.location.pathname;
  document.querySelectorAll(".topbar nav a").forEach((link) => {
    const isActive =
      (path === "/capture" || path === "/")
        ? link.getAttribute("href") === "/capture"
        : path.startsWith("/dashboard") || path.startsWith("/items/")
          ? link.getAttribute("href") === "/dashboard"
          : path === "/account"
            ? link.getAttribute("href") === "/account"
            : false;
    if (isActive) link.setAttribute("aria-current", "page");
    else link.removeAttribute("aria-current");
  });
}

async function navigate(path) {
  if (activeCaptureController) {
    const canLeave = await activeCaptureController.prepareNavigation();
    if (!canLeave) return;
  }
  history.pushState({}, "", path);
  renderRoute();
}

function detailRefreshBlocked() {
  const activeControl = document.activeElement;
  const editorHasFocus =
    activeControl instanceof HTMLElement &&
    activeControl.matches(
      "#update-form textarea, #edit-form input, #edit-form textarea, #edit-form select, #reminder-form input",
    );
  const dirtyForm = document.querySelector(
    '#update-form[data-dirty="true"], #edit-form[data-dirty="true"], #reminder-form[data-dirty="true"]',
  );
  const modalOpen = document.querySelector(
    "#edit-dialog[open], #reminder-dialog[open]",
  );
  const pinSaving = document.querySelector('#detail-pin-button[data-saving="true"]');
  return editorHasFocus || Boolean(dirtyForm) || Boolean(modalOpen) || Boolean(pinSaving);
}

function scheduleDetailPoll(itemId, delay = 2500) {
  if (pollTimer) window.clearTimeout(pollTimer);
  pollTimer = window.setTimeout(() => {
    pollTimer = null;
    if (detailRefreshBlocked()) {
      scheduleDetailPoll(itemId);
      return;
    }
    void renderDetail(itemId, { silent: true, automatic: true });
  }, delay);
}

document.addEventListener("click", (event) => {
  const selectableItem = event.target.closest("[data-selectable-item]");
  if (selectableItem) {
    const itemId = Number(selectableItem.dataset.itemId);
    if (suppressedDashboardClickItemId === itemId) {
      event.preventDefault();
      event.stopPropagation();
      suppressedDashboardClickItemId = null;
      return;
    }
    if (dashboardSelection.active) {
      event.preventDefault();
      const focusIntent = createDashboardFocusIntent("item", itemId);
      toggleDashboardSelection(itemId);
      void renderDashboard({ silent: true, focusIntent });
      return;
    }
  }
  const link = event.target.closest("a[data-link]");
  if (!link || link.origin !== window.location.origin) return;
  event.preventDefault();
  void navigate(`${link.pathname}${link.search}`);
});

document.addEventListener("keydown", (event) => {
  if (!dashboardSelection.active || event.key !== " ") return;
  const selectableItem = event.target.closest("[data-selectable-item]");
  if (!selectableItem) return;
  event.preventDefault();
  const itemId = Number(selectableItem.dataset.itemId);
  const focusIntent = createDashboardFocusIntent("item", itemId);
  toggleDashboardSelection(itemId);
  void renderDashboard({ silent: true, focusIntent });
});

function endSelectableTouchPress(pointerId) {
  if (activeSelectableTouchPress?.pointerId !== pointerId) return;
  const itemId = activeSelectableTouchPress.itemId;
  activeSelectableTouchPress = null;
  if (recentSelectableTouchPress !== null) {
    window.clearTimeout(recentSelectableTouchPress.timer);
  }
  const record = { itemId };
  record.timer = window.setTimeout(() => {
    if (recentSelectableTouchPress === record) {
      recentSelectableTouchPress = null;
    }
  }, SELECTABLE_TOUCH_MENU_GRACE_MS);
  recentSelectableTouchPress = record;
}

document.addEventListener("pointerup", (event) => {
  endSelectableTouchPress(event.pointerId);
});

document.addEventListener("pointercancel", (event) => {
  endSelectableTouchPress(event.pointerId);
});

window.addEventListener("popstate", () => {
  const controller = activeCaptureController;
  if (!controller) {
    renderRoute();
    return;
  }
  void controller.prepareNavigation().then((canLeave) => {
    if (canLeave) {
      renderRoute();
      return;
    }
    history.pushState({}, "", controller.location);
  });
});

function formatDate(value) {
  if (!value) return "";
  const dateOnly = value.slice(0, 10);
  const [year, month, day] = dateOnly.split("-");
  return `${year}年${Number(month)}月${Number(day)}日`;
}

function formatTime(value) {
  if (!value) return "";
  return new Intl.DateTimeFormat("zh-CN", {
    dateStyle: "medium",
    timeStyle: "short",
  }).format(new Date(value));
}

function profileTimezone() {
  return authentication.user?.timezone || "UTC";
}

function zonedParts(value, timezoneName = profileTimezone()) {
  const formatter = new Intl.DateTimeFormat("en-CA", {
    timeZone: timezoneName,
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
    fractionalSecondDigits: 3,
    hourCycle: "h23",
  });
  return Object.fromEntries(
    formatter
      .formatToParts(new Date(value))
      .filter((part) => part.type !== "literal")
      .map((part) => [part.type, part.value]),
  );
}

function profileToday() {
  const parts = zonedParts(new Date());
  return `${parts.year}-${parts.month}-${parts.day}`;
}

function addProfileDays(days) {
  const [year, month, day] = profileToday().split("-").map(Number);
  const target = new Date(Date.UTC(year, month - 1, day + days, 12));
  return `${target.getUTCFullYear()}-${String(target.getUTCMonth() + 1).padStart(2, "0")}-${String(target.getUTCDate()).padStart(2, "0")}`;
}

function localDateDistance(localDate) {
  const [todayYear, todayMonth, todayDay] = profileToday().split("-").map(Number);
  const [year, month, day] = localDate.split("-").map(Number);
  return Math.round(
    (Date.UTC(year, month - 1, day) - Date.UTC(todayYear, todayMonth - 1, todayDay)) /
      86_400_000,
  );
}

function chineseClock(hourText, minuteText) {
  const hour = Number(hourText);
  const period = hour < 6 ? "凌晨" : hour < 12 ? "上午" : hour < 18 ? "下午" : "晚上";
  const displayHour = hour % 12 || 12;
  return `${period}${displayHour}:${minuteText}`;
}

function reminderExactTime(remindAt) {
  if (!remindAt) return "尚未设置具体时间";
  const parts = zonedParts(remindAt);
  return `${Number(parts.month)} 月 ${Number(parts.day)} 日 ${parts.hour}:${parts.minute}`;
}

function upcomingReminderItems(items, nowMilliseconds = Date.now()) {
  return items
    .filter((item) => {
      const timestamp = Date.parse(item.reminder?.remind_at || "");
      return item.reminder?.status === "scheduled" &&
        Number.isFinite(timestamp) && timestamp > nowMilliseconds;
    })
    .sort(
      (left, right) => Date.parse(left.reminder.remind_at) - Date.parse(right.reminder.remind_at),
    )
    .slice(0, 3);
}

function upcomingReminderList(items, nowMilliseconds = Date.now()) {
  return upcomingReminderItems(items, nowMilliseconds).map((item) => `
    <a class="upcoming-reminder-row" href="/items/${item.id}" data-link>
      <time datetime="${escapeHtml(item.reminder.remind_at)}">${escapeHtml(reminderExactTime(item.reminder.remind_at))}</time>
      <span>${escapeHtml(item.title)}</span>
    </a>`).join("");
}

function reminderNaturalText(reminder) {
  if (!reminder?.remind_at) return "还需要选择一个具体时间";
  const target = new Date(reminder.remind_at);
  const difference = target.getTime() - Date.now();
  if (difference <= -86_400_000) {
    return `已过期 ${Math.floor(-difference / 86_400_000)} 天`;
  }
  if (reminder.status === "due" || difference <= 0) {
    const minutes = Math.floor(Math.abs(difference) / 60_000);
    return minutes < 1 ? "刚刚到了提醒时间" : `${minutes} 分钟前已到提醒时间`;
  }
  const minutes = Math.ceil(difference / 60_000);
  if (minutes < 60) return `${minutes} 分钟后会微提醒你`;
  if (minutes < 360) return `${Math.ceil(minutes / 60)} 小时后会微提醒你`;
  const parts = zonedParts(reminder.remind_at);
  const localDate = `${parts.year}-${parts.month}-${parts.day}`;
  const distance = localDateDistance(localDate);
  const dayLabel = distance === 0
    ? "今天"
    : distance === 1
      ? "明天"
      : distance === 2
        ? "后天"
        : distance === 3
          ? "大后天"
          : `${Number(parts.month)} 月 ${Number(parts.day)} 日`;
  return `${dayLabel}${chineseClock(parts.hour, parts.minute)}会微提醒你`;
}

function reminderCardText(reminder) {
  if (!reminder || reminder.status === "cancelled") return null;
  if (reminder.status === "needs_confirmation") return "提醒时间待设置";
  if (reminder.status === "due") {
    return reminder.surfaced_time ? null : "已到提醒时间";
  }
  return `提醒 · ${reminderNaturalText(reminder).replace("会微提醒你", "")}`;
}

function itemTypeLabel(value) {
  return itemTypeLabels[value] || String(value).replaceAll("_", " ").replaceAll("-", " ");
}

function deadlineTemporalState(value, now = new Date()) {
  if (!value) return null;
  const nowParts = zonedParts(now);
  const today = `${nowParts.year}-${nowParts.month}-${nowParts.day}`;
  const timed = value.includes("T") || value.includes(" ");
  const aware = timed && /(?:Z|[+-]\d{2}:\d{2})$/i.test(value);
  const parts = aware ? zonedParts(value) : null;
  const localDate = parts ? `${parts.year}-${parts.month}-${parts.day}` : value.slice(0, 10);
  const localTime = !timed ? null : parts
    ? `${parts.hour}:${parts.minute}:${parts.second}.${parts.fractionalSecond}`
    : value.slice(11);
  const nowTime = `${nowParts.hour}:${nowParts.minute}:${nowParts.second}.${nowParts.fractionalSecond}`;
  // Fixed-width local wall strings preserve naive precision without inventing
  // an offset, UTC instant, or DST fold for legacy values.
  const wallTime = (clock) => {
    const [whole, fraction = ""] = clock.split(".");
    return `${whole.length === 5 ? `${whole}:00` : whole}.${fraction.padEnd(6, "0")}`;
  };
  const overdue = aware ? new Date(value).getTime() < new Date(now).getTime()
    : localDate < today || (timed && localDate === today && wallTime(localTime) < wallTime(nowTime));
  return { localDate, localTime, overdue, today };
}

function deadlineDisplay(value) {
  const state = deadlineTemporalState(value);
  if (!state) return "";
  const clock = state.localTime ? ` ${state.localTime.slice(0, 5)}` : "";
  return `${formatDate(state.localDate)}${clock}`;
}

function deadlinePresentation(value, now = new Date()) {
  const state = deadlineTemporalState(value, now);
  if (!state) return null;
  const clock = state.localTime ? ` ${state.localTime.slice(0, 5)}` : "";
  const exact = `${formatDate(state.localDate)}${clock}`;
  if (state.overdue) return { text: `已截止 ${exact}`, emphasis: true, kind: "overdue" };
  // UTC is only used for calendar-day arithmetic, not as a date-only deadline.
  const dayNumber = (text) => {
    const [year, month, day] = text.split("-").map(Number);
    return Date.UTC(year, month - 1, day) / 86_400_000;
  };
  const difference = dayNumber(state.localDate) - dayNumber(state.today);
  if (difference === 0) return { text: `今天${clock}截止`, emphasis: true, kind: "deadline" };
  if (difference === 1) return { text: `明天${clock}截止`, emphasis: true, kind: "deadline" };
  if (difference <= 7) return { text: `${difference} 天后${clock}截止`, emphasis: true, kind: "deadline" };
  return { text: `截止 ${exact}`, emphasis: false };
}

function selectedDashboardStatus() {
  const status = new URLSearchParams(window.location.search).get("status") || "active";
  return Object.hasOwn(lifecycleViews, status) ? status : "active";
}

function selectedDashboardPage() {
  const page = Number(new URLSearchParams(window.location.search).get("page") || "1");
  return Number.isSafeInteger(page) && page > 0 ? page : 1;
}

function dashboardPath(status, page = 1) {
  const parameters = new URLSearchParams();
  if (status !== "active") parameters.set("status", status);
  if (page > 1) parameters.set("page", String(page));
  const query = parameters.toString();
  return query ? `/dashboard?${query}` : "/dashboard";
}

function lifecycleNavigation(selectedStatus) {
  if (selectedStatus === "trash") return "";
  return `
    <nav class="lifecycle-navigation" aria-label="主要事项视图">
      ${Object.entries(lifecycleViews)
        .filter(([status]) => status !== "trash")
        .map(([status, view]) => `
          <a href="${dashboardPath(status)}" data-link ${status === selectedStatus ? 'aria-current="page"' : ""}>
            ${escapeHtml(view.label)}
          </a>`)
        .join("")}
    </nav>`;
}

function resetDashboardSelection() {
  dashboardSelection.generation += 1;
  dashboardSelection.active = false;
  dashboardSelection.status = null;
  dashboardSelection.page = null;
  dashboardSelection.ids.clear();
  dashboardSelection.visibleIds = [];
  suppressedDashboardClickItemId = null;
}

function toggleDashboardSelection(itemId) {
  if (dashboardSelection.ids.has(itemId)) dashboardSelection.ids.delete(itemId);
  else dashboardSelection.ids.add(itemId);
}

function reconcileDashboardSelectionView(selectedStatus, selectedPage) {
  if (
    dashboardSelection.status !== null && (
      dashboardSelection.status !== selectedStatus ||
      dashboardSelection.page !== selectedPage
    )
  ) {
    resetDashboardSelection();
  }
}

function selectAllVisibleDashboardItems() {
  if (dashboardSelection.visibleIds.length > MAX_BULK_LIFECYCLE_ITEMS) {
    throw new Error("当前页面超过批量操作上限，请刷新后重试。");
  }
  dashboardSelection.ids = new Set(dashboardSelection.visibleIds);
}

function createDashboardFocusIntent(kind, itemId = null) {
  return {
    kind,
    itemId,
    status: selectedDashboardStatus(),
    generation: dashboardSelection.generation,
  };
}

function restoreDashboardFocus(intent) {
  if (
    !intent ||
    intent.generation !== dashboardSelection.generation ||
    intent.status !== selectedDashboardStatus() ||
    window.location.pathname !== "/dashboard"
  ) return;

  let target = null;
  if (intent.kind === "item" && Number.isSafeInteger(intent.itemId)) {
    target = document.querySelector(
      `[data-selectable-item][data-item-id="${intent.itemId}"]`,
    );
  } else if (intent.kind === "first_item") {
    target = document.querySelector("[data-selectable-item]");
  } else if (intent.kind === "select_all") {
    target = document.querySelector("[data-select-all]");
  } else if (intent.kind === "selection_entry") {
    const selectionEntry = document.querySelector("#selection-entry");
    if (selectionEntry && !selectionEntry.disabled) target = selectionEntry;
  }

  if (!target && dashboardSelection.active) {
    target = document.querySelector("[data-selection-focus-fallback]");
  }
  if (!target) {
    const selectionEntry = document.querySelector("#selection-entry");
    if (selectionEntry && !selectionEntry.disabled) target = selectionEntry;
  }
  if (!target) target = document.querySelector("[data-dashboard-focus-fallback]");
  target?.focus({ preventScroll: true });
}

function selectableItemAttributes(itemId) {
  if (!dashboardSelection.active) {
    return `data-selectable-item data-item-id="${itemId}"`;
  }
  return `data-selectable-item data-item-id="${itemId}" role="option" aria-selected="${dashboardSelection.ids.has(itemId)}"`;
}

function selectionIndicator(itemId) {
  if (!dashboardSelection.active) return "";
  const selected = dashboardSelection.ids.has(itemId);
  return `<span class="selection-indicator" aria-hidden="true">${selected ? "✓" : ""}</span>`;
}

function itemCard(item) {
  const signals = [];
  const metadata = [];
  const deadline = deadlinePresentation(item.deadline);
  if (deadline?.emphasis) {
    signals.push(`<span class="card-signal ${escapeHtml(deadline.kind)}">${escapeHtml(deadline.text)}</span>`);
  } else if (deadline) {
    metadata.push(deadline.text);
  }
  if (item.estimated_time) metadata.push(`约 ${item.estimated_time} 分钟`);
  const reminderText = reminderCardText(item.reminder);
  if (reminderText) metadata.push(reminderText);
  return `
    <a class="item-card selectable-item ${dashboardSelection.ids.has(item.id) ? "selected" : ""}" href="/items/${item.id}" data-link ${selectableItemAttributes(item.id)}>
      ${selectionIndicator(item.id)}
      <h3>${escapeHtml(item.title)}</h3>
      ${signals.length ? `<div class="card-signals">${signals.join("")}</div>` : ""}
      ${metadata.length ? `<div class="card-meta">${metadata.map((value) => `<span>${escapeHtml(value)}</span>`).join("")}</div>` : ""}
    </a>`;
}

function historyCompletionText(completedAt) {
  if (!completedAt) return "完成时间未知";
  const parts = zonedParts(completedAt);
  return `${Number(parts.month)} 月 ${Number(parts.day)} 日完成`;
}

function trashRemainingText(trashedAt, nowMilliseconds = Date.now()) {
  if (!trashedAt) return "删除时间尚不可用";
  const trashedMilliseconds = Date.parse(trashedAt);
  if (!Number.isFinite(trashedMilliseconds)) return "删除时间尚不可用";
  const remainingMilliseconds =
    trashedMilliseconds + (30 * 86_400_000) - nowMilliseconds;
  if (remainingMilliseconds <= 0) return "即将永久删除";
  return `${Math.ceil(remainingMilliseconds / 86_400_000)} 天后永久删除`;
}

function lifecycleListRow(item, status) {
  const secondary = status === "completed"
    ? historyCompletionText(item.completed_at)
    : trashRemainingText(item.trashed_at);
  return `
    <a class="lifecycle-list-row selectable-item ${dashboardSelection.ids.has(item.id) ? "selected" : ""}" href="/items/${item.id}" data-link ${selectableItemAttributes(item.id)}>
      ${selectionIndicator(item.id)}
      <span class="lifecycle-row-copy">
        <strong>${escapeHtml(item.title)}</strong>
        <span>${escapeHtml(secondary)}</span>
      </span>
    </a>`;
}

function selectionActions(status) {
  return {
    active: [
      ["complete", "标记为已完成", "primary-button"],
      ["move_to_trash", "移入回收站", "secondary-button"],
    ],
    completed: [
      ["restore_to_current", "恢复为当前", "primary-button"],
      ["move_to_trash", "移入回收站", "secondary-button"],
    ],
    trash: [
      ["restore_from_trash", "恢复", "primary-button"],
      ["permanently_delete", "永久删除", "text-button danger-button"],
    ],
  }[status];
}

function dashboardSelectionBar(status) {
  if (!dashboardSelection.active) return "";
  const count = dashboardSelection.ids.size;
  return `
    <section class="selection-bar" aria-label="批量操作">
      <p aria-live="polite"><strong>${count}</strong> 项已选择</p>
      <div class="selection-actions">
        ${status === "trash" ? '<button class="text-button" type="button" data-select-all>全选本页</button>' : ""}
        ${selectionActions(status).map(([action, label, className]) => `
          <button class="${className}" type="button" data-bulk-action="${action}" ${count ? "" : "disabled"}>${label}</button>`).join("")}
        <button class="text-button" type="button" data-cancel-selection data-selection-focus-fallback>取消</button>
      </div>
      <p id="selection-status" class="status-message" role="status"></p>
    </section>`;
}

function dashboardPagination(status, page, totalPages, totalItems, visibleCount) {
  if (totalPages <= 1) return "";
  const previous = page > 1
    ? `<a class="text-button" href="${dashboardPath(status, page - 1)}" data-link>上一页</a>`
    : '<span class="text-button disabled" aria-disabled="true">上一页</span>';
  const next = page < totalPages
    ? `<a class="text-button" href="${dashboardPath(status, page + 1)}" data-link>下一页</a>`
    : '<span class="text-button disabled" aria-disabled="true">下一页</span>';
  return `
    <nav class="dashboard-pagination" aria-label="事项分页">
      ${previous}
      <span>第 ${page} / ${totalPages} 页，本页 ${visibleCount} 项，共 ${totalItems} 项</span>
      ${next}
    </nav>`;
}

function bindDashboardLongPressRows(selectedStatus) {
  const selectedPage = selectedDashboardPage();
  document.querySelectorAll("[data-selectable-item]").forEach((row) => {
    let pointerId = null;
    let startX = 0;
    let startY = 0;
    let timer = null;
    let longPressed = false;
    let selectionGeneration = dashboardSelection.generation;

    const cancelTimer = () => {
      if (timer !== null) window.clearTimeout(timer);
      timer = null;
    };
    const finishPointer = (event) => {
      if (pointerId !== event.pointerId) return;
      cancelTimer();
      if (row.hasPointerCapture?.(pointerId)) {
        row.releasePointerCapture(pointerId);
      }
      pointerId = null;
    };

    row.addEventListener("pointerdown", (event) => {
      if (event.pointerType === "touch" || event.pointerType === "pen") {
        activeSelectableTouchPress = {
          pointerId: event.pointerId,
          startX: event.clientX,
          startY: event.clientY,
          itemId: Number(row.dataset.itemId),
        };
      }
      if (
        dashboardSelection.active ||
        pointerId !== null ||
        event.isPrimary === false ||
        (event.button !== undefined && event.button !== 0)
      ) return;
      pointerId = event.pointerId;
      startX = event.clientX;
      startY = event.clientY;
      longPressed = false;
      selectionGeneration = dashboardSelection.generation;
      row.setPointerCapture?.(pointerId);
      timer = window.setTimeout(() => {
        timer = null;
        if (
          selectionGeneration !== dashboardSelection.generation ||
          window.location.pathname !== "/dashboard" ||
          selectedDashboardStatus() !== selectedStatus ||
          selectedDashboardPage() !== selectedPage
        ) return;
        longPressed = true;
        const itemId = Number(row.dataset.itemId);
        suppressedDashboardClickItemId = itemId;
        window.setTimeout(() => {
          if (suppressedDashboardClickItemId === itemId) {
            suppressedDashboardClickItemId = null;
          }
        }, 1_000);
        dashboardSelection.active = true;
        dashboardSelection.status = selectedStatus;
        dashboardSelection.page = selectedDashboardPage();
        dashboardSelection.ids.add(itemId);
        void renderDashboard({
          silent: true,
          focusIntent: createDashboardFocusIntent("item", itemId),
        });
      }, ITEM_LONG_PRESS_DELAY_MS);
    });
    row.addEventListener("pointermove", (event) => {
      if (
        activeSelectableTouchPress?.pointerId === event.pointerId &&
        Math.hypot(
          event.clientX - activeSelectableTouchPress.startX,
          event.clientY - activeSelectableTouchPress.startY,
        ) > ITEM_LONG_PRESS_MOVE_THRESHOLD_PX
      ) {
        activeSelectableTouchPress = null;
      }
      if (pointerId !== event.pointerId || timer === null) return;
      if (
        Math.hypot(event.clientX - startX, event.clientY - startY) >
        ITEM_LONG_PRESS_MOVE_THRESHOLD_PX
      ) {
        cancelTimer();
      }
    });
    row.addEventListener("pointerup", finishPointer);
    row.addEventListener("pointercancel", finishPointer);
    row.addEventListener("lostpointercapture", (event) => {
      if (pointerId === event.pointerId) {
        cancelTimer();
        pointerId = null;
      }
    });
    row.addEventListener("contextmenu", (event) => {
      const touchLikeLongPressMenu =
        (event.pointerType === "touch" || event.pointerType === "pen") &&
        event.button !== 2;
      const recentTouchPressOnRow =
        event.pointerType !== "mouse" &&
        recentSelectableTouchPress?.itemId === Number(row.dataset.itemId);
      if (
        longPressed ||
        activeSelectableTouchPress !== null ||
        touchLikeLongPressMenu ||
        recentTouchPressOnRow
      ) {
        event.preventDefault();
      }
    });
  });
}

async function submitBulkLifecycle(action, itemIds) {
  return api("/api/items/bulk-lifecycle", {
    method: "POST",
    body: JSON.stringify({ action, item_ids: [...itemIds] }),
  });
}

function bindDashboardSelectionControls(selectedStatus) {
  const selectionEntry = document.querySelector("#selection-entry");
  selectionEntry?.addEventListener("click", () => {
    dashboardSelection.active = true;
    dashboardSelection.status = selectedStatus;
    dashboardSelection.page = selectedDashboardPage();
    dashboardSelection.ids.clear();
    void renderDashboard({
      silent: true,
      focusIntent: createDashboardFocusIntent("first_item"),
    });
  });
  document.querySelector("[data-cancel-selection]")?.addEventListener("click", () => {
    resetDashboardSelection();
    void renderDashboard({
      silent: true,
      focusIntent: createDashboardFocusIntent("selection_entry"),
    });
  });
  document.querySelector("[data-select-all]")?.addEventListener("click", () => {
    selectAllVisibleDashboardItems();
    void renderDashboard({
      silent: true,
      focusIntent: createDashboardFocusIntent("select_all"),
    });
  });
  document.querySelectorAll("[data-bulk-action]").forEach((button) => {
    button.addEventListener("click", async () => {
      const itemIds = [...dashboardSelection.ids];
      if (!itemIds.length) return;
      const destructive = button.dataset.bulkAction === "permanently_delete";
      if (
        destructive &&
        !window.confirm(`确认永久删除已选择的 ${itemIds.length} 个事项吗？此操作无法撤销。`)
      ) return;
      button.disabled = true;
      const status = document.querySelector("#selection-status");
      if (status) status.textContent = "正在应用批量操作…";
      try {
        await submitBulkLifecycle(button.dataset.bulkAction, itemIds);
        resetDashboardSelection();
        await renderDashboard({
          silent: true,
          focusIntent: createDashboardFocusIntent("selection_entry"),
        });
      } catch (error) {
        if (status) {
          status.dataset.kind = "error";
          status.textContent = error.message;
        }
        button.disabled = false;
      }
    });
  });
  document.querySelector("#clear-trash-button")?.addEventListener("click", async (event) => {
    const snapshotIds = [...dashboardSelection.visibleIds];
    if (!snapshotIds.length) return;
    const button = event.currentTarget;
    const confirmation = button.dataset.clearTrashScope === "all"
      ? `确认清空回收站中的 ${snapshotIds.length} 个事项吗？此操作无法撤销。`
      : `回收站共有 ${button.dataset.totalItems} 个事项。确认永久删除本页显示的 ${snapshotIds.length} 个事项吗？此操作无法撤销。`;
    if (!window.confirm(confirmation)) return;
    button.disabled = true;
    try {
      await submitBulkLifecycle("permanently_delete", snapshotIds);
      resetDashboardSelection();
      await renderDashboard({
        silent: true,
        focusIntent: createDashboardFocusIntent("selection_entry"),
      });
    } catch (error) {
      const status = document.querySelector("#dashboard-action-status");
      if (status) {
        status.dataset.kind = "error";
        status.textContent = error.message;
      }
      button.disabled = false;
    }
  });
}

function selectedReminderText(localDate, localTime) {
  const distance = localDateDistance(localDate);
  const [year, month, day] = localDate.split("-").map(Number);
  const [hour, minute] = localTime.split(":");
  const dateLabel = distance === 0
    ? "今天"
    : distance === 1
      ? "明天"
      : distance === 2
        ? "后天"
        : distance === 3
          ? "大后天"
          : `${year} 年 ${month} 月 ${day} 日`;
  return `将在${dateLabel}${chineseClock(hour, minute)}微提醒你`;
}

function openReminderEditor({ item, reminder = null, onSaved }) {
  document.querySelector("#reminder-dialog")?.remove();
  const isActiveReminder = reminder && ["needs_confirmation", "scheduled"].includes(reminder.status);
  const existingParts = reminder?.remind_at ? zonedParts(reminder.remind_at) : null;
  let selectedDate = existingParts
    ? `${existingParts.year}-${existingParts.month}-${existingParts.day}`
    : addProfileDays(0);
  let selectedTime = existingParts
    ? `${existingParts.hour}:${existingParts.minute}`
    : authentication.user.default_reminder_time;
  let usingDefaultTime = !existingParts;

  appElement.insertAdjacentHTML("beforeend", `
    <dialog id="reminder-dialog" class="reminder-dialog" aria-labelledby="reminder-dialog-title">
      <form id="reminder-form" class="reminder-surface">
        <header class="reminder-dialog-header">
          <div>
            <p class="eyebrow">微提醒</p>
            <h2 id="reminder-dialog-title">${isActiveReminder ? "修改提醒时间" : "什么时候再想起它？"}</h2>
          </div>
          <button class="reminder-close-button" type="button" aria-label="关闭提醒设置">×</button>
        </header>
        <p class="reminder-item-title">${escapeHtml(item.title)}</p>
        <div class="reminder-date-options" role="group" aria-label="快速选择日期">
          <button type="button" data-days="0">今天</button>
          <button type="button" data-days="1">明天</button>
          <button type="button" data-days="2">后天</button>
          <button type="button" id="reminder-custom-date-button">选日期</button>
        </div>
        <div id="reminder-date-field" class="reminder-date-field" hidden>
          <label for="reminder-date">选择日期</label>
          <input id="reminder-date" type="date" min="${profileToday()}" value="${selectedDate}" required />
          <p class="form-hint">今天是 ${formatDate(profileToday())}；过去日期不可选择。</p>
        </div>
        <div class="reminder-time-summary">
          <p id="reminder-selection-summary"></p>
          <button id="reminder-change-time" class="secondary-button quiet-button" type="button">改时间</button>
        </div>
        <div id="reminder-time-field" class="reminder-time-field" hidden>
          <label for="reminder-time">具体时间</label>
          <input id="reminder-time" type="time" value="${selectedTime}" required />
          <p class="form-hint">未主动修改时使用账户默认时间 ${escapeHtml(authentication.user.default_reminder_time)}。</p>
        </div>
        <p id="reminder-form-status" class="status-message" role="status"></p>
        <div class="reminder-dialog-actions">
          <button class="secondary-button reminder-cancel-button" type="button">暂不设置</button>
          <button class="primary-button" type="submit">保存提醒</button>
        </div>
      </form>
    </dialog>`);

  const dialog = document.querySelector("#reminder-dialog");
  const form = document.querySelector("#reminder-form");
  const dateInput = document.querySelector("#reminder-date");
  const timeInput = document.querySelector("#reminder-time");
  const dateField = document.querySelector("#reminder-date-field");
  const timeField = document.querySelector("#reminder-time-field");
  const status = document.querySelector("#reminder-form-status");
  const submit = form.querySelector("button[type='submit']");

  const renderSelection = () => {
    document.querySelector("#reminder-selection-summary").textContent = selectedReminderText(
      selectedDate,
      selectedTime,
    );
    form.querySelectorAll("[data-days]").forEach((button) => {
      button.dataset.selected = String(addProfileDays(Number(button.dataset.days)) === selectedDate);
    });
  };
  const close = () => {
    if (typeof dialog.close === "function") dialog.close();
    else dialog.remove();
  };

  form.querySelectorAll("[data-days]").forEach((button) => {
    button.addEventListener("click", () => {
      selectedDate = addProfileDays(Number(button.dataset.days));
      dateInput.value = selectedDate;
      dateField.hidden = true;
      form.dataset.dirty = "true";
      renderSelection();
    });
  });
  document.querySelector("#reminder-custom-date-button").addEventListener("click", () => {
    dateField.hidden = false;
    dateInput.focus();
  });
  dateInput.addEventListener("change", () => {
    if (!dateInput.value) return;
    selectedDate = dateInput.value;
    form.dataset.dirty = "true";
    renderSelection();
  });
  document.querySelector("#reminder-change-time").addEventListener("click", () => {
    timeField.hidden = false;
    timeInput.focus();
  });
  timeInput.addEventListener("change", () => {
    selectedTime = timeInput.value;
    usingDefaultTime = false;
    form.dataset.dirty = "true";
    renderSelection();
  });
  [document.querySelector(".reminder-close-button"), document.querySelector(".reminder-cancel-button")]
    .forEach((button) => button.addEventListener("click", close));
  dialog.addEventListener("close", () => dialog.remove(), { once: true });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    submit.disabled = true;
    status.dataset.kind = "";
    status.textContent = "正在保存提醒…";
    const payload = { local_date: selectedDate };
    if (!usingDefaultTime || isActiveReminder) payload.local_time = selectedTime;
    try {
      const saved = await api(
        isActiveReminder ? `/api/reminders/${reminder.id}` : `/api/items/${item.id}/reminder`,
        {
          method: isActiveReminder ? "PUT" : "POST",
          body: JSON.stringify(payload),
        },
      );
      close();
      await onSaved(saved);
      notificationOnboardingHasReminder = true;
      renderNotificationOnboarding();
    } catch (error) {
      status.dataset.kind = "error";
      status.textContent = `提醒未保存：${error.message}`;
      submit.disabled = false;
    }
  });

  renderSelection();
  form.dataset.dirty = "false";
  if (typeof dialog.showModal === "function") dialog.showModal();
  else dialog.setAttribute("open", "");
}

function quickConfirmationCard(item) {
  const reminderChoice = item.reminder?.status === "needs_confirmation"
    ? `
        <div class="quick-reminder-choice reminder-needs-confirmation">
          <span>你想之后被提醒，但还没有确定时间。</span>
          <div>
            <button class="secondary-button reminder-decline-button" type="button" data-item-id="${item.id}">暂时不用提醒</button>
            <button class="primary-button reminder-set-button" type="button" data-item-id="${item.id}">设置时间</button>
          </div>
        </div>`
    : "";
  return `
    <article class="quick-confirmation-card">
      <h3>${escapeHtml(item.title)}</h3>
      ${reminderChoice}
    </article>`;
}

function renderLoading() {
  appElement.innerHTML = '<p class="loading" role="status">正在确认登录状态…</p>';
}

function renderNetworkError() {
  appElement.innerHTML = `
    <section class="auth-layout">
      <div class="panel auth-panel network-panel">
        <p class="eyebrow">暂时无法连接</p>
        <h1>没有清除你的登录状态。</h1>
        <p class="subtitle">服务器暂时不可用或网络已断开。恢复连接后重试即可。</p>
        <button id="auth-retry-button" class="primary-button" type="button">重试连接</button>
      </div>
    </section>`;
  document.querySelector("#auth-retry-button").addEventListener("click", () => {
    void initializeAuthentication();
  });
}

function renderNotFound() {
  appElement.innerHTML = `
    <section class="auth-layout">
      <div class="empty-state not-found-panel">
        <p class="eyebrow">404</p>
        <h1>这个页面不存在。</h1>
        <a href="/" data-link>返回 SelfEcho</a>
      </div>
    </section>`;
}

function publicAuthIdentityMarkup() {
  return `
        <div class="login-branding">
          <p class="login-product-brand">SelfEcho AI</p>
          <p class="login-formal-name">个人智能记录工具</p>
        </div>`;
}

function renderLogin() {
  appElement.innerHTML = `
    <section class="auth-layout">
      <div class="auth-heading">
        ${publicAuthIdentityMarkup()}
        <p class="eyebrow">登录</p>
        <h1>继续记录。</h1>
        <p class="subtitle">回到只属于你的记录和事项。</p>
      </div>
      <div class="panel auth-panel">
        <form id="login-form" class="auth-form">
          <div class="field-group">
            <label for="login-email">邮箱</label>
            <input id="login-email" name="email" type="email" autocomplete="email" maxlength="320" required autofocus />
          </div>
          <div class="field-group">
            <label for="login-password">密码</label>
            <input id="login-password" name="password" type="password" autocomplete="current-password" maxlength="128" required />
          </div>
          <p id="login-status" class="status-message" role="alert"></p>
          <button class="primary-button auth-submit" type="submit">登录</button>
        </form>
        <p class="auth-switch">收到邀请码？<a href="/register" data-link>注册账号</a></p>
      </div>
    </section>`;

  const form = document.querySelector("#login-form");
  const status = document.querySelector("#login-status");
  const submitButton = form.querySelector("button[type='submit']");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    submitButton.disabled = true;
    status.dataset.kind = "";
    status.textContent = "正在登录…";
    const formData = new FormData(form);
    try {
      const user = await api("/api/auth/login", {
        method: "POST",
        body: JSON.stringify({
          email: formData.get("email"),
          password: formData.get("password"),
        }),
      });
      setAuthentication(AUTH_STATES.AUTHENTICATED, user);
      history.replaceState({}, "", safeLoginDestination());
      renderRoute();
    } catch (error) {
      status.dataset.kind = "error";
      status.textContent = loginErrorMessage(error);
      submitButton.disabled = false;
    }
  });
}

function loginErrorMessage(error) {
  if (error instanceof NetworkError) return error.message;
  if (error instanceof ApiError && error.status === 401) {
    return "邮箱或密码不正确，或账号当前不可用。";
  }
  return "暂时无法登录，请稍后重试。";
}

function detectedTimezone() {
  try {
    return Intl.DateTimeFormat().resolvedOptions().timeZone || "Asia/Shanghai";
  } catch (_) {
    return "Asia/Shanghai";
  }
}

function renderRegister() {
  appElement.innerHTML = `
    <section class="auth-layout">
      <div class="auth-heading">
        ${publicAuthIdentityMarkup()}
        <p class="eyebrow">受邀注册</p>
        <h1>创建账号。</h1>
        <p class="subtitle">每个账号都有独立的数据空间。</p>
      </div>
      <div class="panel auth-panel">
        <form id="register-form" class="auth-form">
          <div class="field-group">
            <label for="register-invite">邀请码</label>
            <input id="register-invite" name="invite_code" type="password" autocomplete="off" maxlength="256" required autofocus />
          </div>
          <div class="field-group">
            <label for="register-email">邮箱</label>
            <input id="register-email" name="email" type="email" autocomplete="email" maxlength="320" required />
          </div>
          <div class="field-group">
            <label for="register-display-name">显示名称</label>
            <input id="register-display-name" name="display_name" type="text" autocomplete="name" maxlength="100" required />
          </div>
          <div class="field-group">
            <label for="register-password">密码</label>
            <input id="register-password" name="password" type="password" autocomplete="new-password" minlength="12" maxlength="128" required />
            <p class="field-hint">至少 12 个字符；建议使用密码管理器生成并保存。</p>
          </div>
          <div class="field-group">
            <label for="register-timezone">时区</label>
            <input id="register-timezone" name="timezone" type="text" maxlength="100" value="${escapeHtml(detectedTimezone())}" required />
          </div>
          <p id="register-status" class="status-message" role="alert"></p>
          <button class="primary-button auth-submit" type="submit">注册并登录</button>
        </form>
        <p class="auth-switch">已有账号？<a href="/login" data-link>返回登录</a></p>
      </div>
    </section>`;

  const form = document.querySelector("#register-form");
  const status = document.querySelector("#register-status");
  const submitButton = form.querySelector("button[type='submit']");
  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    submitButton.disabled = true;
    status.dataset.kind = "";
    status.textContent = "正在创建账号…";
    const formData = new FormData(form);
    try {
      const user = await api("/api/auth/register", {
        method: "POST",
        body: JSON.stringify({
          invite_code: formData.get("invite_code"),
          email: formData.get("email"),
          password: formData.get("password"),
          display_name: formData.get("display_name"),
          timezone: formData.get("timezone"),
        }),
      });
      setAuthentication(AUTH_STATES.AUTHENTICATED, user);
      history.replaceState({}, "", "/");
      renderRoute();
    } catch (error) {
      status.dataset.kind = "error";
      status.textContent = registrationErrorMessage(error);
      submitButton.disabled = false;
    }
  });
}

function registrationErrorMessage(error) {
  if (error instanceof NetworkError) return error.message;
  if (error instanceof ApiError && error.status === 403) {
    return "邀请码无效，或注册当前未开放。";
  }
  if (error instanceof ApiError && error.status === 409) {
    return "这个邮箱已经注册，请直接登录。";
  }
  if (error instanceof ApiError && error.status === 422) {
    return "注册信息不符合要求，请检查邮箱、密码和时区。";
  }
  return "暂时无法创建账号，请稍后重试。";
}

function renderAccount() {
  const user = authentication.user;
  appElement.innerHTML = `
    <section class="page-heading account-heading">
      <p class="eyebrow">账户</p>
      <h1>账户设置</h1>
      <p class="subtitle">${escapeHtml(user.display_name)}，在这里管理提醒和当前账号。</p>
    </section>
    <div class="account-panel">
      <section class="settings-section profile-settings" aria-labelledby="profile-settings-heading">
        <h2 id="profile-settings-heading">个人资料</h2>
      <dl class="account-details">
        <div><dt>邮箱</dt><dd>${escapeHtml(user.email)}</dd></div>
        <div><dt>显示名称</dt><dd>${escapeHtml(user.display_name)}</dd></div>
        <div><dt>时区</dt><dd>${escapeHtml(user.timezone)}</dd></div>
      </dl>
      </section>
      <form id="reminder-settings-form" class="settings-section reminder-settings-form" aria-labelledby="reminder-preferences-heading">
        <div>
          <h2 id="reminder-preferences-heading">提醒偏好</h2>
          <label for="default-reminder-time">无具体时间时默认提醒</label>
          <p class="form-hint">按 ${escapeHtml(user.timezone)} 的本地时间解释。</p>
        </div>
        <input id="default-reminder-time" type="time" value="${escapeHtml(user.default_reminder_time)}" required />
        <button class="secondary-button" type="submit">保存默认时间</button>
        <p id="reminder-settings-status" class="status-message" role="status"></p>
      </form>
      <section class="settings-section notification-device-settings" aria-labelledby="notification-device-heading">
        <div>
          <h2 id="notification-device-heading">此设备通知</h2>
          <p class="form-hint">通知只用于你创建的提醒，不用于广告或活动推广。</p>
          <p id="notification-device-status" class="status-message" role="status"></p>
        </div>
        <button id="notification-device-action" class="secondary-button" type="button" hidden></button>
      </section>
      <section id="email-reminder-settings" class="settings-section email-reminder-settings" aria-labelledby="email-reminder-heading">
        <div class="email-reminder-heading">
          <div>
            <h2 id="email-reminder-heading">邮件提醒</h2>
            <p class="form-hint">邮件提醒与当前设备的系统通知彼此独立。</p>
          </div>
          <span id="email-reminder-badge" class="settings-badge">读取中</span>
        </div>
        <div id="email-reminder-controls">
          <p class="status-message" role="status">正在读取邮件提醒设置…</p>
        </div>
      </section>
      <div class="account-actions settings-section">
        <p id="logout-status" class="status-message" role="alert"></p>
        <button id="logout-button" class="secondary-button" type="button">退出登录</button>
      </div>
    </div>`;

  const button = document.querySelector("#logout-button");
  const status = document.querySelector("#logout-status");
  const reminderSettingsForm = document.querySelector("#reminder-settings-form");
  const reminderSettingsStatus = document.querySelector("#reminder-settings-status");
  const notificationAction = document.querySelector("#notification-device-action");
  renderNotificationDeviceSettings();
  void initializePushForAuthenticatedUser();
  void loadEmailReminderSettings();
  notificationAction.addEventListener("click", async () => {
    notificationAction.disabled = true;
    if (["enabled", "cleanup_required"].includes(pushDeviceState.status)) {
      await disableDeviceNotifications();
    } else {
      await enableDeviceNotifications();
    }
    renderNotificationDeviceSettings();
  });
  reminderSettingsForm.addEventListener("submit", async (event) => {
    event.preventDefault();
    const submit = reminderSettingsForm.querySelector("button[type='submit']");
    const defaultTime = document.querySelector("#default-reminder-time").value;
    submit.disabled = true;
    reminderSettingsStatus.dataset.kind = "";
    reminderSettingsStatus.textContent = "正在保存默认时间…";
    try {
      const settings = await api("/api/reminder-settings", {
        method: "PATCH",
        body: JSON.stringify({ default_reminder_time: defaultTime }),
      });
      authentication.user.default_reminder_time = settings.default_reminder_time;
      reminderSettingsStatus.dataset.kind = "success";
      reminderSettingsStatus.textContent = `已保存：${settings.default_reminder_time}`;
    } catch (error) {
      reminderSettingsStatus.dataset.kind = "error";
      reminderSettingsStatus.textContent = `默认时间未保存：${error.message}`;
    } finally {
      submit.disabled = false;
    }
  });
  button.addEventListener("click", async () => {
    button.disabled = true;
    status.dataset.kind = "";
    status.textContent = "正在退出…";
    try {
      await performLogout();
    } catch (error) {
      status.dataset.kind = "error";
      status.textContent = error instanceof ApiError && error.status === 403
        ? "安全校验已失效，请刷新页面后重试。"
        : `退出失败：${error.message}`;
      button.disabled = false;
    }
  });
}

async function loadEmailReminderSettings() {
  const controls = document.querySelector("#email-reminder-controls");
  if (!controls) return;
  try {
    const settings = await api("/api/email-reminders/settings");
    renderEmailReminderSettings(settings);
  } catch (error) {
    controls.innerHTML = `<p class="status-message" data-kind="error" role="alert">无法读取邮件提醒设置：${escapeHtml(error.message)}</p>`;
    const badge = document.querySelector("#email-reminder-badge");
    if (badge) badge.textContent = "不可用";
  }
}

function renderEmailReminderSettings(settings, message = "", kind = "") {
  const controls = document.querySelector("#email-reminder-controls");
  const badge = document.querySelector("#email-reminder-badge");
  if (!controls || !badge) return;
  const address = settings.email_address;
  const isVerified = settings.verification_status === "verified";
  const isPaused = settings.health_status === "paused";
  const stateLabel = isPaused
    ? "已暂停"
    : !address
      ? "未设置"
      : !isVerified
        ? "待验证"
        : settings.enabled
          ? "已开启"
          : "已关闭";
  badge.textContent = stateLabel;
  badge.dataset.kind = isPaused ? "error" : settings.effective_active ? "success" : "";
  const addressValue = address || authentication.user?.email || "";
  const addressHelp = isPaused
    ? "该地址已暂停接收提醒。需要更换并验证其他邮箱，不能用同一地址自行解除。"
    : address && !isVerified
      ? settings.enabled
        ? "待验证；你之前的开启意愿已保留，验证成功后会自动恢复。"
        : "待验证；验证成功后仍由你决定是否开启邮件提醒。"
      : "修改地址后，邮件提醒会暂停，直到新地址验证成功。";
  const verificationControls = address && !isVerified && !isPaused
    ? `
      <div class="email-verification-actions">
        <button id="email-send-code" class="secondary-button" type="button" ${settings.provider_available ? "" : "disabled"}>发送 / 重发验证码</button>
        <form id="email-verification-form" class="email-inline-form">
          <label for="email-verification-code">6 位验证码</label>
          <input id="email-verification-code" name="code" type="text" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{6}" minlength="6" maxlength="6" required />
          <button class="primary-button" type="submit">验证</button>
        </form>
      </div>`
    : "";
  const enabledControls = address && isVerified && !isPaused
    ? `
      <div class="email-enabled-row">
        <div>
          <strong>邮件提醒${settings.enabled ? "已开启" : "已关闭"}</strong>
          <p class="form-hint">事项到期时，会按当前设置发送提醒邮件。</p>
        </div>
        <button id="email-enabled-toggle" class="${settings.enabled ? "secondary-button" : "primary-button"}" type="button">${settings.enabled ? "关闭邮件提醒" : "开启邮件提醒"}</button>
      </div>
      <div class="email-test-row">
        <button id="email-test-button" class="secondary-button" type="button" ${settings.test_email_available ? "" : "disabled"}>发送测试邮件</button>
        <p class="form-hint">${settings.test_email_available ? "测试邮件只会发送到当前已验证邮箱。" : "当前未提供测试邮件，因此暂时不能发送。"}</p>
      </div>`
    : "";
  const providerNotice = settings.provider_available
    ? ""
    : '<p class="status-message" data-kind="error">邮件提醒服务尚未配置，暂时不能发送验证码或提醒。</p>';
  controls.innerHTML = `
    <form id="email-address-form" class="email-address-form">
      <div class="field-group">
        <label for="email-reminder-address">提醒邮箱</label>
        <input id="email-reminder-address" name="email_address" type="email" autocomplete="email" maxlength="320" value="${escapeHtml(addressValue)}" required />
        <p class="field-hint">${escapeHtml(addressHelp)}</p>
      </div>
      <button class="secondary-button" type="submit">${address ? "更改邮箱" : "设置邮箱"}</button>
    </form>
    ${providerNotice}
    ${isPaused ? '<p class="status-message" data-kind="error">邮件提醒当前不可用，请更换并验证其他邮箱。</p>' : ""}
    ${verificationControls}
    ${enabledControls}
    <p id="email-reminder-status" class="status-message" data-kind="${escapeHtml(kind)}" role="status">${escapeHtml(message)}</p>`;

  const addressForm = document.querySelector("#email-address-form");
  addressForm?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const submit = addressForm.querySelector("button[type='submit']");
    const input = document.querySelector("#email-reminder-address");
    await runEmailSettingsAction(
      submit,
      "正在保存提醒邮箱…",
      async () => api("/api/email-reminders/address", {
        method: "PUT",
        body: JSON.stringify({ email_address: input.value }),
      }),
      "提醒邮箱已保存，请发送验证码完成验证。",
    );
  });
  document.querySelector("#email-send-code")?.addEventListener("click", async (event) => {
    await runEmailOperationAction(
      event.currentTarget,
      "正在提交验证码邮件…",
      () => api("/api/email-reminders/verification/send", { method: "POST" }),
    );
  });
  document.querySelector("#email-verification-form")?.addEventListener("submit", async (event) => {
    event.preventDefault();
    const form = event.currentTarget;
    const submit = form.querySelector("button[type='submit']");
    const code = form.querySelector("input[name='code']").value;
    await runEmailOperationAction(
      submit,
      "正在验证…",
      () => api("/api/email-reminders/verification/confirm", {
        method: "POST",
        body: JSON.stringify({ code }),
      }),
    );
  });
  document.querySelector("#email-enabled-toggle")?.addEventListener("click", async (event) => {
    await runEmailSettingsAction(
      event.currentTarget,
      "正在更新邮件提醒…",
      () => api("/api/email-reminders/enabled", {
        method: "PUT",
        body: JSON.stringify({ enabled: !settings.enabled }),
      }),
      settings.enabled ? "邮件提醒已关闭。" : "邮件提醒已开启。",
    );
  });
  document.querySelector("#email-test-button")?.addEventListener("click", async (event) => {
    await runEmailOperationAction(
      event.currentTarget,
      "正在提交测试邮件…",
      () => api("/api/email-reminders/test", { method: "POST" }),
    );
  });
}

async function runEmailSettingsAction(button, pendingMessage, action, successMessage) {
  const status = document.querySelector("#email-reminder-status");
  button.disabled = true;
  if (status) {
    status.dataset.kind = "";
    status.textContent = pendingMessage;
  }
  try {
    const settings = await action();
    renderEmailReminderSettings(settings, successMessage, "success");
  } catch (error) {
    button.disabled = false;
    if (status) {
      status.dataset.kind = "error";
      status.textContent = error.message;
    }
  }
}

async function runEmailOperationAction(button, pendingMessage, action) {
  const status = document.querySelector("#email-reminder-status");
  button.disabled = true;
  if (status) {
    status.dataset.kind = "";
    status.textContent = pendingMessage;
  }
  try {
    const result = await action();
    if (result.settings) {
      renderEmailReminderSettings(result.settings, result.message, "success");
    } else if (status) {
      status.dataset.kind = "success";
      status.textContent = result.message;
      button.disabled = false;
    }
  } catch (error) {
    button.disabled = false;
    if (status) {
      status.dataset.kind = "error";
      status.textContent = error.message;
    }
  }
}

function safeLoginDestination() {
  const next = new URLSearchParams(window.location.search).get("next");
  if (!next || !next.startsWith("/") || next.startsWith("//")) return "/";
  try {
    const target = new URL(next, window.location.origin);
    return target.origin === window.location.origin && isPrivatePath(target.pathname)
      ? `${target.pathname}${target.search}`
      : "/";
  } catch (_) {
    return "/";
  }
}

function isPrivatePath(path) {
  return path === "/" || path === "/capture" || path === "/dashboard" ||
    path === "/account" || /^\/items\/\d+$/.test(path);
}

function isKnownPath(path) {
  return isPrivatePath(path) || path === "/login" || path === "/register";
}

function completeLocalLogout() {
  if (pollTimer) window.clearTimeout(pollTimer);
  if (authRedirectTimer) window.clearTimeout(authRedirectTimer);
  pollTimer = null;
  authRedirectTimer = null;
  clearCaptureDraft();
  setAuthentication(AUTH_STATES.UNAUTHENTICATED, null);
  appElement.replaceChildren();
  history.replaceState({}, "", "/login");
  renderRoute();
}

function scheduleSessionExpiredRedirect() {
  if (authentication.status !== AUTH_STATES.AUTHENTICATED || authRedirectTimer) return;
  if (activeCaptureController?.deferSessionExpiredRedirect()) return;
  const next = `${window.location.pathname}${window.location.search}`;
  setAuthentication(AUTH_STATES.UNAUTHENTICATED, null);
  authRedirectTimer = window.setTimeout(() => {
    authRedirectTimer = null;
    if (authentication.status !== AUTH_STATES.UNAUTHENTICATED) return;
    if (pollTimer) window.clearTimeout(pollTimer);
    pollTimer = null;
    appElement.replaceChildren();
    history.replaceState({}, "", `/login?next=${encodeURIComponent(next)}`);
    renderRoute();
  }, 0);
}

function captureDraftStorageKey() {
  return authentication.user
    ? `${CAPTURE_DRAFT_KEY}.${authentication.user.id}`
    : `${CAPTURE_DRAFT_KEY}.anonymous`;
}

function readCaptureBuffer() {
  try {
    const raw = sessionStorage.getItem(captureDraftStorageKey());
    if (!raw) return { text: "", dirty: false, draftId: null, revision: 0, savePending: false };
    try {
      const parsed = JSON.parse(raw);
      if (parsed && parsed.version === 1 && typeof parsed.text === "string") {
        return {
          text: parsed.text,
          dirty: parsed.dirty === true,
          draftId: Number.isInteger(parsed.draftId) ? parsed.draftId : null,
          revision: Number.isInteger(parsed.revision) ? parsed.revision : 0,
          savePending: parsed.savePending === true,
        };
      }
    } catch (_) {
      // A pre-v0.5 plain-text value is still recoverable local input.
    }
    return { text: raw, dirty: true, draftId: null, revision: 0, savePending: false };
  } catch (_) {
    return { text: "", dirty: false, draftId: null, revision: 0, savePending: false };
  }
}

function readCaptureDraft() {
  return readCaptureBuffer().text;
}

function writeCaptureDraft(value, metadata = {}) {
  try {
    const key = captureDraftStorageKey();
    if (value || metadata.dirty || metadata.savePending) {
      sessionStorage.setItem(key, JSON.stringify({
        version: 1,
        text: value,
        dirty: metadata.dirty === true,
        draftId: Number.isInteger(metadata.draftId) ? metadata.draftId : null,
        revision: Number.isInteger(metadata.revision) ? metadata.revision : 0,
        savePending: metadata.savePending === true,
      }));
    }
    else sessionStorage.removeItem(key);
  } catch (_) {
    // Capture still works if browser storage is unavailable.
  }
}

function clearCaptureDraft() {
  writeCaptureDraft("");
}

function voiceGestureCancelArmed(startY, currentY) {
  return currentY <= startY - VOICE_CANCEL_DISTANCE_PX;
}

function captureHasActiveSegment(draft) {
  return Boolean(draft?.voice_segments?.some((segment) =>
    ["pending", "transcribing"].includes(segment.transcription_status)
  ));
}

function captureHasFailedSegment(draft) {
  return Boolean(draft?.voice_segments?.some((segment) =>
    segment.transcription_status === "failed"
  ));
}

// Transcription failures cannot be classified as no-speech versus generic
// provider failure, so they share one truthful copy; only codes whose backend
// message is already safe and specific keep the backend wording.
const VOICE_TRANSCRIPTION_FAILURE_COPY = "这段录音没有得到可用文字。原始录音已保留。";
const VOICE_DETAILED_FAILURE_CODES = new Set([
  "media_probe",
  "unsupported_media",
  "conversion",
  "draft_text_limit",
]);

function voiceSegmentFailureCopy(segment) {
  if (
    segment.failure_code &&
    VOICE_DETAILED_FAILURE_CODES.has(segment.failure_code) &&
    segment.failure_message
  ) {
    return segment.failure_message;
  }
  return VOICE_TRANSCRIPTION_FAILURE_COPY;
}

function captureDiscardControl({
  draft,
  hasText,
  hasPendingUpload,
  savePending,
  discardPending,
}) {
  return {
    hidden: Boolean(savePending || (!draft && !hasText && !hasPendingUpload)),
    disabled: Boolean(discardPending),
  };
}

function capturePrefersWholeDraftDiscard({
  draft,
  currentText,
  hasPendingUpload,
  hasRevisionConflict,
  savePending = false,
}) {
  const segments = draft?.voice_segments || [];
  return !String(currentText || "").trim() &&
    segments.length === 1 &&
    segments[0].transcription_status === "failed" &&
    !hasPendingUpload &&
    !hasRevisionConflict &&
    !savePending;
}

async function runWithCaptureDiscardPending({
  state,
  updateControls,
  operation,
}) {
  if (state.discardPending) return false;
  state.discardPending = true;
  updateControls();
  try {
    await operation();
    return true;
  } finally {
    state.discardPending = false;
    updateControls();
  }
}

function voiceSegmentStatusSnapshot(draft) {
  return new Map((draft?.voice_segments || []).map((segment) => [
    segment.id,
    segment.transcription_status,
  ]));
}

function observeVoiceSegmentCompletions(previousStatuses, draft) {
  const statuses = voiceSegmentStatusSnapshot(draft);
  const completedSegmentIds = [];
  for (const [segmentId, status] of statuses) {
    if (
      status === "succeeded" &&
      ["pending", "transcribing"].includes(previousStatuses.get(segmentId))
    ) {
      completedSegmentIds.push(segmentId);
    }
  }
  return { statuses, completedSegmentIds };
}

function applyVoiceCompletionFeedback(
  textarea,
  {
    documentObject = document,
    navigatorObject = navigator,
    schedule = (callback, delay) => window.setTimeout(callback, delay),
  } = {},
) {
  textarea.classList.remove("voice-completion-feedback");
  textarea.classList.add("voice-completion-feedback");
  textarea.scrollTop = textarea.scrollHeight;
  if (
    documentObject.activeElement === textarea &&
    typeof textarea.setSelectionRange === "function"
  ) {
    const textEnd = textarea.value.length;
    textarea.setSelectionRange(textEnd, textEnd);
  }
  const timer = schedule(() => {
    textarea.classList.remove("voice-completion-feedback");
  }, VOICE_COMPLETION_HIGHLIGHT_MS);
  if (documentObject.visibilityState === "visible") {
    try {
      if (typeof navigatorObject?.vibrate === "function") {
        navigatorObject.vibrate(VOICE_COMPLETION_VIBRATION_MS);
      }
    } catch (_) {
      // Haptic completion feedback is best-effort only.
    }
  }
  return timer;
}

function isDraftRevisionConflict(error) {
  return error instanceof ApiError && error.status === 409 &&
    error.message === "Capture Draft revision is stale";
}

async function recoverVoiceUploadRevisionConflict({
  pendingUpload,
  localText,
  loadServerDraft,
  isRetained,
  adoptEquivalent,
  requireChoice,
  retryPendingUpload,
}) {
  const response = await loadServerDraft();
  if (!isRetained(pendingUpload)) return "cancelled";
  const serverDraft = response.draft;
  if (serverDraft && serverDraft.current_text === localText) {
    adoptEquivalent(serverDraft, response.voice_available === true);
    await retryPendingUpload(pendingUpload);
    return "retried";
  }
  requireChoice(serverDraft, response.voice_available === true, pendingUpload);
  return "requires_choice";
}

function voiceAudioMarkup(segmentId, label = "播放原始录音", { preload = "none" } = {}) {
  return `<audio class="voice-audio" controls preload="${preload}" aria-label="${escapeHtml(label)}" src="/api/voice-segments/${segmentId}/audio"></audio>`;
}

function renderCapture() {
  appElement.innerHTML = `
    <div class="capture-view">
      <section class="page-heading capture-heading">
        <h1>先记下来</h1>
      </section>
      <div id="capture-micro-reminders"></div>
      <section class="capture-panel" aria-label="记录内容">
        <form id="capture-form">
          <label class="visually-hidden" for="capture-text">记录内容</label>
          <textarea id="capture-text" name="original_text" maxlength="10000" autofocus placeholder="想到什么，就写下来……"></textarea>
          <div class="voice-capture-row">
            <button id="voice-hold-button" class="voice-hold-button" type="button" hidden>
              <span aria-hidden="true">●</span>
              <span class="voice-hold-label">按住说话</span>
            </button>
            <p id="voice-gesture-hint" class="form-hint" hidden>按住录音；按住时上滑，松手取消。</p>
          </div>
          <div id="voice-segment-list" class="voice-segment-list" aria-live="polite"></div>
          <div id="pending-upload-actions" class="pending-upload-actions" hidden>
            <p>这段录音尚未得到服务器安全确认，暂时保留在本页。</p>
            <button class="secondary-button pending-upload-retry" type="button">重试上传</button>
            <button class="text-button pending-upload-delete" type="button">删除这段录音</button>
          </div>
          <div id="capture-revision-conflict" class="capture-revision-conflict" hidden></div>
          <div class="form-footer">
            <p id="capture-status" class="status-message" role="status">原文会先保存。</p>
            <div class="capture-actions">
              <button class="primary-button capture-submit" type="submit">保存</button>
              <button id="capture-discard" class="text-button" type="button" hidden>丢弃草稿</button>
            </div>
          </div>
        </form>
      </section>
      <section id="upcoming-reminder-section" class="section capture-upcoming" aria-labelledby="upcoming-reminder-heading" hidden>
        <div class="capture-upcoming-heading">
          <h2 id="upcoming-reminder-heading">接下来会提醒</h2>
        </div>
        <div id="upcoming-reminder-list" class="upcoming-reminder-list"></div>
      </section>
      <section id="quick-confirmation-section" class="section" hidden>
        <div class="section-heading">
          <h2>待快速确认</h2>
          <span id="quick-confirmation-count" class="count"></span>
        </div>
        <div id="quick-confirmation-list" class="quick-confirmation-list"></div>
        <p id="quick-confirmation-status" class="status-message" role="status"></p>
      </section>
    </div>`;

  const form = document.querySelector("#capture-form");
  const textarea = document.querySelector("#capture-text");
  const status = document.querySelector("#capture-status");
  const button = form.querySelector(".capture-submit");
  const discardButton = document.querySelector("#capture-discard");
  const voiceButton = document.querySelector("#voice-hold-button");
  const voiceLabel = voiceButton.querySelector(".voice-hold-label");
  const voiceHint = document.querySelector("#voice-gesture-hint");
  const segmentList = document.querySelector("#voice-segment-list");
  const pendingUploadActions = document.querySelector("#pending-upload-actions");
  const revisionConflictPanel = document.querySelector("#capture-revision-conflict");
  const quickSection = document.querySelector("#quick-confirmation-section");
  const upcomingSection = document.querySelector("#upcoming-reminder-section");
  const upcomingList = document.querySelector("#upcoming-reminder-list");
  const captureReminders = document.querySelector("#capture-micro-reminders");
  const quickList = document.querySelector("#quick-confirmation-list");
  const quickCount = document.querySelector("#quick-confirmation-count");
  const quickStatus = document.querySelector("#quick-confirmation-status");
  let quickItems = [];
  const localBuffer = readCaptureBuffer();
  const state = {
    phase: CAPTURE_BROWSER_STATES.BOOTSTRAPPING,
    draft: null,
    dirty: localBuffer.dirty,
    savePending: localBuffer.savePending,
    discardPending: false,
    voiceAvailable: false,
    autosaveTimer: null,
    statusPollTimer: null,
    completionFeedbackTimer: null,
    flushPromise: null,
    pendingUpload: null,
    revisionConflict: null,
    gesture: null,
    disposed: false,
    userTypedDuringBootstrap: false,
    voiceSegmentStatuses: new Map(),
  };
  textarea.value = localBuffer.text;

  function setCaptureStatus(message, kind = "") {
    if (state.disposed || !status.isConnected) return;
    status.dataset.kind = kind;
    status.textContent = message;
  }

  function writeSafetyBuffer({ savePending = state.savePending } = {}) {
    writeCaptureDraft(textarea.value, {
      dirty: state.dirty,
      draftId: state.draft?.id ?? localBuffer.draftId,
      revision: state.draft?.revision ?? localBuffer.revision,
      savePending,
    });
  }

  function renderVoiceSegments() {
    const segments = state.draft?.voice_segments || [];
    const prefersWholeDraftDiscard = capturePrefersWholeDraftDiscard({
      draft: state.draft,
      currentText: textarea.value,
      hasPendingUpload: Boolean(state.pendingUpload),
      hasRevisionConflict: Boolean(state.revisionConflict),
      savePending: state.savePending,
    });
    segmentList.innerHTML = segments.map((segment, index) => {
      const statusText = {
        pending: "等待转写",
        transcribing: "正在转写",
        succeeded: "已加入可编辑文字",
        failed: "转写未完成",
      }[segment.transcription_status] || segment.transcription_status;
      const deleteAction = prefersWholeDraftDiscard
        ? ""
        : `<button class="text-button voice-segment-delete" type="button" data-segment-id="${segment.id}">删除录音</button>`;
      const failure = segment.transcription_status === "failed"
        ? `<p class="voice-segment-error">${escapeHtml(voiceSegmentFailureCopy(segment))}</p>
           <div class="voice-segment-actions">
             <button class="primary-button voice-segment-retry" type="button" data-segment-id="${segment.id}">重试</button>
             ${deleteAction}
           </div>`
        : "";
      return `<article class="voice-segment" data-status="${escapeHtml(segment.transcription_status)}">
        <div class="voice-segment-heading">
          <strong>录音 ${index + 1}</strong>
          <span>${escapeHtml(statusText)}</span>
        </div>
        ${voiceAudioMarkup(segment.id, `播放第 ${index + 1} 段原始录音`, { preload: "metadata" })}
        ${failure}
      </article>`;
    }).join("");
  }

  function renderRevisionConflict() {
    if (!state.revisionConflict) {
      revisionConflictPanel.hidden = true;
      revisionConflictPanel.innerHTML = "";
      return;
    }
    const serverDraft = state.revisionConflict.serverDraft;
    revisionConflictPanel.hidden = false;
    revisionConflictPanel.innerHTML = `
      <strong>草稿在其他页面发生了变化</strong>
      <p>${state.pendingUpload
        ? "尚未上传的录音仍保留在本页。请选择文字版本后，将继续上传同一段录音。"
        : "请选择要保留的文字版本；系统不会自动覆盖任一版本。"}</p>
      <div class="revision-conflict-comparison">
        <div><span>本页文字</span><pre>${escapeHtml(textarea.value)}</pre></div>
        <div><span>另一页面保存的文字</span><pre>${escapeHtml(serverDraft?.current_text ?? "（另一页面的草稿已不存在）")}</pre></div>
      </div>
      <div class="revision-conflict-actions">
        <button class="secondary-button conflict-keep-local" type="button">保留本页文字并继续</button>
        ${serverDraft ? '<button class="secondary-button conflict-use-server" type="button">使用另一页面文字并继续</button>' : ""}
      </div>`;
  }

  function updateCaptureControls() {
    const hasActive = captureHasActiveSegment(state.draft);
    const hasFailed = captureHasFailedSegment(state.draft);
    const transient = [
      CAPTURE_BROWSER_STATES.REQUESTING_MIC,
      CAPTURE_BROWSER_STATES.RECORDING,
      CAPTURE_BROWSER_STATES.CANCEL_ARMED,
      CAPTURE_BROWSER_STATES.FINALIZING,
      CAPTURE_BROWSER_STATES.UPLOADING,
      CAPTURE_BROWSER_STATES.SAVING,
    ].includes(state.phase);
    textarea.disabled = state.savePending || hasActive || transient;
    voiceButton.hidden = !state.voiceAvailable;
    voiceHint.hidden = !state.voiceAvailable;
    voiceButton.disabled = state.savePending || !state.voiceAvailable || hasActive || hasFailed || transient ||
      state.phase === CAPTURE_BROWSER_STATES.BOOTSTRAPPING || Boolean(state.pendingUpload);
    button.disabled = transient || hasActive || hasFailed || Boolean(state.pendingUpload) ||
      Boolean(state.revisionConflict) ||
      !textarea.value.trim();
    const discardControl = captureDiscardControl({
      draft: state.draft,
      hasText: Boolean(textarea.value),
      hasPendingUpload: Boolean(state.pendingUpload),
      savePending: state.savePending,
      discardPending: state.discardPending,
    });
    discardButton.hidden = discardControl.hidden;
    discardButton.disabled = discardControl.disabled;
    pendingUploadActions.hidden = !state.pendingUpload;
    pendingUploadActions.querySelector(".pending-upload-retry").disabled =
      Boolean(state.revisionConflict) || state.phase === CAPTURE_BROWSER_STATES.UPLOADING;
    renderRevisionConflict();
    voiceButton.dataset.state = state.phase;
    voiceLabel.textContent = state.phase === CAPTURE_BROWSER_STATES.REQUESTING_MIC
      ? "正在请求麦克风…"
      : state.phase === CAPTURE_BROWSER_STATES.RECORDING
        ? "松手完成 · 上滑取消"
        : state.phase === CAPTURE_BROWSER_STATES.CANCEL_ARMED
          ? "松手取消"
          : state.phase === CAPTURE_BROWSER_STATES.FINALIZING
            ? "正在结束录音…"
            : state.phase === CAPTURE_BROWSER_STATES.UPLOADING
              ? "正在安全保存…"
              : "按住说话";
    renderVoiceSegments();
  }

  function scheduleDraftPoll() {
    if (state.statusPollTimer) window.clearTimeout(state.statusPollTimer);
    if (state.disposed || !captureHasActiveSegment(state.draft)) return;
    state.statusPollTimer = window.setTimeout(() => {
      state.statusPollTimer = null;
      void refreshDraft({ polling: true });
    }, CAPTURE_STATUS_POLL_DELAY_MS);
  }

  async function refreshDraft({ polling = false } = {}) {
    try {
      const response = await api("/api/capture-draft");
      if (state.disposed) return null;
      state.voiceAvailable = response.voice_available === true;
      const completion = observeVoiceSegmentCompletions(
        state.voiceSegmentStatuses,
        response.draft,
      );
      state.voiceSegmentStatuses = completion.statuses;
      if (response.draft) {
        state.draft = response.draft;
        if (!state.dirty && !state.savePending) {
          textarea.value = response.draft.current_text;
          clearCaptureDraft();
        }
      } else if (!state.savePending) {
        state.draft = null;
      }
      const active = captureHasActiveSegment(state.draft);
      if (polling || active) {
        state.phase = active ? CAPTURE_BROWSER_STATES.POLLING : CAPTURE_BROWSER_STATES.OPEN;
      }
      if (!active && captureHasFailedSegment(state.draft)) {
        setCaptureStatus("请先处理未完成的录音，再保存。", "error");
      } else if (!active && polling) {
        setCaptureStatus("转写已加入文本，你可以继续编辑后保存。", "success");
      }
      updateCaptureControls();
      if (
        completion.completedSegmentIds.length > 0 &&
        response.draft &&
        textarea.value === response.draft.current_text
      ) {
        if (state.completionFeedbackTimer) {
          window.clearTimeout(state.completionFeedbackTimer);
        }
        state.completionFeedbackTimer = applyVoiceCompletionFeedback(textarea);
      }
      scheduleDraftPoll();
      return state.draft;
    } catch (error) {
      if (!state.disposed) {
        setCaptureStatus(`暂时无法刷新草稿：${error.message}`, "error");
        scheduleDraftPoll();
      }
      return null;
    }
  }

  function scheduleAutosave() {
    if (state.autosaveTimer) window.clearTimeout(state.autosaveTimer);
    if (state.savePending || state.disposed) return;
    state.autosaveTimer = window.setTimeout(() => {
      state.autosaveTimer = null;
      void flushDraft({ quiet: true });
    }, CAPTURE_AUTOSAVE_DELAY_MS);
  }

  async function flushDraft({ quiet = false, ensureDraft = false } = {}) {
    if (state.disposed) return state.draft;
    if (state.revisionConflict) {
      if (!quiet) {
        setCaptureStatus("请先明确选择本页或另一页面保存的文字。", "error");
      }
      return state.draft;
    }
    if (state.flushPromise) {
      await state.flushPromise;
      if (!state.dirty) return state.draft;
    }
    if (state.savePending) {
      if (!quiet) setCaptureStatus("上次保存结果待确认，请再次点“保存”。", "error");
      return state.draft;
    }
    const snapshot = textarea.value;
    if (!state.dirty && state.draft) return state.draft;
    if (!ensureDraft && !state.draft && !snapshot && !state.dirty) return null;
    const expectedRevision = state.draft?.revision || 0;
    writeSafetyBuffer();
    if (!quiet) setCaptureStatus("正在安全保存草稿…");
    state.flushPromise = (async () => {
      try {
        const response = await api("/api/capture-draft", {
          method: "PUT",
          body: JSON.stringify({ current_text: snapshot, revision: expectedRevision }),
        });
        if (state.disposed) return response.draft;
        state.draft = response.draft;
        state.voiceAvailable = response.voice_available === true;
        if (textarea.value === snapshot) {
          state.dirty = false;
          clearCaptureDraft();
        } else {
          state.dirty = true;
          writeSafetyBuffer();
          scheduleAutosave();
        }
        if (!(state.gesture && state.phase === CAPTURE_BROWSER_STATES.REQUESTING_MIC)) {
          state.phase = CAPTURE_BROWSER_STATES.OPEN;
        }
        if (!quiet) setCaptureStatus("草稿已保存。", "success");
        return state.draft;
      } catch (error) {
        state.dirty = true;
        writeSafetyBuffer();
        state.phase = error instanceof ApiError && error.status === 409
          ? CAPTURE_BROWSER_STATES.REVISION_CONFLICT
          : CAPTURE_BROWSER_STATES.DIRTY;
        setCaptureStatus(
          error instanceof ApiError && error.status === 409
            ? "草稿已在其他页面更新；本页未确认文字仍保留，请刷新后手动取舍。"
            : `草稿尚未同步；本页文字仍保留：${error.message}`,
          "error",
        );
        throw error;
      } finally {
        state.flushPromise = null;
        updateCaptureControls();
      }
    })();
    return state.flushPromise;
  }

  async function bootstrapDraft() {
    try {
      const response = await api("/api/capture-draft");
      if (state.disposed) return;
      state.voiceAvailable = response.voice_available === true;
      const serverDraft = response.draft;
      if (serverDraft) {
        state.draft = serverDraft;
        const localMatchesServer = localBuffer.text === serverDraft.current_text;
        const pendingSaveMatchesServer = localBuffer.savePending &&
          localBuffer.draftId === serverDraft.id &&
          localBuffer.revision === serverDraft.revision &&
          localMatchesServer;
        if (!state.userTypedDuringBootstrap && pendingSaveMatchesServer) {
          textarea.value = localBuffer.text;
          state.dirty = false;
          state.savePending = true;
          setCaptureStatus("上次保存结果待确认；请再次点“保存”安全确认。", "error");
        } else if (!state.userTypedDuringBootstrap && (!localBuffer.dirty || localMatchesServer)) {
          textarea.value = serverDraft.current_text;
          state.dirty = false;
          state.savePending = false;
          clearCaptureDraft();
        } else {
          state.dirty = true;
          setCaptureStatus("已恢复本页未确认文字；另一页面保存的草稿未被自动覆盖。", "error");
        }
      } else if (localBuffer.savePending && localBuffer.draftId) {
        state.draft = {
          id: localBuffer.draftId,
          revision: localBuffer.revision,
          current_text: localBuffer.text,
          voice_segments: [],
        };
        state.savePending = true;
        setCaptureStatus("上次保存结果待确认；请再次点“保存”安全确认。", "error");
      } else {
        state.draft = null;
        state.dirty = localBuffer.dirty || state.userTypedDuringBootstrap;
      }
      state.voiceSegmentStatuses = voiceSegmentStatusSnapshot(state.draft);
      state.phase = captureHasActiveSegment(state.draft)
        ? CAPTURE_BROWSER_STATES.POLLING
        : state.dirty
          ? CAPTURE_BROWSER_STATES.DIRTY
          : CAPTURE_BROWSER_STATES.OPEN;
      updateCaptureControls();
      if (state.dirty && !state.savePending) scheduleAutosave();
      scheduleDraftPoll();
    } catch (error) {
      state.phase = state.dirty ? CAPTURE_BROWSER_STATES.DIRTY : CAPTURE_BROWSER_STATES.OPEN;
      setCaptureStatus(`暂时无法读取已保存草稿；本页文字仍保留：${error.message}`, "error");
      updateCaptureControls();
    }
  }

  function stopTracks(stream) {
    stream?.getTracks?.().forEach((track) => track.stop());
  }

  function abandonVoiceGesture(gesture, message) {
    if (gesture.limitTimer) window.clearTimeout(gesture.limitTimer);
    stopTracks(gesture.stream);
    if (state.gesture === gesture) state.gesture = null;
    state.phase = CAPTURE_BROWSER_STATES.OPEN;
    if (!state.disposed) {
      setCaptureStatus(message, "error");
      updateCaptureControls();
    }
  }

  function finishRecording(discard = false) {
    const gesture = state.gesture;
    if (!gesture?.recorder || gesture.recorder.state === "inactive") return;
    if (gesture.limitTimer) window.clearTimeout(gesture.limitTimer);
    gesture.discard = discard;
    state.phase = CAPTURE_BROWSER_STATES.FINALIZING;
    updateCaptureControls();
    gesture.recorder.stop();
  }

  async function uploadRecording(
    blob,
    clientSegmentId,
    { allowConflictRecovery = true } = {},
  ) {
    const existingPending = state.pendingUpload;
    const pendingUpload = existingPending &&
      existingPending.blob === blob &&
      existingPending.clientSegmentId === clientSegmentId
      ? existingPending
      : { blob, clientSegmentId };
    state.pendingUpload = pendingUpload;
    state.phase = CAPTURE_BROWSER_STATES.UPLOADING;
    updateCaptureControls();
    setCaptureStatus("正在安全保存原始录音…");
    try {
      await flushDraft({ ensureDraft: true });
      if (!state.draft) throw new Error("草稿尚未就绪");
      const uploadedSegment = await rawApi(
        `/api/capture-draft/voice-segments/${encodeURIComponent(clientSegmentId)}?revision=${state.draft.revision}`,
        {
          method: "PUT",
          body: blob,
          headers: { "Content-Type": blob.type || "application/octet-stream" },
        },
      );
      state.voiceSegmentStatuses.set(
        uploadedSegment.id,
        uploadedSegment.transcription_status,
      );
      if (state.pendingUpload === pendingUpload) state.pendingUpload = null;
      state.revisionConflict = null;
      state.phase = CAPTURE_BROWSER_STATES.POLLING;
      setCaptureStatus("原始录音已安全保存，正在转写…", "success");
      await refreshDraft({ polling: true });
    } catch (error) {
      if (
        allowConflictRecovery &&
        isDraftRevisionConflict(error) &&
        state.pendingUpload === pendingUpload
      ) {
        state.phase = CAPTURE_BROWSER_STATES.UPLOADING;
        updateCaptureControls();
        try {
          const outcome = await recoverVoiceUploadRevisionConflict({
            pendingUpload,
            localText: textarea.value,
            loadServerDraft: () => api("/api/capture-draft"),
            isRetained: (candidate) => state.pendingUpload === candidate,
            adoptEquivalent: (serverDraft, voiceAvailable) => {
              state.draft = serverDraft;
              state.voiceAvailable = voiceAvailable;
              state.dirty = false;
              state.revisionConflict = null;
              clearCaptureDraft();
            },
            requireChoice: (serverDraft, voiceAvailable) => {
              if (state.autosaveTimer) window.clearTimeout(state.autosaveTimer);
              state.autosaveTimer = null;
              state.draft = serverDraft;
              state.voiceAvailable = voiceAvailable;
              state.dirty = true;
              state.revisionConflict = { serverDraft };
              state.phase = CAPTURE_BROWSER_STATES.REVISION_CONFLICT;
              writeSafetyBuffer();
              setCaptureStatus(
                "另一页面保存的草稿与本页文字不同；录音仍保留，请明确选择文字版本。",
                "error",
              );
              updateCaptureControls();
            },
            retryPendingUpload: (retained) => uploadRecording(
              retained.blob,
              retained.clientSegmentId,
              { allowConflictRecovery: false },
            ),
          });
          if (outcome !== "cancelled") return;
        } catch (recoveryError) {
          error = recoveryError;
        }
      }
      state.phase = CAPTURE_BROWSER_STATES.OPEN;
      setCaptureStatus(`录音尚未保存成功，已保留在本页：${error.message}`, "error");
      updateCaptureControls();
    }
  }

  async function beginVoiceGesture(event) {
    if (voiceButton.disabled || state.gesture) return;
    if (!navigator.mediaDevices?.getUserMedia || typeof MediaRecorder === "undefined") {
      setCaptureStatus("此浏览器暂不支持录音；仍可继续输入文字。", "error");
      return;
    }
    event.preventDefault();
    const gesture = {
      pointerId: event.pointerId,
      startY: event.clientY,
      released: false,
      cancelArmed: false,
      recorder: null,
      stream: null,
      chunks: [],
      discard: false,
      limitTimer: null,
    };
    state.gesture = gesture;
    voiceButton.setPointerCapture?.(event.pointerId);
    state.phase = CAPTURE_BROWSER_STATES.REQUESTING_MIC;
    updateCaptureControls();
    setCaptureStatus("正在请求麦克风权限…");
    // Permission is requested synchronously from pointerdown's user gesture.
    let mediaPromise;
    try {
      mediaPromise = navigator.mediaDevices.getUserMedia({ audio: true });
    } catch (_) {
      abandonVoiceGesture(gesture, "浏览器无法请求麦克风；仍可继续输入文字。");
      return;
    }
    const flushPromise = flushDraft({ ensureDraft: true });
    const [mediaResult, flushResult] = await Promise.allSettled([
      mediaPromise,
      flushPromise,
    ]);
    if (mediaResult.status !== "fulfilled") {
      state.gesture = null;
      state.phase = CAPTURE_BROWSER_STATES.OPEN;
      setCaptureStatus("没有获得麦克风权限；仍可继续输入文字。", "error");
      updateCaptureControls();
      return;
    }
    const stream = mediaResult.value;
    if (
      flushResult.status !== "fulfilled" || gesture.released ||
      gesture.cancelArmed || state.disposed || state.gesture !== gesture
    ) {
      stopTracks(stream);
      state.gesture = null;
      state.phase = CAPTURE_BROWSER_STATES.OPEN;
      if (!state.disposed) {
        setCaptureStatus(gesture.released ? "已取消录音。" : "草稿未保存，未开始录音。", gesture.released ? "" : "error");
        updateCaptureControls();
      }
      return;
    }
    gesture.stream = stream;
    let recorder;
    try {
      recorder = new MediaRecorder(stream);
    } catch (_) {
      abandonVoiceGesture(
        gesture,
        "浏览器无法使用默认录音格式；草稿原文已保留。",
      );
      return;
    }
    gesture.recorder = recorder;
    recorder.addEventListener("dataavailable", (dataEvent) => {
      if (dataEvent.data?.size) gesture.chunks.push(dataEvent.data);
    });
    recorder.addEventListener("error", () => {
      gesture.failureMessage = "录音过程发生错误，没有上传不完整音频。";
      if (recorder.state === "inactive") {
        abandonVoiceGesture(gesture, gesture.failureMessage);
      } else {
        finishRecording(true);
      }
    }, { once: true });
    recorder.addEventListener("stop", async () => {
      stopTracks(stream);
      if (gesture.limitTimer) window.clearTimeout(gesture.limitTimer);
      const shouldDiscard = gesture.discard || state.disposed;
      const chunks = gesture.chunks;
      const mediaType = recorder.mimeType || chunks[0]?.type || "application/octet-stream";
      state.gesture = null;
      state.phase = CAPTURE_BROWSER_STATES.OPEN;
      if (shouldDiscard) {
        if (!state.disposed) {
          setCaptureStatus(
            gesture.failureMessage || "录音已取消，没有上传。",
            gesture.failureMessage ? "error" : "success",
          );
          updateCaptureControls();
        }
        return;
      }
      const blob = new Blob(chunks, { type: mediaType });
      if (!blob.size) {
        setCaptureStatus("没有录到可上传的音频；请重试。", "error");
        updateCaptureControls();
        return;
      }
      const clientSegmentId = globalThis.crypto?.randomUUID?.() ||
        `voice-${Date.now()}-${Math.random().toString(16).slice(2)}`;
      await uploadRecording(blob, clientSegmentId);
    }, { once: true });
    try {
      recorder.start();
    } catch (_) {
      abandonVoiceGesture(
        gesture,
        "浏览器无法启动录音；草稿原文已保留。",
      );
      return;
    }
    gesture.limitTimer = window.setTimeout(() => {
      gesture.released = true;
      finishRecording(false);
    }, VOICE_MAX_RECORDING_MS);
    state.phase = CAPTURE_BROWSER_STATES.RECORDING;
    setCaptureStatus("正在录音；松手完成，上滑后松手取消。");
    updateCaptureControls();
  }

  function releaseVoiceGesture(event, forceCancel = false) {
    const gesture = state.gesture;
    if (!gesture || event.pointerId !== gesture.pointerId || gesture.released) return;
    gesture.released = true;
    if (forceCancel) gesture.cancelArmed = true;
    try {
      voiceButton.releasePointerCapture?.(event.pointerId);
    } catch (_) {
      // Capture may already have been released by the browser.
    }
    if (gesture.recorder) finishRecording(gesture.cancelArmed);
  }

  textarea.addEventListener("input", () => {
    state.userTypedDuringBootstrap = state.phase === CAPTURE_BROWSER_STATES.BOOTSTRAPPING;
    state.dirty = true;
    if (state.revisionConflict) {
      state.phase = CAPTURE_BROWSER_STATES.REVISION_CONFLICT;
      setCaptureStatus("本页文字已更新；请选择要保留的文字版本。", "error");
    } else if (state.savePending) {
      state.savePending = false;
      state.phase = CAPTURE_BROWSER_STATES.REVISION_CONFLICT;
      setCaptureStatus("上次保存结果尚未确认；新文字只保留在本页，请先刷新确认。", "error");
    } else {
      state.phase = CAPTURE_BROWSER_STATES.DIRTY;
      setCaptureStatus("正在保存草稿…");
      scheduleAutosave();
    }
    writeSafetyBuffer();
    updateCaptureControls();
  });
  textarea.addEventListener("blur", () => {
    void flushDraft({ quiet: true });
  });
  voiceButton.addEventListener("pointerdown", (event) => {
    if (event.pointerType === "mouse" && event.button !== 0) return;
    void beginVoiceGesture(event);
  });
  voiceButton.addEventListener("pointermove", (event) => {
    const gesture = state.gesture;
    if (!gesture || gesture.pointerId !== event.pointerId || gesture.released) return;
    gesture.cancelArmed = voiceGestureCancelArmed(gesture.startY, event.clientY);
    if (gesture.recorder) {
      state.phase = gesture.cancelArmed
        ? CAPTURE_BROWSER_STATES.CANCEL_ARMED
        : CAPTURE_BROWSER_STATES.RECORDING;
      updateCaptureControls();
    }
  });
  voiceButton.addEventListener("pointerup", (event) => releaseVoiceGesture(event));
  voiceButton.addEventListener("pointercancel", (event) => releaseVoiceGesture(event, true));
  voiceButton.addEventListener("lostpointercapture", (event) => releaseVoiceGesture(event, true));

  segmentList.addEventListener("click", async (event) => {
    const retry = event.target.closest(".voice-segment-retry");
    const remove = event.target.closest(".voice-segment-delete");
    const target = retry || remove;
    if (!target) return;
    const segmentId = Number(target.dataset.segmentId);
    target.disabled = true;
    if (remove && !window.confirm("永久删除这段原始录音吗？此操作不可恢复。")) {
      target.disabled = false;
      return;
    }
    try {
      if (retry) {
        const retriedSegment = await api(`/api/voice-segments/${segmentId}/retry`, {
          method: "POST",
        });
        state.voiceSegmentStatuses.set(
          retriedSegment.id,
          retriedSegment.transcription_status,
        );
        setCaptureStatus("正在重试转写…");
      } else {
        await api(`/api/voice-segments/${segmentId}`, { method: "DELETE" });
        setCaptureStatus("未完成的录音已删除，可以重新录制。", "success");
      }
      await refreshDraft({ polling: Boolean(retry) });
    } catch (error) {
      setCaptureStatus(`操作未完成：${error.message}`, "error");
      target.disabled = false;
    }
  });

  pendingUploadActions.querySelector(".pending-upload-retry").addEventListener("click", () => {
    if (
      state.pendingUpload &&
      !state.revisionConflict &&
      state.phase !== CAPTURE_BROWSER_STATES.UPLOADING
    ) {
      void uploadRecording(state.pendingUpload.blob, state.pendingUpload.clientSegmentId);
    }
  });
  pendingUploadActions.querySelector(".pending-upload-delete").addEventListener("click", () => {
    if (!window.confirm("删除这段尚未上传的录音吗？此操作不可恢复。")) return;
    state.pendingUpload = null;
    setCaptureStatus(
      state.revisionConflict
        ? "未上传录音已删除；仍需选择要保留的草稿文字。"
        : "未上传录音已删除。",
      state.revisionConflict ? "error" : "success",
    );
    updateCaptureControls();
  });

  revisionConflictPanel.addEventListener("click", (event) => {
    const keepLocal = event.target.closest(".conflict-keep-local");
    const useServer = event.target.closest(".conflict-use-server");
    if ((!keepLocal && !useServer) || !state.revisionConflict) return;
    const pendingUpload = state.pendingUpload;
    const serverDraft = state.revisionConflict.serverDraft;
    if (useServer) {
      if (!serverDraft) return;
      textarea.value = serverDraft.current_text;
      state.draft = serverDraft;
      state.dirty = false;
      clearCaptureDraft();
    } else {
      state.draft = serverDraft;
      state.dirty = true;
      writeSafetyBuffer();
    }
    state.revisionConflict = null;
    state.phase = pendingUpload || state.dirty
      ? CAPTURE_BROWSER_STATES.DIRTY
      : CAPTURE_BROWSER_STATES.OPEN;
    updateCaptureControls();
    if (pendingUpload) {
      void uploadRecording(pendingUpload.blob, pendingUpload.clientSegmentId);
    } else if (keepLocal) {
      void flushDraft();
    } else {
      setCaptureStatus("已采用另一页面保存的草稿。", "success");
    }
  });

  discardButton.addEventListener("click", async () => {
    if (state.discardPending) return;
    if (!window.confirm("永久丢弃当前草稿、未保存文字和其中的原始录音吗？")) return;
    if (state.pendingUpload) {
      state.pendingUpload = null;
    }
    state.revisionConflict = null;
    await runWithCaptureDiscardPending({
      state,
      updateControls: updateCaptureControls,
      operation: async () => {
        try {
          if (state.draft && !state.savePending) {
            await api(`/api/capture-draft?revision=${state.draft.revision}`, {
              method: "DELETE",
            });
          }
          state.draft = null;
          state.voiceSegmentStatuses = new Map();
          state.dirty = false;
          state.savePending = false;
          textarea.value = "";
          clearCaptureDraft();
          setCaptureStatus("草稿已丢弃。", "success");
        } catch (error) {
          setCaptureStatus(
            isDraftRevisionConflict(error)
              ? "草稿已在其他页面变化，本页文字仍保留。请刷新后再取舍。"
              : `草稿未丢弃，本页文字仍保留：${error.message}`,
            "error",
          );
        }
      },
    });
  });

  const visibilityHandler = () => {
    if (document.visibilityState === "hidden") {
      writeSafetyBuffer();
      void flushDraft({ quiet: true });
    }
  };
  const beforeUnloadHandler = (event) => {
    writeSafetyBuffer();
    if (state.pendingUpload || state.revisionConflict) {
      event.preventDefault();
      event.returnValue = "";
    }
  };
  document.addEventListener("visibilitychange", visibilityHandler);
  window.addEventListener("beforeunload", beforeUnloadHandler);

  activeCaptureController = {
    location: `${window.location.pathname}${window.location.search}`,
    flush: flushDraft,
    deferSessionExpiredRedirect() {
      if (!state.pendingUpload && !state.gesture) return false;
      setCaptureStatus(
        "登录状态已失效；尚未确认的录音仍保留在本页，请先重试或明确删除。",
        "error",
      );
      return true;
    },
    async prepareNavigation() {
      if (state.pendingUpload || state.revisionConflict) {
        setCaptureStatus(
          state.pendingUpload
            ? "录音尚未安全上传；请先重试上传或明确删除。"
            : "草稿文字冲突尚未解决；请先明确选择文字版本。",
          "error",
        );
        return false;
      }
      await flushDraft({ quiet: true }).catch(() => null);
      return true;
    },
    dispose() {
      state.disposed = true;
      if (state.autosaveTimer) window.clearTimeout(state.autosaveTimer);
      if (state.statusPollTimer) window.clearTimeout(state.statusPollTimer);
      if (state.completionFeedbackTimer) {
        window.clearTimeout(state.completionFeedbackTimer);
      }
      if (state.gesture?.recorder && state.gesture.recorder.state !== "inactive") {
        state.gesture.discard = true;
        state.gesture.recorder.stop();
      } else {
        stopTracks(state.gesture?.stream);
      }
      document.removeEventListener("visibilitychange", visibilityHandler);
      window.removeEventListener("beforeunload", beforeUnloadHandler);
    },
  };

  function renderQuickConfirmation() {
    const items = quickItems.filter(
      (item) => item.reminder?.status === "needs_confirmation",
    );
    quickSection.hidden = items.length === 0;
    quickCount.textContent = items.length;
    quickList.innerHTML = items.map(quickConfirmationCard).join("");
  }

  function renderUpcomingReminders() {
    const markup = upcomingReminderList(quickItems);
    upcomingList.innerHTML = markup;
    upcomingSection.hidden = !markup;
  }

  async function loadQuickConfirmation() {
    if (pollTimer) window.clearTimeout(pollTimer);
    pollTimer = null;
    try {
      const data = await api("/api/items");
      if (!quickSection.isConnected) return;
      captureReminders.innerHTML = inAppReminderList(data.due_reminders);
      markRenderedRemindersSurfaced(data.due_reminders);
      quickItems = [...data.sortable_items, ...data.needs_confirmation];
      notificationOnboardingHasReminder = quickItems.some((item) =>
        reminderIsNotificationEligible(item.reminder)
      ) || data.due_reminders.length > 0;
      renderUpcomingReminders();
      renderQuickConfirmation();
      renderNotificationOnboarding();
      if (data.pending_inputs.length) {
        pollTimer = window.setTimeout(loadQuickConfirmation, 2500);
      }
    } catch (_) {
      // Capture remains fully usable if the optional queue cannot be loaded.
    }
  }

  quickList.addEventListener("click", async (event) => {
    const reminderChoice = event.target.closest(
      ".reminder-decline-button, .reminder-set-button",
    );
    if (reminderChoice) {
      const item = quickItems.find(
        (candidate) => candidate.id === Number(reminderChoice.dataset.itemId),
      );
      if (!item) return;
      if (reminderChoice.classList.contains("reminder-set-button")) {
        openReminderEditor({
          item,
          reminder: item.reminder,
          onSaved: async (saved) => {
            item.reminder = saved;
            quickStatus.dataset.kind = "success";
            quickStatus.textContent = "微提醒已设置。";
            renderUpcomingReminders();
            renderQuickConfirmation();
          },
        });
        return;
      }
      if (reminderChoice.classList.contains("reminder-decline-button")) {
        reminderChoice.disabled = true;
        quickStatus.dataset.kind = "";
        quickStatus.textContent = "正在保存选择…";
        try {
          item.reminder = await api(`/api/reminders/${item.reminder.id}`, {
            method: "DELETE",
          });
          quickStatus.dataset.kind = "success";
          quickStatus.textContent = "已记住：暂时不用提醒。";
          renderQuickConfirmation();
        } catch (error) {
          quickStatus.dataset.kind = "error";
          quickStatus.textContent = `选择未保存：${error.message}`;
          reminderChoice.disabled = false;
        }
        return;
      }
      return;
    }
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    if (!textarea.value.trim()) return;
    if (
      state.pendingUpload || state.revisionConflict || state.gesture ||
      captureHasActiveSegment(state.draft) ||
      captureHasFailedSegment(state.draft)
    ) {
      setCaptureStatus("请先完成、重试或明确删除当前录音，再保存。", "error");
      return;
    }
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.textContent = "保存中…";
    setCaptureStatus("正在保存…");
    try {
      if (!state.savePending) {
        await flushDraft({ ensureDraft: true });
      }
      if (!state.draft) {
        throw new Error("草稿尚未建立；本页文字仍保留。");
      }
      state.phase = CAPTURE_BROWSER_STATES.SAVING;
      state.savePending = true;
      state.dirty = false;
      writeSafetyBuffer({ savePending: true });
      updateCaptureControls();
      await api("/api/capture-draft/save", {
        method: "POST",
        body: JSON.stringify({
          draft_id: state.draft.id,
          revision: state.draft.revision,
        }),
      });
      state.draft = null;
      state.dirty = false;
      state.savePending = false;
      state.phase = CAPTURE_BROWSER_STATES.OPEN;
      textarea.value = "";
      clearCaptureDraft();
      status.dataset.kind = "success";
      status.innerHTML = '<span class="save-feedback"><strong>已保存</strong><span>正在整理</span></span>';
      textarea.focus();
      void loadQuickConfirmation();
      window.setTimeout(() => {
        if (status.isConnected && status.dataset.kind === "success") {
          status.dataset.kind = "";
          status.textContent = "原文会先保存。";
        }
      }, 1800);
    } catch (error) {
      state.phase = CAPTURE_BROWSER_STATES.OPEN;
      const definiteRejection = error instanceof ApiError &&
        [404, 409, 422].includes(error.status);
      if (definiteRejection) {
        state.savePending = false;
        writeSafetyBuffer({ savePending: false });
        await refreshDraft();
      } else if (state.savePending) {
        writeSafetyBuffer({ savePending: true });
      }
      setCaptureStatus(`保存结果尚未确认，输入仍保留：${error.message}`, "error");
    } finally {
      button.removeAttribute("aria-busy");
      button.textContent = "保存";
      updateCaptureControls();
    }
  });
  void bootstrapDraft();
  void loadQuickConfirmation();
}

function inputStatusCard(input, failed = false) {
  const failureDisplay = captureFailureDisplay(input);
  const originalAudio = failed && input.voice_segment_ids?.length
    ? `<div class="saved-capture-audio">
        <strong>原始录音</strong>
        ${input.voice_segment_ids.map((segmentId, index) =>
          voiceAudioMarkup(segmentId, `播放未整理记录的第 ${index + 1} 段原始录音`)
        ).join("")}
      </div>`
    : "";
  return `
    <article class="status-card ${failed ? "failed" : "pending"}">
      <p class="original-preview">${escapeHtml(input.original_text)}</p>
      <p class="muted">${failed ? "整理失败，原文已安全保存。" : "原文已保存，正在整理。"}</p>
      ${failed ? `<div class="failure-reason"><span class="failure-kind">${escapeHtml(failureDisplay.title)}</span><span>${escapeHtml(failureDisplay.message)}</span></div>` : ""}
      ${originalAudio}
      ${failed ? `<div class="failed-capture-actions">
        <button class="secondary-button retry-button" data-input-id="${input.id}">重新整理</button>
        <button class="text-button danger-button delete-input-button" data-input-id="${input.id}">永久删除这条记录</button>
      </div>` : ""}
    </article>`;
}

function inputHistoryItem(input) {
  const failed = input.processing_status === "failed";
  const failureDisplay = captureFailureDisplay(input);
  return `
    <li>
      ${escapeHtml(input.original_text)}
      <span class="history-meta">${formatTime(input.created_time)} · ${labels[input.processing_status]}</span>
      ${input.voice_segment_ids?.length ? `<div class="history-audio-list">
        ${input.voice_segment_ids.map((segmentId, index) =>
          voiceAudioMarkup(segmentId, `播放第 ${index + 1} 段原始录音`)
        ).join("")}
      </div>` : ""}
      ${failed ? `<span class="history-error">${escapeHtml(failureDisplay.title)}：${escapeHtml(failureDisplay.message)}</span>` : ""}
      ${failed ? `<button class="secondary-button detail-retry-button" data-input-id="${input.id}">重新整理这条记录</button>` : ""}
    </li>`;
}

function inAppReminderList(reminders) {
  if (!reminders.length) return "";
  return `
    <section class="micro-reminder-section" aria-label="到了时间的微提醒">
      ${reminders.map((reminder) => `
        <article class="micro-reminder" data-reminder-id="${reminder.id}">
          <p>提醒一下：你之前希望现在记起这件事：</p>
          <a href="/items/${reminder.item_id}" data-link>${escapeHtml(reminder.item_title)}</a>
          <span>${escapeHtml(reminderExactTime(reminder.remind_at))}</span>
        </article>`).join("")}
    </section>`;
}

function markRenderedRemindersSurfaced(reminders) {
  const rendered = reminders.filter((reminder) =>
    document.querySelector(`.micro-reminder[data-reminder-id="${reminder.id}"]`),
  );
  if (!rendered.length) return;
  void Promise.allSettled(
    rendered.map((reminder) =>
      api(`/api/reminders/${reminder.id}/surface`, { method: "POST" }),
    ),
  );
}

async function renderDashboard({ silent = false, focusIntent = null } = {}) {
  if (!silent) {
    appElement.innerHTML = '<p class="loading">正在读取事项…</p>';
  }
  try {
    const selectedStatus = selectedDashboardStatus();
    const requestedPage = selectedDashboardPage();
    reconcileDashboardSelectionView(selectedStatus, requestedPage);
    const selectionGeneration = dashboardSelection.generation;
    const view = lifecycleViews[selectedStatus];
    const data = await api(
      `/api/items?status=${encodeURIComponent(selectedStatus)}&page=${requestedPage}`,
    );
    if (
      selectionGeneration !== dashboardSelection.generation ||
      window.location.pathname !== "/dashboard" ||
      selectedDashboardStatus() !== selectedStatus ||
      selectedDashboardPage() !== requestedPage
    ) return;
    const page = Number.isSafeInteger(data.page) ? data.page : requestedPage;
    const pageSize = Number.isSafeInteger(data.page_size)
      ? data.page_size
      : MAX_BULK_LIFECYCLE_ITEMS;
    const totalPages = Number.isSafeInteger(data.total_pages) ? data.total_pages : 1;
    const activeCount = data.sortable_items.length + data.needs_confirmation.length;
    const inactiveItems = [...data.sortable_items, ...data.needs_confirmation];
    const visibleCount = selectedStatus === "active" ? activeCount : inactiveItems.length;
    const totalItems = Number.isSafeInteger(data.total_items)
      ? data.total_items
      : visibleCount;
    if (visibleCount > pageSize || pageSize > MAX_BULK_LIFECYCLE_ITEMS) {
      throw new Error("事项页面容量与批量操作上限不一致。");
    }
    if (page !== requestedPage) {
      history.replaceState({}, "", dashboardPath(selectedStatus, page));
    }
    const visibleItems = selectedStatus === "active"
      ? [...data.sortable_items, ...data.needs_confirmation]
      : inactiveItems;
    dashboardSelection.visibleIds = visibleItems.map((item) => item.id);
    if (dashboardSelection.active) dashboardSelection.page = page;
    dashboardSelection.ids = new Set(
      [...dashboardSelection.ids].filter((itemId) =>
        dashboardSelection.visibleIds.includes(itemId)
      ),
    );
    const selectionListAttributes = dashboardSelection.active
      ? 'role="listbox" aria-multiselectable="true"'
      : "";
    appElement.innerHTML = `
      <section class="page-heading dashboard-heading ${escapeHtml(selectedStatus)}">
        <div class="dashboard-title-row">
          <h1>${selectedStatus === "trash" ? "回收站" : "事项"}</h1>
          ${selectedStatus === "trash"
            ? '<a class="trash-entry" href="/dashboard" data-link>返回事项</a>'
            : '<a class="trash-entry" href="/dashboard?status=trash" data-link><span aria-hidden="true">🗑</span>回收站</a>'}
        </div>
      </section>

      ${lifecycleNavigation(selectedStatus)}

      <section class="lifecycle-view-heading">
        <div>
          <h2 id="dashboard-view-heading" tabindex="-1" data-dashboard-focus-fallback>${escapeHtml(view.heading)}</h2>
          <span class="dashboard-total" aria-label="${totalItems} 个事项">${totalItems}</span>
        </div>
        <div class="lifecycle-view-actions">
          <button id="selection-entry" class="text-button" type="button" ${visibleCount ? "" : "disabled"}>选择</button>
          ${selectedStatus === "trash" && visibleCount
            ? `<button id="clear-trash-button" class="text-button danger-button" type="button" data-clear-trash-scope="${totalPages === 1 && totalItems === visibleCount ? "all" : "page"}" data-total-items="${totalItems}">${totalPages === 1 && totalItems === visibleCount ? "清空回收站" : "永久删除本页事项"}</button>`
            : ""}
        </div>
      </section>

      ${selectedStatus === "trash"
        ? '<p class="trash-retention-note">事项将在移入回收站 30 天后永久删除。</p>'
        : ""}

      ${dashboardSelectionBar(selectedStatus)}
      <p id="dashboard-action-status" class="status-message" role="status"></p>

      ${selectedStatus === "active" ? inAppReminderList(data.due_reminders) : ""}

      ${selectedStatus === "active" && data.pending_inputs.length ? `
        <section class="section">
          <div class="section-heading"><h2>正在整理</h2><span class="count">${data.pending_inputs.length}</span></div>
          <div class="status-list">${data.pending_inputs.map((item) => inputStatusCard(item)).join("")}</div>
        </section>` : ""}

      ${selectedStatus === "active" && data.failed_inputs.length ? `
        <section class="section">
          <div class="section-heading"><h2>需要重试</h2><span class="count">${data.failed_inputs.length}</span></div>
          <div class="status-list">${data.failed_inputs.map((item) => inputStatusCard(item, true)).join("")}</div>
        </section>` : ""}

      ${selectedStatus === "active" ? `
        ${data.sortable_items.length ? `<section class="section dashboard-primary-section">
          <div class="item-list" ${selectionListAttributes}>${data.sortable_items.map(itemCard).join("")}</div>
        </section>` : ""}
        ${data.needs_confirmation.length ? `<section class="section dashboard-secondary-section">
          <div class="section-heading"><h2>信息待确认</h2><span class="count">${data.needs_confirmation.length}</span></div>
          <div class="item-list" ${selectionListAttributes}>${data.needs_confirmation.map(itemCard).join("")}</div>
        </section>` : ""}
      ` : `
        <section class="section dashboard-primary-section">
          ${inactiveItems.length ? `<div class="lifecycle-list" ${selectionListAttributes}>${inactiveItems.map((item) => lifecycleListRow(item, selectedStatus)).join("")}</div>` : `<div class="empty-state compact-empty-state">${escapeHtml(view.empty)}</div>`}
        </section>`}

      ${selectedStatus === "active" && activeCount === 0 && data.pending_inputs.length === 0 && data.failed_inputs.length === 0 ? `<div class="empty-state compact-empty-state section">${escapeHtml(view.empty)} <a href="/capture" data-link>去记录一条想法</a></div>` : ""}`;

    appElement.insertAdjacentHTML(
      "beforeend",
      dashboardPagination(
        selectedStatus,
        page,
        totalPages,
        totalItems,
        visibleCount,
      ),
    );

    notificationOnboardingHasReminder = selectedStatus === "active" && (
      [...data.sortable_items, ...data.needs_confirmation].some((item) =>
        reminderIsNotificationEligible(item.reminder)
      ) || data.due_reminders.length > 0
    );
    renderNotificationOnboarding();
    bindDashboardSelectionControls(selectedStatus);
    bindDashboardLongPressRows(selectedStatus);

    if (selectedStatus === "active") {
      markRenderedRemindersSurfaced(data.due_reminders);
    }

    document.querySelectorAll(".retry-button").forEach((button) => {
      button.addEventListener("click", async () => {
        button.disabled = true;
        try {
          await api(`/api/inputs/${button.dataset.inputId}/retry`, { method: "POST" });
          await renderDashboard({ silent: true });
        } catch (error) {
          button.disabled = false;
          button.textContent = error.message;
        }
      });
    });

    document.querySelectorAll(".delete-input-button").forEach((button) => {
      button.addEventListener("click", async () => {
        if (!window.confirm("永久删除这条未整理记录、原文和原始录音吗？此操作无法撤销。")) return;
        button.disabled = true;
        try {
          await api(`/api/inputs/${button.dataset.inputId}`, { method: "DELETE" });
          await renderDashboard({ silent: true });
        } catch (error) {
          button.disabled = false;
          button.textContent = error.message;
        }
      });
    });

    restoreDashboardFocus(focusIntent);

    if (
      selectedStatus === "active" &&
      data.pending_inputs.length &&
      !dashboardSelection.active
    ) {
      pollTimer = window.setTimeout(() => renderDashboard({ silent: true }), 2500);
    }
  } catch (error) {
    appElement.innerHTML = `<div class="empty-state section">无法读取事项：${escapeHtml(error.message)}</div>`;
  }
}

function detailField(label, value, full = false, className = "") {
  const classes = [full ? "full" : "", className].filter(Boolean).join(" ");
  return `<div class="${classes}"><dt>${escapeHtml(label)}</dt><dd>${escapeHtml(value ?? "未填写")}</dd></div>`;
}

function supplementalLabel(key) {
  const normalizedKey = String(key)
    .trim()
    .replace(/([a-z0-9])([A-Z])/g, "$1_$2")
    .replaceAll("_", " ")
    .replaceAll("-", " ")
    .replace(/\s+/g, " ")
    .toLowerCase()
    .replaceAll(" ", "_");
  if (supplementalLabels[normalizedKey]) return supplementalLabels[normalizedKey];
  const readable = normalizedKey.replaceAll("_", " ");
  return readable ? `${readable.charAt(0).toUpperCase()}${readable.slice(1)}` : "补充信息";
}

function renderSupplementalValue(value) {
  if (value === null || value === undefined || value === "") {
    return '<span class="supplemental-empty">未填写</span>';
  }
  if (Array.isArray(value)) {
    if (value.length === 0) return '<span class="supplemental-empty">未填写</span>';
    if (value.every((entry) => entry === null || ["string", "number", "boolean"].includes(typeof entry))) {
      return `<span>${value.map((entry) => {
        if (entry === null) return "未填写";
        if (typeof entry === "boolean") return entry ? "是" : "否";
        return escapeHtml(entry);
      }).join("、")}</span>`;
    }
    return `<ul class="supplemental-list">${value.map((entry) => `<li>${renderSupplementalValue(entry)}</li>`).join("")}</ul>`;
  }
  if (typeof value === "object") {
    const entries = Object.entries(value);
    if (entries.length === 0) return '<span class="supplemental-empty">未填写</span>';
    return `<dl class="supplemental-nested">${entries
      .map(([key, entry]) => `<div><dt>${escapeHtml(supplementalLabel(key))}</dt><dd>${renderSupplementalValue(entry)}</dd></div>`)
      .join("")}</dl>`;
  }
  if (typeof value === "boolean") return `<span>${value ? "是" : "否"}</span>`;
  return `<span>${escapeHtml(value)}</span>`;
}

function renderExtraInformation(extraInformation) {
  if (!extraInformation || Object.keys(extraInformation).length === 0) {
    return '<p class="muted">暂无补充信息。</p>';
  }
  return `<dl class="supplemental-grid">${Object.entries(extraInformation)
    .map(([key, value]) => `<div><dt>${escapeHtml(supplementalLabel(key))}</dt><dd>${renderSupplementalValue(value)}</dd></div>`)
    .join("")}</dl>`;
}

function buildEditForm(item) {
  return `
    <form id="edit-form" class="edit-form">
      <div class="full"><label for="edit-title">标题</label><input id="edit-title" value="${escapeHtml(item.title)}" maxlength="200" required /></div>
      <div><label for="edit-type">类型</label><input id="edit-type" value="${escapeHtml(item.type)}" maxlength="50" required /></div>
      <div><label for="edit-deadline">截止日期</label><input id="edit-deadline" type="date" value="${escapeHtml(deadlineTemporalState(item.deadline)?.localDate || "")}" /></div>
      <div><label for="edit-estimate">预计分钟数</label><input id="edit-estimate" type="number" min="1" max="100800" value="${item.estimated_time || ""}" /></div>
      <div class="full"><label for="edit-next">下一步</label><textarea id="edit-next" maxlength="1000">${escapeHtml(item.next_action || "")}</textarea></div>
      <details class="full edit-extra-disclosure">
        <summary>高级编辑补充信息</summary>
        <div class="edit-extra-field">
          <label for="edit-extra">补充信息（JSON）</label>
          <textarea id="edit-extra">${escapeHtml(JSON.stringify(item.extra_information || {}, null, 2))}</textarea>
          <p class="form-hint">仅在需要直接调整结构化补充内容时使用。</p>
        </div>
      </details>
      <div class="form-footer edit-form-footer">
        <p id="edit-status-message" class="status-message" role="status">截止日期变化会要求确认。</p>
        <div class="edit-form-buttons">
          <button class="secondary-button" id="edit-cancel-button" type="button">取消</button>
          <button class="primary-button" type="submit">保存修正</button>
        </div>
      </div>
    </form>`;
}

function changedPatch(item) {
  const candidate = {
    title: document.querySelector("#edit-title").value.trim(),
    type: document.querySelector("#edit-type").value.trim(),
    deadline: document.querySelector("#edit-deadline").value || null,
    estimated_time: document.querySelector("#edit-estimate").value
      ? Number(document.querySelector("#edit-estimate").value)
      : null,
    next_action: document.querySelector("#edit-next").value.trim() || null,
  };
  const patch = {};
  Object.entries(candidate).forEach(([key, value]) => {
    const original = key === "deadline" && item[key] ? deadlineTemporalState(item[key]).localDate : item[key];
    if (value !== original) patch[key] = value;
  });

  const extraText = document.querySelector("#edit-extra").value.trim() || "{}";
  let extra;
  try {
    extra = JSON.parse(extraText);
  } catch (_) {
    throw new Error("补充信息必须是有效的 JSON 对象。 ");
  }
  if (!extra || Array.isArray(extra) || typeof extra !== "object") {
    throw new Error("补充信息必须是 JSON 对象。");
  }
  if (JSON.stringify(extra) !== JSON.stringify(item.extra_information || {})) {
    patch.extra_information = extra;
  }
  return patch;
}

function lifecycleActions(item) {
  if (item.status === "trash") {
    return `
      <button class="primary-button lifecycle-primary lifecycle-status-button" data-status="active">恢复</button>
      <button class="text-button danger-button permanent-delete-button">永久删除</button>`;
  }
  if (item.status === "active") {
    return `
      <button class="primary-button lifecycle-primary lifecycle-status-button" data-status="completed">标记为已完成</button>
      <button class="text-button lifecycle-status-button" data-status="trash">移入回收站</button>`;
  }
  return `
    <button class="primary-button lifecycle-primary lifecycle-status-button" data-status="active">恢复为当前</button>
    <button class="text-button lifecycle-status-button" data-status="trash">移入回收站</button>`;
}

function reminderDetailSection(item, reminder) {
  let body;
  let actions = "";
  if (!reminder) {
    body = '<p class="muted">还没有设置微提醒。需要时再打开即可。</p>';
    if (item.status === "active") {
      actions = '<button class="secondary-button reminder-set-detail-button" type="button">设置提醒</button>';
    }
  } else if (reminder.status === "scheduled") {
    body = `
      <p class="reminder-natural">${escapeHtml(reminderNaturalText(reminder))}</p>
      <p class="reminder-exact">${escapeHtml(reminderExactTime(reminder.remind_at))}</p>`;
    actions = `
      <button class="secondary-button reminder-set-detail-button" type="button">修改时间</button>
      <button class="text-button reminder-cancel-detail-button" type="button">关闭提醒</button>`;
  } else if (reminder.status === "needs_confirmation") {
    body = `
      <p class="reminder-natural">你想之后被提醒，但还没有一个可以安全执行的时间。</p>
      ${reminder.source_expression ? `<p class="reminder-exact">原话里的时间：${escapeHtml(reminder.source_expression)}</p>` : ""}`;
    actions = `
      <button class="secondary-button reminder-set-detail-button" type="button">设置时间</button>
      <button class="text-button reminder-cancel-detail-button" type="button">关闭提醒</button>`;
  } else if (reminder.status === "due") {
    body = `
      <p class="reminder-natural">${escapeHtml(reminderNaturalText(reminder))}</p>
      <p class="reminder-exact">${escapeHtml(reminderExactTime(reminder.remind_at))}</p>
      <p class="muted">这只表示 SelfEcho 已把它重新带回视野。</p>`;
    if (item.status === "active") {
      actions = '<button class="secondary-button reminder-set-detail-button" type="button">再次设置提醒</button>';
    }
  } else {
    body = '<p class="muted">这个微提醒已关闭；记录仍保留。</p>';
    if (item.status === "active") {
      actions = '<button class="secondary-button reminder-set-detail-button" type="button">重新设置提醒</button>';
    }
  }
  return `
    <section class="detail-block reminder-detail-block" data-reminder-status="${escapeHtml(reminder?.status || "off")}">
      <h2>微提醒</h2>
      ${body}
      ${actions ? `<div class="detail-actions reminder-detail-actions">${actions}</div>` : ""}
      <p id="reminder-action-status" class="status-message" role="status"></p>
    </section>`;
}

function detailPinState(item) {
  if (item.status !== "active") {
    return item.is_pinned ? "已保留置顶设置，回到当前事项后生效。" : "未置顶";
  }
  return item.is_pinned ? "已置顶，在当前事项中靠前显示。" : "未置顶，可让这件事在当前事项中靠前显示。";
}

async function renderDetail(
  itemId,
  { silent = false, actionMessage = "", automatic = false } = {},
) {
  if (automatic && detailRefreshBlocked()) {
    scheduleDetailPoll(itemId);
    return;
  }
  if (!silent) appElement.innerHTML = '<p class="loading">正在读取事项…</p>';
  try {
    const data = await api(`/api/items/${itemId}`);
    // A user may start typing while an automatic fetch is in flight. Check
    // again immediately before replacing the DOM so that draft text always wins.
    if (automatic && detailRefreshBlocked()) {
      scheduleDetailPoll(itemId);
      return;
    }
    const item = data.item;
    const backPath = dashboardPath(item.status);
    const backLabel = lifecycleViews[item.status]?.label || "事项";
    const hasProcessing = data.inputs.some((input) => ["pending", "processing"].includes(input.processing_status));
    appElement.innerHTML = `
      <div class="detail-view">
        <a class="back-link" href="${backPath}" data-link>← 返回${escapeHtml(backLabel)}</a>
        <section class="page-heading detail-heading">
          <div class="detail-heading-meta">
            <span class="detail-category">${escapeHtml(itemTypeLabel(item.type))}</span>
            <span class="detail-state">${escapeHtml(labels[item.status])}</span>
          </div>
          <h1>${escapeHtml(item.title)}</h1>
          <div class="detail-actions">
            ${item.status === "active" ? `<button id="detail-pin-button" class="secondary-button" type="button">${item.is_pinned ? "取消置顶" : "置顶显示"}</button>` : ""}
            <p id="detail-pin-state" class="muted">${detailPinState(item)}</p>
          </div>
          <p id="detail-pin-status" class="status-message" role="status"></p>
        </section>

        <div class="detail-content">
          <section class="detail-block understanding-block">
            <div class="understanding-heading">
              <h2>当前理解</h2>
              <button class="edit-entry-button" id="edit-item-button" type="button" aria-haspopup="dialog" aria-controls="edit-dialog">修正字段</button>
            </div>
            <dl class="detail-grid">
              ${detailField("下一步", item.next_action || "未填写", true, "detail-next-action")}
              ${detailField("截止日期", item.deadline ? deadlineDisplay(item.deadline) : "未填写")}
              ${detailField("预计耗时", item.estimated_time ? `${item.estimated_time} 分钟` : "未填写")}
            </dl>
            ${item.extra_information && Object.keys(item.extra_information).length ? `
              <div class="supplemental-section">
                <h3>补充信息</h3>
                ${renderExtraInformation(item.extra_information)}
              </div>` : ""}
          </section>

          ${reminderDetailSection(item, data.reminder)}

          <section class="detail-block continue-block">
            <h2>继续记录</h2>
            <form id="update-form">
              <label class="visually-hidden" for="update-text">新增情况或修正</label>
              <textarea id="update-text" maxlength="10000" required placeholder="补充新情况、进展，或修正之前的理解…"></textarea>
              <div class="form-footer">
                <p id="update-status" class="status-message" role="status">补充原文会先保存。</p>
                <button class="primary-button update-submit" type="submit">保存补充</button>
              </div>
            </form>
          </section>

          <details class="detail-block advanced-block">
            <summary>原始记录（${data.inputs.length}）</summary>
            <ol class="history-list">${data.inputs.map(inputHistoryItem).join("")}</ol>
          </details>

          <details class="detail-block advanced-block more-actions-block">
            <summary>更多信息与操作</summary>
            <dl class="detail-metadata">
              ${detailField("状态", labels[item.status])}
              ${detailField("类型", itemTypeLabel(item.type))}
              ${detailField("更新时间", formatTime(item.updated_time), true)}
            </dl>
            <div class="reprocess-section">
              <p class="muted">需要时，可用全部原始记录重新整理当前理解。</p>
              <button class="secondary-button quiet-button" id="reprocess-item-button">重新整理</button>
              <p id="reprocess-status" class="status-message" role="status"></p>
            </div>
          </details>

          <section class="detail-block lifecycle-block">
            <h2>事项状态</h2>
            <div class="detail-actions lifecycle-actions">
              ${lifecycleActions(item)}
            </div>
            <p id="item-action-status" class="status-message" role="status"></p>
          </section>
        </div>

        <dialog id="edit-dialog" class="edit-dialog" aria-labelledby="edit-dialog-title">
          <section class="edit-surface">
            <header class="edit-surface-header">
              <div>
                <p class="eyebrow">修正字段</p>
                <h2 id="edit-dialog-title">修正当前理解</h2>
              </div>
              <button class="edit-close-button" id="edit-close-button" type="button" aria-label="关闭修正字段">×</button>
            </header>
            <div class="edit-scroll-region">
              ${buildEditForm(item)}
            </div>
          </section>
        </dialog>

      </div>`;

    notificationOnboardingHasReminder = reminderIsNotificationEligible(
      data.reminder,
    );
    renderNotificationOnboarding();

    const updateForm = document.querySelector("#update-form");
    const updateButton = updateForm.querySelector("button");
    const updateStatus = document.querySelector("#update-status");
    const editForm = document.querySelector("#edit-form");
    const editDialog = document.querySelector("#edit-dialog");
    const editTrigger = document.querySelector("#edit-item-button");
    const editCloseButton = document.querySelector("#edit-close-button");
    const editCancelButton = document.querySelector("#edit-cancel-button");
    const editSubmitButton = editForm.querySelector("button[type='submit']");
    const editMessage = document.querySelector("#edit-status-message");
    const actionStatus = document.querySelector("#item-action-status");
    const reprocessStatus = document.querySelector("#reprocess-status");
    const reminderActionStatus = document.querySelector("#reminder-action-status");
    const pinButton = document.querySelector("#detail-pin-button");
    const pinState = document.querySelector("#detail-pin-state");
    const pinStatus = document.querySelector("#detail-pin-status");
    const syncPin = (updatedItem) => {
      item.is_pinned = updatedItem.is_pinned;
      item.status = updatedItem.status;
      pinState.textContent = detailPinState(item);
      if (pinButton) {
        pinButton.hidden = item.status !== "active";
        pinButton.textContent = item.is_pinned ? "取消置顶" : "置顶显示";
      }
    };
    pinButton?.addEventListener("click", async () => {
      if (pinButton.disabled || item.status !== "active") return;
      pinButton.disabled = true;
      pinButton.dataset.saving = "true";
      pinStatus.dataset.kind = "";
      pinStatus.textContent = "正在保存…";
      try {
        const updatedItem = await api(`/api/items/${itemId}`, {
          method: "PATCH",
          body: JSON.stringify({ is_pinned: !item.is_pinned }),
        });
        syncPin(updatedItem);
        pinStatus.dataset.kind = "success";
        pinStatus.textContent = "显示设置已保存。";
      } catch (error) {
        pinStatus.dataset.kind = "error";
        pinStatus.textContent = `未能确认保存，请重试：${error.message}`;
      } finally {
        pinButton.dataset.saving = "false";
        pinButton.disabled = false;
      }
    });

    const closeEditor = () => {
      if (typeof editDialog.close === "function") editDialog.close("cancel");
      else {
        editDialog.removeAttribute("open");
        document.body.classList.remove("editing-fields");
        editTrigger.focus();
      }
    };
    const requestEditorClose = () => {
      if (editDialog.dataset.saving === "true") return;
      if (
        editForm.dataset.dirty === "true" &&
        !window.confirm("放弃尚未保存的字段修改吗？")
      ) {
        return;
      }
      editForm.reset();
      editForm.dataset.dirty = "false";
      editMessage.dataset.kind = "";
      editMessage.textContent = "重要性、紧急性或截止日期变化会要求确认。";
      closeEditor();
    };
    const setEditorSaving = (saving) => {
      editDialog.dataset.saving = String(saving);
      editDialog.setAttribute("aria-busy", String(saving));
      editSubmitButton.disabled = saving;
      editCancelButton.disabled = saving;
      editCloseButton.disabled = saving;
      editSubmitButton.textContent = saving ? "保存中…" : "保存修正";
    };

    editTrigger.addEventListener("click", () => {
      if (typeof editDialog.showModal === "function") editDialog.showModal();
      else editDialog.setAttribute("open", "");
      document.body.classList.add("editing-fields");
      window.requestAnimationFrame(() => document.querySelector("#edit-title").focus());
    });
    editCloseButton.addEventListener("click", requestEditorClose);
    editCancelButton.addEventListener("click", requestEditorClose);
    editDialog.addEventListener("cancel", (event) => {
      event.preventDefault();
      requestEditorClose();
    });
    editDialog.addEventListener("close", () => {
      document.body.classList.remove("editing-fields");
      editTrigger.focus();
    });

    [updateForm, editForm].forEach((form) => {
      form.dataset.dirty = "false";
      const markDirty = () => {
        form.dataset.dirty = "true";
      };
      form.addEventListener("input", markDirty);
      form.addEventListener("change", markDirty);
    });
    updateForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      const text = document.querySelector("#update-text").value.trim();
      if (!text) return;
      updateButton.disabled = true;
      updateButton.setAttribute("aria-busy", "true");
      updateButton.textContent = "保存中…";
      updateStatus.dataset.kind = "";
      updateStatus.textContent = "正在保存补充原文…";
      try {
        await api(`/api/items/${itemId}/inputs`, {
          method: "POST",
          body: JSON.stringify({ original_text: text, input_method: "text" }),
        });
        const updateTextarea = document.querySelector("#update-text");
        updateTextarea.value = "";
        updateTextarea.blur();
        updateForm.dataset.dirty = "false";
        updateStatus.dataset.kind = "success";
        updateStatus.textContent = "已保存，正在后台整理。";
        updateButton.removeAttribute("aria-busy");
        updateButton.textContent = "已保存";
        scheduleDetailPoll(itemId, 1800);
      } catch (error) {
        updateStatus.dataset.kind = "error";
        updateStatus.textContent = `尚未保存，输入仍保留：${error.message}`;
        updateButton.removeAttribute("aria-busy");
        updateButton.textContent = "保存补充";
        updateButton.disabled = false;
      }
    });

    editForm.addEventListener("submit", async (event) => {
      event.preventDefault();
      try {
        const patch = changedPatch(item);
        if (Object.keys(patch).length === 0) {
          editMessage.textContent = "没有需要保存的变化。";
          return;
        }
        const deadlineChanged = Object.hasOwn(patch, "deadline");
        if (deadlineChanged && !window.confirm("确认修改截止日期吗？")) return;
        patch.confirmed_important_fields = deadlineChanged;
        setEditorSaving(true);
        editMessage.dataset.kind = "";
        editMessage.textContent = "正在保存修正…";
        const updatedItem = await api(`/api/items/${itemId}`, {
          method: "PATCH",
          body: JSON.stringify(patch),
        });
        Object.assign(item, updatedItem);
        editForm.dataset.dirty = "false";
        setEditorSaving(false);
        closeEditor();
        if (detailRefreshBlocked()) {
          actionStatus.dataset.kind = "success";
          actionStatus.textContent = "字段修正已保存；当前未提交补充已保留。";
          scheduleDetailPoll(itemId);
          return;
        }
        await renderDetail(itemId, { silent: true });
      } catch (error) {
        editMessage.dataset.kind = "error";
        editMessage.textContent = error.message;
        setEditorSaving(false);
      }
    });

    document.querySelectorAll(".detail-retry-button").forEach((button) => {
      button.addEventListener("click", async () => {
        button.disabled = true;
        try {
          await api(`/api/inputs/${button.dataset.inputId}/retry`, {
            method: "POST",
          });
          await renderDetail(itemId, { silent: true, automatic: true });
        } catch (error) {
          button.disabled = false;
          button.textContent = error.message;
        }
      });
    });

    const reminderSetButton = document.querySelector(".reminder-set-detail-button");
    if (reminderSetButton) {
      reminderSetButton.addEventListener("click", () => {
        openReminderEditor({
          item,
          reminder: data.reminder,
          onSaved: async () => {
            await renderDetail(itemId, { silent: true });
          },
        });
      });
    }
    const reminderCancelButton = document.querySelector(".reminder-cancel-detail-button");
    if (reminderCancelButton) {
      reminderCancelButton.addEventListener("click", async () => {
        reminderCancelButton.disabled = true;
        reminderActionStatus.textContent = "正在关闭提醒…";
        try {
          await api(`/api/reminders/${data.reminder.id}`, { method: "DELETE" });
          await renderDetail(itemId, { silent: true });
        } catch (error) {
          reminderActionStatus.dataset.kind = "error";
          reminderActionStatus.textContent = `提醒未关闭：${error.message}`;
          reminderCancelButton.disabled = false;
        }
      });
    }
    if (data.reminder?.status === "due" && !data.reminder.surfaced_time) {
      const reminderWasRendered = document.querySelector(
        `.reminder-detail-block[data-reminder-status="due"]`,
      );
      if (reminderWasRendered) {
        void api(`/api/reminders/${data.reminder.id}/surface`, { method: "POST" }).catch(
          () => {},
        );
      }
    }

    if (actionMessage) {
      reprocessStatus.dataset.kind = "success";
      reprocessStatus.textContent = actionMessage;
    }
    const reprocessButton = document.querySelector("#reprocess-item-button");
    reprocessButton.addEventListener("click", async () => {
      reprocessButton.disabled = true;
      reprocessStatus.dataset.kind = "";
      reprocessStatus.textContent = "正在使用全部原始记录重新整理…";
      try {
        await api(`/api/items/${itemId}/reprocess`, { method: "POST" });
        await renderDetail(itemId, {
          silent: true,
          actionMessage: "重新整理完成；核心字段冲突时已保留原值。",
        });
      } catch (error) {
        reprocessStatus.dataset.kind = "error";
        reprocessStatus.textContent = `重新整理失败：${error.message}`;
        reprocessButton.disabled = false;
      }
    });

    document.querySelectorAll(".lifecycle-status-button").forEach((button) => {
      button.addEventListener("click", async () => {
        const nextStatus = button.dataset.status;
        if (pinButton?.disabled) return;
        if (pinButton) pinButton.disabled = true;
        button.disabled = true;
        actionStatus.textContent = "正在更新状态…";
        try {
          const updatedItem = await api(`/api/items/${itemId}`, {
            method: "PATCH",
            body: JSON.stringify({ status: nextStatus }),
          });
          syncPin(updatedItem);
          if (detailRefreshBlocked()) {
            actionStatus.dataset.kind = "success";
            actionStatus.textContent = "状态已更新；当前未提交输入已保留。";
            scheduleDetailPoll(itemId);
            return;
          }
          await renderDetail(itemId, { silent: true });
        } catch (error) {
          actionStatus.dataset.kind = "error";
          actionStatus.textContent = error.message;
          button.disabled = false;
        } finally {
          if (pinButton) pinButton.disabled = false;
        }
      });
    });

    const deleteButton = document.querySelector(".permanent-delete-button");
    if (deleteButton) {
      deleteButton.addEventListener("click", async () => {
        if (!window.confirm("确认永久删除这个事项及其原始记录吗？此操作无法撤销。")) return;
        deleteButton.disabled = true;
        try {
          await api(`/api/items/${itemId}`, { method: "DELETE" });
          navigate(dashboardPath("trash"));
        } catch (error) {
          actionStatus.dataset.kind = "error";
          actionStatus.textContent = error.message;
          deleteButton.disabled = false;
        }
      });
    }

    if (hasProcessing) {
      scheduleDetailPoll(itemId);
    }
  } catch (error) {
    if (automatic && detailRefreshBlocked()) {
      scheduleDetailPoll(itemId);
      return;
    }
    appElement.innerHTML = `<div class="empty-state section">无法读取事项：${escapeHtml(error.message)}<br /><a href="/dashboard" data-link>返回事项</a></div>`;
  }
}

function renderRoute() {
  resetDashboardSelection();
  if (activeCaptureController) {
    const previousCaptureController = activeCaptureController;
    activeCaptureController = null;
    previousCaptureController.dispose();
  }
  if (pollTimer) window.clearTimeout(pollTimer);
  pollTimer = null;
  document.body.classList.remove("editing-fields");
  setActiveNavigation();
  window.scrollTo({ top: 0, behavior: "instant" });
  const path = window.location.pathname;

  if (!isKnownPath(path)) {
    renderNotFound();
    appElement.focus({ preventScroll: true });
    return;
  }

  if (authentication.status === AUTH_STATES.LOADING) {
    renderLoading();
    return;
  }
  if (authentication.status === AUTH_STATES.NETWORK_ERROR) {
    renderNetworkError();
    return;
  }
  if (authentication.status === AUTH_STATES.UNAUTHENTICATED) {
    if (path === "/register") renderRegister();
    else if (path === "/login" || path === "/") renderLogin();
    else {
      const next = `${path}${window.location.search}`;
      history.replaceState({}, "", `/login?next=${encodeURIComponent(next)}`);
      renderLogin();
    }
    appElement.focus({ preventScroll: true });
    return;
  }

  if (path === "/login" || path === "/register") {
    const destination = path === "/login" ? safeLoginDestination() : "/";
    history.replaceState({}, "", destination);
    renderRoute();
    return;
  }

  const detailMatch = path.match(/^\/items\/(\d+)$/);
  if (detailMatch) renderDetail(Number(detailMatch[1]));
  else if (path === "/dashboard") renderDashboard();
  else if (path === "/account") renderAccount();
  else renderCapture();
  if (path !== "/capture" && path !== "/") {
    appElement.focus({ preventScroll: true });
  }
}

function waitForStartupAuthenticationRetry(delay) {
  return new Promise((resolve) => window.setTimeout(resolve, delay));
}

async function fetchCurrentUserWithStartupRetry() {
  for (let attempt = 0; ; attempt += 1) {
    try {
      return await api("/api/auth/me");
    } catch (error) {
      const retryDelay = STARTUP_AUTH_RETRY_DELAYS_MS[attempt];
      if (!(error instanceof NetworkError) || retryDelay === undefined) {
        throw error;
      }
      await waitForStartupAuthenticationRetry(retryDelay);
    }
  }
}

async function runAuthenticationInitialization() {
  setAuthentication(AUTH_STATES.LOADING);
  renderRoute();
  try {
    const user = await fetchCurrentUserWithStartupRetry();
    setAuthentication(AUTH_STATES.AUTHENTICATED, user);
  } catch (error) {
    if (error instanceof ApiError && error.status === 401) {
      setAuthentication(AUTH_STATES.UNAUTHENTICATED, null);
    } else {
      // A transient failure must not erase an already resolved user in memory.
      setAuthentication(AUTH_STATES.NETWORK_ERROR);
    }
  }
  renderRoute();
}

async function initializeAuthentication() {
  if (authenticationInitializationPromise) {
    return authenticationInitializationPromise;
  }
  authenticationInitializationPromise = runAuthenticationInitialization();
  try {
    await authenticationInitializationPromise;
  } finally {
    authenticationInitializationPromise = null;
  }
}

void initializeAuthentication();

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker
      .register("/service-worker.js", { scope: "/" })
      .then((registration) => registration.update())
      .catch(() => {
        // Installability is an enhancement; API workflows remain available.
      });
  });
}

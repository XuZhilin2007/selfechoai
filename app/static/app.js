const appElement = document.querySelector("#app");
const primaryNavigation = document.querySelector("#primary-navigation");
let pollTimer = null;
let authRedirectTimer = null;

const AUTH_STATES = Object.freeze({
  LOADING: "loading",
  AUTHENTICATED: "authenticated",
  UNAUTHENTICATED: "unauthenticated",
  NETWORK_ERROR: "network_error",
});
const CSRF_COOKIE_NAME = "selfecho_csrf";
const CAPTURE_DRAFT_KEY = "selfecho.capture-draft";
const authentication = {
  status: AUTH_STATES.LOADING,
  user: null,
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
  configuration: "配置缺失",
  network: "网络错误",
  api: "API 错误",
  invalid_output: "输出无效",
  internal: "内部错误",
};

const lifecycleViews = {
  active: {
    label: "当前",
    heading: "现在值得关注的事",
    empty: "还没有需要关注的事项。",
  },
  completed: {
    label: "已完成",
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

function navigate(path) {
  history.pushState({}, "", path);
  renderRoute();
}

function detailRefreshBlocked() {
  const activeControl = document.activeElement;
  const editorHasFocus =
    activeControl instanceof HTMLElement &&
    activeControl.matches(
      "#update-form textarea, #edit-form input, #edit-form textarea, #edit-form select",
    );
  const dirtyForm = document.querySelector(
    '#update-form[data-dirty="true"], #edit-form[data-dirty="true"]',
  );
  const modalOpen = document.querySelector(
    "#trash-confirm-dialog[open], #edit-dialog[open]",
  );
  return editorHasFocus || Boolean(dirtyForm) || Boolean(modalOpen);
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
  const link = event.target.closest("a[data-link]");
  if (!link || link.origin !== window.location.origin) return;
  event.preventDefault();
  navigate(`${link.pathname}${link.search}`);
});

window.addEventListener("popstate", renderRoute);

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

function tag(label, value) {
  return `<span class="tag ${escapeHtml(value)}">${escapeHtml(label)}：${escapeHtml(labels[value] || value)}</span>`;
}

function itemTypeLabel(value) {
  return itemTypeLabels[value] || String(value).replaceAll("_", " ").replaceAll("-", " ");
}

function deadlinePresentation(value) {
  if (!value) return null;
  const [year, month, day] = value.slice(0, 10).split("-").map(Number);
  const deadline = new Date(year, month - 1, day);
  if (Number.isNaN(deadline.getTime())) {
    return { text: `截止 ${formatDate(value)}`, emphasis: false };
  }
  const today = new Date();
  today.setHours(0, 0, 0, 0);
  const difference = Math.round((deadline.getTime() - today.getTime()) / 86_400_000);
  if (difference < 0) return { text: `已截止 ${formatDate(value)}`, emphasis: true, kind: "overdue" };
  if (difference === 0) return { text: "今天截止", emphasis: true, kind: "deadline" };
  if (difference === 1) return { text: "明天截止", emphasis: true, kind: "deadline" };
  if (difference <= 7) return { text: `${difference} 天后截止`, emphasis: true, kind: "deadline" };
  return { text: `截止 ${formatDate(value)}`, emphasis: false };
}

function selectedDashboardStatus() {
  const status = new URLSearchParams(window.location.search).get("status") || "active";
  return Object.hasOwn(lifecycleViews, status) ? status : "active";
}

function dashboardPath(status) {
  return status === "active" ? "/dashboard" : `/dashboard?status=${status}`;
}

function lifecycleNavigation(selectedStatus) {
  return `
    <nav class="lifecycle-navigation" aria-label="事项状态">
      ${Object.entries(lifecycleViews)
        .map(([status, view]) => `
          <a href="${dashboardPath(status)}" data-link ${status === selectedStatus ? 'aria-current="page"' : ""}>
            ${escapeHtml(view.label)}
          </a>`)
        .join("")}
    </nav>`;
}

function itemCard(item) {
  const signals = [];
  const metadata = [];
  if (item.importance === "high") signals.push('<span class="card-signal importance">高重要</span>');
  else if (item.importance !== "unknown") metadata.push(`重要 ${labels[item.importance]}`);
  if (item.urgency === "high") signals.push('<span class="card-signal urgency">高紧急</span>');
  else if (item.urgency !== "unknown") metadata.push(`紧急 ${labels[item.urgency]}`);
  if (item.importance === "unknown" || item.urgency === "unknown") {
    signals.push('<span class="card-signal unknown">优先级待确认</span>');
  }
  const deadline = deadlinePresentation(item.deadline);
  if (deadline?.emphasis) {
    signals.push(`<span class="card-signal ${escapeHtml(deadline.kind)}">${escapeHtml(deadline.text)}</span>`);
  } else if (deadline) {
    metadata.push(deadline.text);
  }
  if (item.estimated_time) metadata.push(`约 ${item.estimated_time} 分钟`);
  return `
    <a class="item-card" href="/items/${item.id}" data-link>
      <h3>${escapeHtml(item.title)}</h3>
      ${signals.length ? `<div class="card-signals">${signals.join("")}</div>` : ""}
      ${metadata.length ? `<div class="card-meta">${metadata.map((value) => `<span>${escapeHtml(value)}</span>`).join("")}</div>` : ""}
    </a>`;
}

function quickPriorityButtons(item, field) {
  const fieldLabel = field === "importance" ? "重要性" : "紧急性";
  return `
    <div class="quick-priority-row">
      <span>${fieldLabel}</span>
      <div class="quick-priority-buttons" role="group" aria-label="${escapeHtml(item.title)}的${fieldLabel}">
        ${["low", "medium", "high"].map((value) => `
          <button
            class="priority-choice-button"
            type="button"
            data-item-id="${item.id}"
            data-field="${field}"
            data-value="${value}"
          >${labels[value]}</button>`).join("")}
      </div>
    </div>`;
}

function quickConfirmationCard(item) {
  return `
    <article class="quick-confirmation-card">
      <h3>${escapeHtml(item.title)}</h3>
      ${item.importance === "unknown" ? quickPriorityButtons(item, "importance") : ""}
      ${item.urgency === "unknown" ? quickPriorityButtons(item, "urgency") : ""}
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

function renderLogin() {
  appElement.innerHTML = `
    <section class="auth-layout">
      <div class="auth-heading">
        <p class="eyebrow">Welcome back</p>
        <h1>继续整理你的想法。</h1>
        <p class="subtitle">登录后只会看到属于你的记录和事项。</p>
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
        <p class="eyebrow">Invite only</p>
        <h1>创建你的 SelfEcho。</h1>
        <p class="subtitle">当前仅对受邀测试用户开放，每个账号拥有独立的数据空间。</p>
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
    <section class="page-heading">
      <p class="eyebrow">Account</p>
      <h1>${escapeHtml(user.display_name)}</h1>
      <p class="subtitle">当前登录账号与本地显示设置。</p>
    </section>
    <section class="panel account-panel">
      <dl class="account-details">
        <div><dt>邮箱</dt><dd>${escapeHtml(user.email)}</dd></div>
        <div><dt>显示名称</dt><dd>${escapeHtml(user.display_name)}</dd></div>
        <div><dt>时区</dt><dd>${escapeHtml(user.timezone)}</dd></div>
      </dl>
      <div class="account-actions">
        <p id="logout-status" class="status-message" role="alert"></p>
        <button id="logout-button" class="secondary-button" type="button">退出登录</button>
      </div>
    </section>`;

  const button = document.querySelector("#logout-button");
  const status = document.querySelector("#logout-status");
  button.addEventListener("click", async () => {
    button.disabled = true;
    status.dataset.kind = "";
    status.textContent = "正在退出…";
    try {
      await api("/api/auth/logout", { method: "POST" });
      completeLocalLogout();
    } catch (error) {
      if (error instanceof ApiError && error.status === 401) {
        completeLocalLogout();
        return;
      }
      status.dataset.kind = "error";
      status.textContent = error instanceof ApiError && error.status === 403
        ? "安全校验已失效，请刷新页面后重试。"
        : `退出失败：${error.message}`;
      button.disabled = false;
    }
  });
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

function readCaptureDraft() {
  try {
    return sessionStorage.getItem(captureDraftStorageKey()) || "";
  } catch (_) {
    return "";
  }
}

function writeCaptureDraft(value) {
  try {
    const key = captureDraftStorageKey();
    if (value) sessionStorage.setItem(key, value);
    else sessionStorage.removeItem(key);
  } catch (_) {
    // Capture still works if browser storage is unavailable.
  }
}

function clearCaptureDraft() {
  writeCaptureDraft("");
}

function renderCapture() {
  appElement.innerHTML = `
    <div class="capture-view">
      <section class="page-heading capture-heading">
        <h1>先记下来</h1>
      </section>
      <section class="panel capture-panel">
        <form id="capture-form">
          <label class="visually-hidden" for="capture-text">记录内容</label>
          <textarea id="capture-text" name="original_text" maxlength="10000" required autofocus placeholder="想到什么，就写下来……"></textarea>
          <div class="form-footer">
            <p id="capture-status" class="status-message" role="status">原文会先保存。</p>
            <button class="primary-button capture-submit" type="submit">保存</button>
          </div>
        </form>
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
  const button = form.querySelector("button");
  const quickSection = document.querySelector("#quick-confirmation-section");
  const quickList = document.querySelector("#quick-confirmation-list");
  const quickCount = document.querySelector("#quick-confirmation-count");
  const quickStatus = document.querySelector("#quick-confirmation-status");
  let quickItems = [];

  textarea.value = readCaptureDraft();
  textarea.addEventListener("input", () => writeCaptureDraft(textarea.value));

  function renderQuickConfirmation() {
    const items = quickItems.filter(
      (item) => item.importance === "unknown" || item.urgency === "unknown",
    );
    quickSection.hidden = items.length === 0;
    quickCount.textContent = items.length;
    quickList.innerHTML = items.map(quickConfirmationCard).join("");
  }

  async function loadQuickConfirmation() {
    if (pollTimer) window.clearTimeout(pollTimer);
    pollTimer = null;
    try {
      const data = await api("/api/items");
      if (!quickSection.isConnected) return;
      quickItems = data.needs_confirmation;
      renderQuickConfirmation();
      if (data.pending_inputs.length) {
        pollTimer = window.setTimeout(loadQuickConfirmation, 2500);
      }
    } catch (_) {
      // Capture remains fully usable if the optional queue cannot be loaded.
    }
  }

  quickList.addEventListener("click", async (event) => {
    const choice = event.target.closest(".priority-choice-button");
    if (!choice) return;
    const item = quickItems.find(
      (candidate) => candidate.id === Number(choice.dataset.itemId),
    );
    if (!item) return;
    if (pollTimer) window.clearTimeout(pollTimer);
    pollTimer = null;

    const field = choice.dataset.field;
    const previousValue = item[field];
    item[field] = choice.dataset.value;
    quickStatus.dataset.kind = "";
    quickStatus.textContent = "正在保存确认…";
    renderQuickConfirmation();
    try {
      const updated = await api(`/api/items/${item.id}`, {
        method: "PATCH",
        body: JSON.stringify({
          [field]: item[field],
          confirmed_important_fields: true,
        }),
      });
      item.importance = updated.importance;
      item.urgency = updated.urgency;
      quickStatus.dataset.kind = "success";
      quickStatus.textContent = "已保存确认。";
      renderQuickConfirmation();
    } catch (error) {
      item[field] = previousValue;
      quickStatus.dataset.kind = "error";
      quickStatus.textContent = `确认未保存：${error.message}`;
      renderQuickConfirmation();
    }
  });

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const originalText = textarea.value.trim();
    if (!originalText) return;
    button.disabled = true;
    button.setAttribute("aria-busy", "true");
    button.textContent = "保存中…";
    status.dataset.kind = "";
    status.textContent = "正在保存原文…";
    try {
      await api("/api/inputs", {
        method: "POST",
        body: JSON.stringify({ original_text: originalText, input_method: "text" }),
      });
      textarea.value = "";
      clearCaptureDraft();
      status.dataset.kind = "success";
      status.innerHTML = '<span class="save-feedback"><strong>✓ 已保存</strong><span>AI 正在后台整理</span></span>';
      textarea.focus();
      void loadQuickConfirmation();
      window.setTimeout(() => {
        if (status.isConnected && status.dataset.kind === "success") {
          status.dataset.kind = "";
          status.textContent = "原文会先保存。";
        }
      }, 1800);
    } catch (error) {
      status.dataset.kind = "error";
      status.textContent = `尚未保存，输入仍保留：${error.message}`;
    } finally {
      button.disabled = false;
      button.removeAttribute("aria-busy");
      button.textContent = "保存";
    }
  });
  void loadQuickConfirmation();
}

function inputStatusCard(input, failed = false) {
  const failureLabel = input.failure_type
    ? `<span class="tag unknown">${escapeHtml(labels[input.failure_type] || input.failure_type)}</span>`
    : "";
  const failureMessage = input.failure_message || "未记录具体原因；请重新整理以获取诊断。";
  return `
    <article class="status-card ${failed ? "failed" : "pending"}">
      <p class="original-preview">${escapeHtml(input.original_text)}</p>
      <p class="muted">${failed ? "整理失败，原文已安全保存。" : "原文已保存，正在整理。"}</p>
      ${failed ? `<div class="failure-reason">${failureLabel}<span>${escapeHtml(failureMessage)}</span></div>` : ""}
      ${failed ? `<button class="secondary-button retry-button" data-input-id="${input.id}">重新整理</button>` : ""}
    </article>`;
}

async function renderDashboard({ silent = false } = {}) {
  if (!silent) {
    appElement.innerHTML = '<p class="loading">正在读取事项…</p>';
  }
  try {
    const selectedStatus = selectedDashboardStatus();
    const view = lifecycleViews[selectedStatus];
    const data = await api(`/api/items?status=${encodeURIComponent(selectedStatus)}`);
    const activeCount = data.sortable_items.length + data.needs_confirmation.length;
    const inactiveItems = [...data.sortable_items, ...data.needs_confirmation];
    const visibleCount = selectedStatus === "active" ? activeCount : inactiveItems.length;
    appElement.innerHTML = `
      <section class="page-heading dashboard-heading ${escapeHtml(selectedStatus)}">
        <div class="dashboard-title-row">
          <h1>${escapeHtml(view.heading)}</h1>
          <span class="dashboard-total" aria-label="${visibleCount} 个事项">${visibleCount}</span>
        </div>
      </section>

      ${lifecycleNavigation(selectedStatus)}

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
          <div class="item-list">${data.sortable_items.map(itemCard).join("")}</div>
        </section>` : ""}
        ${data.needs_confirmation.length ? `<section class="section dashboard-secondary-section">
          <div class="section-heading"><h2>信息待确认</h2><span class="count">${data.needs_confirmation.length}</span></div>
          <div class="item-list">${data.needs_confirmation.map(itemCard).join("")}</div>
        </section>` : ""}
      ` : `
        <section class="section dashboard-primary-section">
          ${inactiveItems.length ? `<div class="item-list">${inactiveItems.map(itemCard).join("")}</div>` : `<div class="empty-state compact-empty-state">${escapeHtml(view.empty)}</div>`}
        </section>`}

      ${selectedStatus === "active" && activeCount === 0 && data.pending_inputs.length === 0 && data.failed_inputs.length === 0 ? `<div class="empty-state compact-empty-state section">${escapeHtml(view.empty)} <a href="/capture" data-link>去记录一条想法</a></div>` : ""}`;

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

    if (selectedStatus === "active" && data.pending_inputs.length) {
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
      <div><label for="edit-status">状态</label><select id="edit-status">${["active", "completed", "trash"].map((value) => `<option value="${value}" ${item.status === value ? "selected" : ""}>${labels[value]}</option>`).join("")}</select></div>
      <div><label for="edit-importance">重要性</label><select id="edit-importance">${["unknown", "low", "medium", "high"].map((value) => `<option value="${value}" ${item.importance === value ? "selected" : ""}>${labels[value]}</option>`).join("")}</select></div>
      <div><label for="edit-urgency">紧急性</label><select id="edit-urgency">${["unknown", "low", "medium", "high"].map((value) => `<option value="${value}" ${item.urgency === value ? "selected" : ""}>${labels[value]}</option>`).join("")}</select></div>
      <div><label for="edit-deadline">截止日期</label><input id="edit-deadline" type="date" value="${escapeHtml(item.deadline ? item.deadline.slice(0, 10) : "")}" /></div>
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
        <p id="edit-status-message" class="status-message" role="status">重要性、紧急性或截止日期变化会要求确认。</p>
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
    status: document.querySelector("#edit-status").value,
    importance: document.querySelector("#edit-importance").value,
    urgency: document.querySelector("#edit-urgency").value,
    deadline: document.querySelector("#edit-deadline").value || null,
    estimated_time: document.querySelector("#edit-estimate").value
      ? Number(document.querySelector("#edit-estimate").value)
      : null,
    next_action: document.querySelector("#edit-next").value.trim() || null,
  };
  const patch = {};
  Object.entries(candidate).forEach(([key, value]) => {
    const original = key === "deadline" && item[key] ? item[key].slice(0, 10) : item[key];
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
      <button class="primary-button lifecycle-primary lifecycle-status-button" data-status="completed">标记完成</button>
      <button class="text-button danger-button lifecycle-status-button" data-status="trash">移入回收站</button>`;
  }
  return `
    <button class="primary-button lifecycle-primary lifecycle-status-button" data-status="active">恢复为进行中</button>
    <button class="text-button danger-button lifecycle-status-button" data-status="trash">移入回收站</button>`;
}

function confirmTrashMove() {
  const message = "确定将这个事项移入回收站吗？之后仍可以从回收站恢复。";
  const dialog = document.querySelector("#trash-confirm-dialog");
  if (!dialog || typeof dialog.showModal !== "function") {
    return Promise.resolve(window.confirm(message));
  }
  dialog.returnValue = "cancel";
  return new Promise((resolve) => {
    dialog.addEventListener("close", () => resolve(dialog.returnValue === "confirm"), {
      once: true,
    });
    dialog.showModal();
  });
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
        </section>

        <section class="detail-block continue-block">
          <h2>又想到什么？</h2>
          <form id="update-form">
            <label class="visually-hidden" for="update-text">新增情况或修正</label>
            <textarea id="update-text" maxlength="10000" required placeholder="补充新情况、进展，或修正之前的理解…"></textarea>
            <div class="form-footer">
              <p id="update-status" class="status-message" role="status">补充原文会先保存。</p>
              <button class="primary-button update-submit" type="submit">保存补充</button>
            </div>
          </form>
        </section>

        <section class="detail-block understanding-block">
          <div class="understanding-heading">
            <h2>当前理解</h2>
            <button class="edit-entry-button" id="edit-item-button" type="button" aria-haspopup="dialog" aria-controls="edit-dialog">修正字段</button>
          </div>
          <dl class="detail-grid">
            ${detailField("下一步", item.next_action || "未填写", true, "detail-next-action")}
            ${detailField("截止日期", item.deadline ? formatDate(item.deadline) : "未填写")}
            ${detailField("预计耗时", item.estimated_time ? `${item.estimated_time} 分钟` : "未填写")}
            ${detailField("重要性", labels[item.importance])}
            ${detailField("紧急性", labels[item.urgency])}
          </dl>
          ${item.extra_information && Object.keys(item.extra_information).length ? `
            <div class="supplemental-section">
              <h3>补充信息</h3>
              ${renderExtraInformation(item.extra_information)}
            </div>` : ""}
        </section>

        <section class="detail-block lifecycle-block">
          <h2>事项状态</h2>
          <div class="detail-actions lifecycle-actions">
            ${lifecycleActions(item)}
          </div>
          <p id="item-action-status" class="status-message" role="status"></p>
        </section>

        <details class="detail-block advanced-block">
          <summary>查看原始记录（${data.inputs.length}）</summary>
          <ol class="history-list">${data.inputs.map((input) => `
            <li>
              ${escapeHtml(input.original_text)}
              <span class="history-meta">${formatTime(input.created_time)} · ${labels[input.processing_status]}</span>
              ${input.processing_status === "failed" ? `<span class="history-error">${escapeHtml(input.failure_message || "未记录具体原因；重新整理后可获得诊断。")}</span>` : ""}
              ${input.processing_status === "failed" ? `<button class="secondary-button detail-retry-button" data-input-id="${input.id}">重新整理这条记录</button>` : ""}
            </li>`).join("")}</ol>
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

        <dialog id="trash-confirm-dialog" class="confirmation-dialog" aria-labelledby="trash-confirm-title">
          <form method="dialog" class="confirmation-dialog-card">
            <h2 id="trash-confirm-title">移入回收站？</h2>
            <p>确定将这个事项移入回收站吗？之后仍可以从回收站恢复。</p>
            <div class="confirmation-dialog-actions">
              <button type="submit" value="cancel" class="secondary-button">取消</button>
              <button type="submit" value="confirm" class="primary-button">移入回收站</button>
            </div>
          </form>
        </dialog>
      </div>`;

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
        const importantChanged = ["importance", "urgency", "deadline"].some((field) => Object.hasOwn(patch, field));
        if (importantChanged && !window.confirm("确认修改重要性、紧急性或截止日期吗？")) return;
        patch.confirmed_important_fields = importantChanged;
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
        if (nextStatus === "trash" && !(await confirmTrashMove())) return;
        button.disabled = true;
        actionStatus.textContent = "正在更新状态…";
        try {
          await api(`/api/items/${itemId}`, {
            method: "PATCH",
            body: JSON.stringify({ status: nextStatus }),
          });
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
          navigate("/dashboard");
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

async function initializeAuthentication() {
  setAuthentication(AUTH_STATES.LOADING);
  renderRoute();
  try {
    const user = await api("/api/auth/me");
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

void initializeAuthentication();

if ("serviceWorker" in navigator) {
  window.addEventListener("load", () => {
    navigator.serviceWorker.register("/service-worker.js", { scope: "/" }).catch(() => {
      // Installability is an enhancement; API workflows remain available.
    });
  });
}

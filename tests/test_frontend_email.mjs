import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const testsDirectory = path.dirname(fileURLToPath(import.meta.url));
const frontendPath = path.join(testsDirectory, "..", "app", "static", "app.js");
const frontendSource = readFileSync(frontendPath, "utf8");

function element() {
  return {
    addEventListener() {},
    classList: { add() {}, remove() {}, contains() { return false; } },
    dataset: {},
    disabled: false,
    focus() {},
    hidden: false,
    innerHTML: "",
    querySelector() { return element(); },
    querySelectorAll() { return []; },
    removeAttribute() {},
    setAttribute() {},
    textContent: "",
    value: "",
  };
}

function createHarness() {
  const controls = element();
  const badge = element();
  const app = element();
  const navigation = element();
  const generic = element();
  const document = {
    body: { classList: { add() {}, remove() {} } },
    cookie: "selfecho_csrf=csrf-token",
    addEventListener() {},
    querySelector(selector) {
      if (selector === "#app") return app;
      if (selector === "#primary-navigation") return navigation;
      if (selector === "#email-reminder-controls") return controls;
      if (selector === "#email-reminder-badge") return badge;
      return generic;
    },
    querySelectorAll() { return []; },
  };
  const context = vm.createContext({
    console,
    document,
    fetch: async () => { throw new Error("unexpected network call"); },
    Headers,
    history: { pushState() {}, replaceState() {} },
    localStorage: { getItem() { return null; }, setItem() {} },
    navigator: {},
    sessionStorage: { getItem() { return null; }, removeItem() {}, setItem() {} },
    Uint8Array,
    URL,
    URLSearchParams,
    window: {
      addEventListener() {},
      clearTimeout() {},
      location: { origin: "https://community.example", pathname: "/account", search: "" },
      scrollTo() {},
      setTimeout() { return 1; },
    },
  });
  const source = frontendSource.replace(
    /^void initializeAuthentication\(\);$/m,
    "",
  ) + `
    globalThis.__renderEmailSettings = renderEmailReminderSettings;
    globalThis.__setEmailUser = (user) => { authentication.user = user; };
  `;
  vm.runInContext(source, context);
  context.__setEmailUser({ email: "login@example.com" });
  return { context, controls, badge };
}

function settings(overrides = {}) {
  return {
    email_address: null,
    verification_status: "pending",
    verified_at: null,
    enabled: false,
    health_status: "healthy",
    pause_reason: null,
    effective_active: false,
    provider_available: true,
    test_email_available: false,
    ...overrides,
  };
}

test("no-address state prefills login identity but explains verification", () => {
  const { context, controls, badge } = createHarness();
  context.__renderEmailSettings(settings());
  assert.equal(badge.textContent, "未设置");
  assert.match(controls.innerHTML, /value="login@example\.com"/);
  assert.match(controls.innerHTML, /修改地址后会暂停实际发送/);
  assert.doesNotMatch(controls.innerHTML, /email-enabled-toggle/);
});

test("disabled provider is explained without exposing configuration", () => {
  const { context, controls } = createHarness();
  context.__renderEmailSettings(settings({ provider_available: false }));
  assert.match(controls.innerHTML, /尚未配置邮件服务/);
  assert.doesNotMatch(controls.innerHTML, /Secret|TemplateID|ap-guangzhou/);
});

test("pending state exposes send, resend, and six-digit verification controls", () => {
  const { context, controls, badge } = createHarness();
  context.__renderEmailSettings(settings({
    email_address: "pending@example.com",
    enabled: true,
  }));
  assert.equal(badge.textContent, "待验证");
  assert.match(controls.innerHTML, /发送 \/ 重发验证码/);
  assert.match(controls.innerHTML, /pattern="\[0-9\]\{6\}"/);
  assert.match(controls.innerHTML, /开启意愿已保留/);
});

test("verified ON and OFF remain distinct account-level states", () => {
  const { context, controls, badge } = createHarness();
  context.__renderEmailSettings(settings({
    email_address: "verified@example.com",
    verification_status: "verified",
    enabled: false,
  }));
  assert.equal(badge.textContent, "已关闭");
  assert.match(controls.innerHTML, /Email Reminder 意愿 OFF/);
  assert.match(controls.innerHTML, /开启邮件提醒/);

  context.__renderEmailSettings(settings({
    email_address: "verified@example.com",
    verification_status: "verified",
    enabled: true,
    effective_active: true,
  }));
  assert.equal(badge.textContent, "已生效");
  assert.equal(badge.dataset.kind, "success");
  assert.match(controls.innerHTML, /Email Reminder 意愿 ON/);
});

test("unhealthy state requires a different address and disables normal delivery", () => {
  const { context, controls, badge } = createHarness();
  context.__renderEmailSettings(settings({
    email_address: "blocked@example.com",
    verification_status: "verified",
    health_status: "paused",
    pause_reason: "blacklisted",
    enabled: true,
  }));
  assert.equal(badge.textContent, "已暂停");
  assert.equal(badge.dataset.kind, "error");
  assert.match(controls.innerHTML, /请更换并验证其他邮箱/);
  assert.doesNotMatch(controls.innerHTML, /email-enabled-toggle/);
});

test("missing dedicated template leaves truthful disabled test capability", () => {
  const { context, controls } = createHarness();
  context.__renderEmailSettings(settings({
    email_address: "verified@example.com",
    verification_status: "verified",
    enabled: true,
  }));
  assert.match(controls.innerHTML, /id="email-test-button"[^>]*disabled/);
  assert.match(controls.innerHTML, /专用测试模板未配置/);
  assert.doesNotMatch(controls.innerHTML, /你已收到/);
});

test("frontend uses fixed Email APIs and preserves device-scoped Push copy", () => {
  for (const endpoint of [
    "/api/email-reminders/settings",
    "/api/email-reminders/address",
    "/api/email-reminders/verification/send",
    "/api/email-reminders/verification/confirm",
    "/api/email-reminders/enabled",
    "/api/email-reminders/test",
  ]) {
    assert.ok(frontendSource.includes(endpoint));
  }
  assert.ok(frontendSource.includes("此设备通知"));
  assert.ok(frontendSource.includes("账户级可选邮件提醒，与此设备的 Web Push 独立"));
  assert.ok(frontendSource.includes("SelfEcho 内提醒仍然可用"));
  assert.ok(frontendSource.includes('headers.set("X-CSRF-Token", csrfToken)'));
  assert.ok(!frontendSource.includes("magic login"));
  assert.ok(!frontendSource.includes("intent://"));
});

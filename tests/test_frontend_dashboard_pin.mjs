import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";
import vm from "node:vm";
import { fileURLToPath } from "node:url";

const testsDirectory = path.dirname(fileURLToPath(import.meta.url));
const frontendPath = path.join(testsDirectory, "..", "app", "static", "app.js");
const frontendSource = readFileSync(frontendPath, "utf8");
const stylesSource = readFileSync(
  path.join(testsDirectory, "..", "app", "static", "styles.css"),
  "utf8",
);

function createPinHarness({ fetchImpl, pinButtons = [], pinnedButton = null }) {
  const elements = new Map();
  function createElement(selector) {
    const children = new Map();
    const handlers = {};
    const element = {
      dataset: {},
      disabled: false,
      handlers,
      hidden: false,
      innerHTML: "",
      isConnected: true,
      textContent: "",
      value: "",
      classList: { add() {}, contains() { return false; }, remove() {} },
      addEventListener(name, handler) {
        (handlers[name] ??= []).push(handler);
      },
      focus() {},
      querySelector(childSelector) {
        if (!children.has(childSelector)) {
          children.set(childSelector, createElement(`${selector} ${childSelector}`));
        }
        return children.get(childSelector);
      },
      querySelectorAll() { return []; },
      removeAttribute() {},
      replaceChildren() {},
      setAttribute() {},
    };
    elements.set(selector, element);
    return element;
  }
  const document = {
    body: { classList: { add() {}, remove() {} } },
    activeElement: null,
    cookie: "selfecho_csrf=pin-token",
    addEventListener() {},
    removeEventListener() {},
    querySelector(selector) {
      if (pinnedButton && selector === '[data-dashboard-pin][data-item-id="7"]') {
        return pinnedButton;
      }
      return elements.get(selector) ?? createElement(selector);
    },
    querySelectorAll(selector) {
      if (selector === "[data-dashboard-pin]") return pinButtons;
      return [];
    },
  };
  const window = {
    addEventListener() {},
    clearTimeout() {},
    confirm: () => true,
    location: {
      pathname: "/dashboard",
      search: "?status=active",
      origin: "https://selfecho.example",
    },
    scrollTo() {},
    setTimeout(callback) { callback(); return 1; },
  };
  const context = vm.createContext({
    console: { warn() {}, error() {} },
    document,
    fetch: fetchImpl,
    Headers,
    history: { pushState() {}, replaceState() {} },
    navigator: {},
    sessionStorage: { getItem() { return null; }, removeItem() {}, setItem() {} },
    URL,
    URLSearchParams,
    window,
  });
  const instrumented = frontendSource.replace(
    /^void initializeAuthentication\(\);$/m,
    "globalThis.__startupPromise = Promise.resolve();",
  ) + `
    globalThis.__pinTest = {
      itemCard,
      bindDashboardPinShortcuts,
      renderDashboardPinStatus,
    };
  `;
  vm.runInContext(instrumented, context, { filename: frontendPath });
  return { context, elements };
}

function pinItem(id, isPinned) {
  return {
    id,
    title: `事项 ${id}`,
    is_pinned: isPinned,
    importance: "unknown",
    urgency: "unknown",
    deadline: null,
    estimated_time: null,
    reminder: null,
  };
}

test("dashboard rows add exactly one pin shortcut outside multi-select", () => {
  const harness = createPinHarness({ fetchImpl: async () => ({ json() {} }) });
  const unpinned = harness.context.__pinTest.itemCard(pinItem(7, false));
  assert.match(unpinned, /class="item-card-row">/);
  assert.equal(unpinned.match(/data-dashboard-pin/g)?.length, 1);
  assert.match(unpinned, /aria-pressed="false"/);
  assert.match(unpinned, /href="\/items\/7"/);
  const pinned = harness.context.__pinTest.itemCard(pinItem(7, true));
  assert.match(pinned, /aria-pressed="true"/);
  assert.match(pinned, /aria-label="取消置顶：事项 7"/);
});

test("pin shortcut PATCHes only is_pinned and surfaces failures without latching", async () => {
  const calls = [];
  let failNext = true;
  const button = {
    dataset: { itemId: "7" },
    disabled: false,
    handlers: {},
    connected: true,
    getAttribute(name) { return name === "aria-pressed" ? "false" : "置顶：事项 7"; },
    addEventListener(name, handler) { this.handlers[name] = [handler]; },
    focus() {},
    querySelector() { return { focus() {} }; },
    querySelectorAll() { return []; },
    removeAttribute() {},
    setAttribute() {},
  };
  const harness = createPinHarness({
    async fetchImpl(requestPath, options) {
      calls.push({ requestPath, method: options?.method, body: options?.body });
      if (failNext) {
        failNext = false;
        return {
          ok: false,
          status: 409,
          async json() { return { detail: "only active items allow Pin changes" }; },
        };
      }
      return jsonResponse({ id: 7, is_pinned: true });
    },
    pinButtons: [button],
    pinnedButton: button,
  });
  harness.context.__pinTest.bindDashboardPinShortcuts();

  await button.handlers.click[0]();

  const status = harness.elements.get("#dashboard-action-status");

  assert.deepEqual(calls, [{
    requestPath: "/api/items/7",
    method: "PATCH",
    body: JSON.stringify({ is_pinned: true }),
  }]);
  assert.equal(button.disabled, false, "failed pin must not latch the button");
  assert.match(status.textContent, /未能确认置顶设置/);
  assert.equal(status.dataset.kind, "error");
});

function jsonResponse(body) {
  return { ok: true, status: 200, async json() { return body; } };
}

test("pin styles keep the row layout and pressed state", () => {
  for (const selector of [
    ".item-card-row",
    ".dashboard-pin-button",
    ".dashboard-pin-button[aria-pressed=\"true\"]",
    ".dashboard-pin-button:disabled",
  ]) {
    assert.ok(stylesSource.includes(selector), `missing style: ${selector}`);
  }
});

test("auth pages read the real registration mode from the config capability", () => {
  assert.match(frontendSource, /api\("\/api\/auth\/config"\)/);
  assert.match(frontendSource, /data-registration-invite/);
  assert.match(frontendSource, /注册当前未开放/);
  assert.match(frontendSource, /没有账号？/);
});

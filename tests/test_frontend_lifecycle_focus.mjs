import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";
import vm from "node:vm";
import { fileURLToPath } from "node:url";


const testsDirectory = path.dirname(fileURLToPath(import.meta.url));
const frontendSource = readFileSync(
  path.join(testsDirectory, "..", "app", "static", "app.js"),
  "utf8",
);
const stylesSource = readFileSync(
  path.join(testsDirectory, "..", "app", "static", "styles.css"),
  "utf8",
);


function parseAttributes(source) {
  const attributes = new Map();
  const pattern = /([:\w-]+)(?:="([^"]*)")?/g;
  for (const match of source.matchAll(pattern)) {
    attributes.set(match[1], match[2] ?? "");
  }
  return attributes;
}


function dataKey(attribute) {
  return attribute.slice(5).replace(/-([a-z])/g, (_match, letter) => letter.toUpperCase());
}


class FocusElement {
  constructor(ownerDocument, tagName, attributes = new Map()) {
    this.ownerDocument = ownerDocument;
    this.tagName = tagName.toUpperCase();
    this.attributes = attributes;
    this.listeners = new Map();
    this.dataset = {};
    this.disabled = attributes.has("disabled");
    this.hidden = attributes.has("hidden");
    this.isConnected = true;
    this.textContent = "";
    this.value = "";
    for (const [name, value] of attributes) {
      if (name.startsWith("data-")) this.dataset[dataKey(name)] = value;
    }
    this.classList = {
      add: (...names) => {
        const values = new Set((this.attributes.get("class") || "").split(/\s+/).filter(Boolean));
        names.forEach((name) => values.add(name));
        this.attributes.set("class", [...values].join(" "));
      },
      contains: (name) => (this.attributes.get("class") || "").split(/\s+/).includes(name),
      remove: (...names) => {
        const removed = new Set(names);
        this.attributes.set(
          "class",
          (this.attributes.get("class") || "")
            .split(/\s+/)
            .filter((name) => name && !removed.has(name))
            .join(" "),
        );
      },
    };
  }

  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(listener);
  }

  getAttribute(name) {
    return this.attributes.has(name) ? this.attributes.get(name) : null;
  }

  setAttribute(name, value) {
    this.attributes.set(name, String(value));
    if (name === "disabled") this.disabled = true;
  }

  removeAttribute(name) {
    this.attributes.delete(name);
    if (name === "disabled") this.disabled = false;
  }

  matchesSelector(selector) {
    if (selector.startsWith("#")) return this.getAttribute("id") === selector.slice(1);
    const itemMatch = selector.match(
      /^\[data-selectable-item\]\[data-item-id="(\d+)"\]$/,
    );
    if (itemMatch) {
      return this.attributes.has("data-selectable-item") &&
        this.getAttribute("data-item-id") === itemMatch[1];
    }
    const dataMatch = selector.match(/^\[([\w-]+)\]$/);
    if (dataMatch) return this.attributes.has(dataMatch[1]);
    if (selector.startsWith(".")) {
      const className = selector.slice(1).split("[")[0];
      return this.classList.contains(className);
    }
    return false;
  }

  closest(selector) {
    if (selector === "[data-selectable-item]" && this.attributes.has("data-selectable-item")) {
      return this;
    }
    if (selector === "a[data-link]" && this.tagName === "A" && this.attributes.has("data-link")) {
      return this;
    }
    return null;
  }

  focus() {
    if (!this.disabled && this.isConnected) this.ownerDocument.activeElement = this;
  }

  blur() {
    if (this.ownerDocument.activeElement === this) {
      this.ownerDocument.activeElement = this.ownerDocument.body;
    }
  }

  remove() {
    this.isConnected = false;
    this.ownerDocument.elements = this.ownerDocument.elements.filter((element) => element !== this);
    if (this.ownerDocument.activeElement === this) {
      this.ownerDocument.activeElement = this.ownerDocument.body;
    }
  }

  async dispatch(type, properties = {}) {
    const event = {
      type,
      target: this,
      currentTarget: this,
      defaultPrevented: false,
      propagationStopped: false,
      preventDefault() { this.defaultPrevented = true; },
      stopPropagation() { this.propagationStopped = true; },
      ...properties,
    };
    for (const listener of this.listeners.get(type) || []) {
      await listener(event);
    }
    if (type === "click" && !event.propagationStopped) {
      await this.ownerDocument.dispatch(type, event);
    }
    return event;
  }

  async press(key) {
    if (key === "Enter") return this.dispatch("click");
    const event = await this.ownerDocument.dispatch("keydown", {
      type: "keydown",
      key,
      target: this,
      currentTarget: this.ownerDocument,
      defaultPrevented: false,
      propagationStopped: false,
      preventDefault() { this.defaultPrevented = true; },
      stopPropagation() { this.propagationStopped = true; },
    });
    if (key === " " && this.tagName === "BUTTON" && !event.defaultPrevented) {
      await this.dispatch("click");
    }
    return event;
  }

  get origin() {
    const href = this.getAttribute("href");
    return href ? new URL(href, this.ownerDocument.origin).origin : "";
  }

  get pathname() {
    const href = this.getAttribute("href");
    return href ? new URL(href, this.ownerDocument.origin).pathname : "";
  }

  get search() {
    const href = this.getAttribute("href");
    return href ? new URL(href, this.ownerDocument.origin).search : "";
  }
}


class FocusDocument {
  constructor(origin) {
    this.origin = origin;
    this.elements = [];
    this.listeners = new Map();
    this.body = new FocusElement(this, "body");
    this.body.classList = { add() {}, contains() { return false; }, remove() {} };
    this.activeElement = this.body;
    this.cookie = "selfecho_csrf=focus-token";
    this.appElement = null;
    this.primaryNavigation = { hidden: false };
  }

  addEventListener(type, listener) {
    if (!this.listeners.has(type)) this.listeners.set(type, []);
    this.listeners.get(type).push(listener);
  }

  async dispatch(type, event) {
    for (const listener of this.listeners.get(type) || []) await listener(event);
    return event;
  }

  replaceMarkup(markup) {
    if (this.activeElement !== this.body && this.activeElement !== this.appElement) {
      this.activeElement = this.body;
    }
    this.elements.forEach((element) => { element.isConnected = false; });
    this.elements = [];
    this.appendMarkup(markup);
  }

  appendMarkup(markup) {
    const tagPattern = /<([a-z][\w-]*)\b([^>]*)>/gi;
    for (const match of markup.matchAll(tagPattern)) {
      this.elements.push(
        new FocusElement(this, match[1], parseAttributes(match[2])),
      );
    }
  }

  querySelector(selector) {
    if (selector === "#app") return this.appElement;
    if (selector === "#primary-navigation") return this.primaryNavigation;
    return this.elements.find((element) => element.matchesSelector(selector)) || null;
  }

  querySelectorAll(selector) {
    if (selector === ".topbar nav a") return [];
    return this.elements.filter((element) => element.matchesSelector(selector));
  }
}


function dashboardItem(id, status = "active") {
  return {
    id,
    title: `Item ${id}`,
    importance: "medium",
    urgency: "medium",
    deadline: null,
    estimated_time: null,
    status,
    completed_at: status === "completed" ? "2026-09-13T00:00:00Z" : null,
    trashed_at: status === "trash" ? "2026-09-13T00:00:00Z" : null,
    status_before_trash: status === "trash" ? "active" : null,
    priority_score: status === "active" ? 2 : null,
    reminder: null,
    show_reminder_prompt: false,
  };
}


function dashboardResponse(items, { page = 1, totalItems = items.length } = {}) {
  return {
    sortable_items: items,
    needs_confirmation: [],
    pending_inputs: [],
    failed_inputs: [],
    due_reminders: [],
    page,
    page_size: 100,
    total_items: totalItems,
    total_pages: Math.max(1, Math.ceil(totalItems / 100)),
  };
}


function mobileRuleDeclarations(selector) {
  const mobileStart = stylesSource.indexOf("@media (max-width: 520px)");
  const narrowStart = stylesSource.indexOf("@media (max-width: 380px)", mobileStart);
  assert.notEqual(mobileStart, -1, "mobile stylesheet block must exist");
  assert.notEqual(narrowStart, -1, "narrow stylesheet block must exist");
  const mobileStyles = stylesSource.slice(mobileStart, narrowStart);
  const escapedSelector = selector.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  const match = mobileStyles.match(new RegExp(`${escapedSelector}\\s*\\{([^}]*)\\}`));
  assert.ok(match, `${selector} must have a mobile rule`);
  return match[1];
}


function createFocusHarness({ status = "active", response }) {
  const origin = "https://selfecho.example";
  const document = new FocusDocument(origin);
  const location = {
    origin,
    pathname: "/dashboard",
    search: status === "active" ? "" : `?status=${status}`,
  };
  const appElement = {
    _markup: "",
    get innerHTML() { return this._markup; },
    set innerHTML(markup) {
      this._markup = markup;
      document.replaceMarkup(markup);
    },
    insertAdjacentHTML(_position, markup) {
      this._markup += markup;
      document.appendMarkup(markup);
    },
    replaceChildren() {
      this.innerHTML = "";
    },
    focus() { document.activeElement = this; },
  };
  document.appElement = appElement;
  const confirmations = [];
  const timers = new Map();
  let timerId = 0;
  const window = {
    addEventListener() {},
    clearTimeout(id) { timers.delete(id); },
    confirm(message) { confirmations.push(message); return true; },
    location,
    scrollTo() {},
    setTimeout(callback) {
      timerId += 1;
      timers.set(timerId, callback);
      return timerId;
    },
  };
  const history = {
    pushState(_state, _unused, destination) { this.replaceState({}, "", destination); },
    replaceState(_state, _unused, destination) {
      const url = new URL(destination, origin);
      location.pathname = url.pathname;
      location.search = url.search;
    },
  };
  const context = vm.createContext({
    console,
    document,
    fetch: async () => { throw new Error("unexpected fetch"); },
    Headers,
    history,
    HTMLElement: FocusElement,
    localStorage: { getItem() { return null; }, setItem() {} },
    navigator: {},
    sessionStorage: { getItem() { return null; }, removeItem() {}, setItem() {} },
    URL,
    URLSearchParams,
    window,
  });
  const instrumented = frontendSource.replace(
    /^void initializeAuthentication\(\);$/m,
    "globalThis.__startupPromise = Promise.resolve();",
  );
  vm.runInContext(instrumented, context);
  context.__dashboardData = response;
  vm.runInContext(
    `
      globalThis.__bulkRequests = [];
      api = async (requestPath, options = {}) => {
        if (requestPath === "/api/items/bulk-lifecycle") {
          const payload = JSON.parse(options.body);
          globalThis.__bulkRequests.push(payload);
          return { action: payload.action, affected_ids: payload.item_ids };
        }
        if (requestPath.startsWith("/api/items?")) return globalThis.__dashboardData;
        throw new Error("unexpected API path: " + requestPath);
      };
    `,
    context,
  );
  return {
    appElement,
    confirmations,
    context,
    document,
    async render() {
      await vm.runInContext("renderDashboard({ silent: true })", context);
    },
    evaluate(source) { return vm.runInContext(source, context); },
    query(selector) { return document.querySelector(selector); },
  };
}


async function settleRenders() {
  for (let index = 0; index < 6; index += 1) {
    await new Promise((resolve) => setImmediate(resolve));
  }
}


test("selection rerenders preserve keyboard focus and cancel restores its entry", async () => {
  const harness = createFocusHarness({
    response: dashboardResponse([dashboardItem(1), dashboardItem(2)]),
  });
  await harness.render();

  const selectionEntry = harness.query("#selection-entry");
  selectionEntry.focus();
  assert.equal(harness.document.activeElement, selectionEntry);
  await selectionEntry.press("Enter");
  await settleRenders();

  let firstItem = harness.query('[data-selectable-item][data-item-id="1"]');
  assert.equal(harness.document.activeElement, firstItem);
  await firstItem.press(" ");
  await settleRenders();
  firstItem = harness.query('[data-selectable-item][data-item-id="1"]');
  assert.equal(firstItem.getAttribute("aria-selected"), "true");
  assert.equal(harness.document.activeElement, firstItem);

  let secondItem = harness.query('[data-selectable-item][data-item-id="2"]');
  secondItem.focus();
  await secondItem.press("Enter");
  await settleRenders();
  secondItem = harness.query('[data-selectable-item][data-item-id="2"]');
  assert.equal(secondItem.getAttribute("aria-selected"), "true");
  assert.equal(harness.document.activeElement, secondItem);

  await harness.query("[data-cancel-selection]").dispatch("click");
  await settleRenders();
  assert.equal(harness.document.activeElement, harness.query("#selection-entry"));
  assert.equal(harness.evaluate("dashboardSelection.active"), false);
});


test("a focused item removed by a rerender falls back to the selection toolbar", async () => {
  const harness = createFocusHarness({
    response: dashboardResponse([dashboardItem(1), dashboardItem(2)]),
  });
  await harness.render();
  await harness.query("#selection-entry").dispatch("click");
  await settleRenders();
  let firstItem = harness.query('[data-selectable-item][data-item-id="1"]');
  await firstItem.press(" ");
  await settleRenders();
  firstItem = harness.query('[data-selectable-item][data-item-id="1"]');
  firstItem.focus();

  harness.context.__dashboardData = dashboardResponse([dashboardItem(2)]);
  await harness.evaluate(`renderDashboard({
    silent: true,
    focusIntent: createDashboardFocusIntent("item", 1),
  })`);

  const fallback = harness.document.activeElement;
  assert.equal(fallback.dataset.selectionFocusFallback, "");
  assert.equal(fallback.isConnected, true);
  assert.equal(harness.evaluate("dashboardSelection.ids.has(1)"), false);
});


test("focus intent from an old lifecycle view is never restored", async () => {
  const harness = createFocusHarness({
    response: dashboardResponse([dashboardItem(1)]),
  });
  await harness.render();
  const item = harness.query('[data-selectable-item][data-item-id="1"]');
  item.focus();
  harness.evaluate(`
    globalThis.__staleFocusIntent = createDashboardFocusIntent("item", 1);
    resetDashboardSelection();
    history.replaceState({}, "", "/dashboard?status=completed");
  `);
  harness.document.activeElement = harness.document.body;

  harness.evaluate("restoreDashboardFocus(globalThis.__staleFocusIntent)");

  assert.equal(harness.document.activeElement, harness.document.body);
});


test("101 Trash items expose and submit one truthful backend-sized page", async () => {
  const items = Array.from({ length: 100 }, (_unused, index) =>
    dashboardItem(index + 1, "trash")
  );
  const harness = createFocusHarness({
    status: "trash",
    response: dashboardResponse(items, { totalItems: 101 }),
  });
  await harness.render();

  assert.match(harness.appElement.innerHTML, /永久删除本页事项/);
  assert.doesNotMatch(harness.appElement.innerHTML, />清空回收站<\/button>/);
  assert.match(harness.appElement.innerHTML, /第 1 \/ 2 页，本页 100 项，共 101 项/);

  await harness.query("#selection-entry").dispatch("click");
  await settleRenders();
  await harness.query("[data-select-all]").dispatch("click");
  await settleRenders();
  assert.equal(harness.evaluate("dashboardSelection.ids.size"), 100);
  assert.equal(harness.document.activeElement.dataset.selectAll, "");

  await harness.query("#clear-trash-button").dispatch("click");
  await settleRenders();
  const requests = JSON.parse(
    harness.evaluate("JSON.stringify(globalThis.__bulkRequests)"),
  );
  assert.equal(requests.length, 1);
  assert.equal(requests[0].action, "permanently_delete");
  assert.equal(requests[0].item_ids.length, 100);
  assert.deepEqual(requests[0].item_ids, items.map((item) => item.id));
  assert.match(harness.confirmations[0], /共有 101 个事项/);
  assert.match(harness.confirmations[0], /本页显示的 100 个事项/);
});


test("Current and Detail hide backend failure diagnostics behind shared user copy", async () => {
  const sentinel = "INTERNAL_SENTINEL_AI_API_KEY_DO_NOT_RENDER";
  const failedInput = {
    id: 41,
    item_id: null,
    original_text: "必须继续保留的失败原文",
    input_method: "text",
    processing_status: "failed",
    failure_type: "configuration",
    failure_message: sentinel,
    created_time: "2026-09-14T08:00:00Z",
    voice_segment_ids: [],
  };
  const response = dashboardResponse([]);
  response.failed_inputs = [failedInput];
  const harness = createFocusHarness({ response });

  await harness.render();

  assert.doesNotMatch(harness.appElement.innerHTML, new RegExp(sentinel));
  assert.match(harness.appElement.innerHTML, /这条记录暂时没有处理完成，可以稍后重试。/);
  assert.match(harness.appElement.innerHTML, />重新整理<\/button>/);
  assert.match(harness.appElement.innerHTML, />永久删除这条记录<\/button>/);

  const detailHistory = harness.evaluate(
    `inputHistoryItem(${JSON.stringify({ ...failedInput, item_id: 7 })})`,
  );
  assert.doesNotMatch(detailHistory, new RegExp(sentinel));
  assert.match(detailHistory, /这条记录暂时没有处理完成，可以稍后重试。/);
  assert.match(detailHistory, /重新整理这条记录/);
  assert.match(frontendSource, /data\.inputs\.map\(inputHistoryItem\)/);

  const fallback = JSON.parse(
    harness.evaluate(`JSON.stringify(captureFailureDisplay({
      failure_type: "future_unknown_type",
      failure_message: ${JSON.stringify(sentinel)},
    }))`),
  );
  assert.equal(fallback.title, "暂时没有整理成功");
  assert.equal(fallback.message, "这条记录暂时没有处理完成，可以稍后重试。");
  assert.doesNotMatch(JSON.stringify(fallback), new RegExp(sentinel));
});


test("mobile Reminder and Edit content bind horizontal safe areas to their own rules", () => {
  for (const [selector, basePadding] of [
    [".reminder-surface", "18px"],
    [".edit-surface-header", "16px"],
    [".edit-scroll-region", "16px"],
  ]) {
    const declarations = mobileRuleDeclarations(selector);
    assert.match(
      declarations,
      new RegExp(
        `padding-left:\\s*max\\(${basePadding},\\s*env\\(safe-area-inset-left,\\s*0px\\)\\)`,
      ),
    );
    assert.match(
      declarations,
      new RegExp(
        `padding-right:\\s*max\\(${basePadding},\\s*env\\(safe-area-inset-right,\\s*0px\\)\\)`,
      ),
    );
  }

  assert.match(mobileRuleDeclarations(".edit-surface-header"), /safe-area-inset-top/);
  assert.match(mobileRuleDeclarations(".edit-scroll-region"), /safe-area-inset-bottom/);
});

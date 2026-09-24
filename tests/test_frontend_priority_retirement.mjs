import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("../app/static/app.js", import.meta.url), "utf8");
const styles = readFileSync(new URL("../app/static/styles.css", import.meta.url), "utf8");

function legacyItem(level = "high") {
  return {
    id: 7, title: "保存的记录", type: "note", status: "active",
    importance: level, urgency: level, priority_score: 3,
    deadline: "2026-09-18T23:30:00Z", estimated_time: 25,
    next_action: "查看材料", extra_information: { concern: "保留用户背景" },
    updated_time: "2026-09-19T00:00:00Z",
  };
}

// Minimal DOM for running the real Detail renderer and its submit listener.
function harness(item = legacyItem(), reminder = null) {
  const elements = new Map();
  const requests = [];
  const confirmations = [];
  let accept = true;
  class Element {
    constructor(value = "") {
      this.value = value;
      this.dataset = {};
      this.listeners = new Map();
      this.classList = { add() {}, remove() {} };
    }
    addEventListener(type, handler) { this.listeners.set(type, handler); }
    setAttribute() {}
    removeAttribute() {}
    focus() {}
    matches() { return false; }
    querySelector() { return new Element(); }
    async submit() { await this.listeners.get("submit")({ preventDefault() {} }); }
    async click() { await this.listeners.get("click")(); }
  }
  const app = new Element();
  Object.defineProperty(app, "innerHTML", {
    get() { return this.markup; },
    set(markup) {
      this.markup = markup;
      elements.clear();
      for (const match of markup.matchAll(/<[^>]+\bid="([^"]+)"[^>]*>/g)) {
        const value = match[0].match(/\bvalue="([^"]*)"/)?.[1] ?? "";
        elements.set(`#${match[1]}`, new Element(value));
      }
      for (const match of markup.matchAll(/<button[^>]*class="[^"]*lifecycle-status-button[^"]*"[^>]*>/g)) {
        const button = new Element();
        button.dataset.status = match[0].match(/data-status="([^"]+)"/)[1];
        elements.set(`lifecycle-${button.dataset.status}`, button);
      }
    },
  });
  const context = vm.createContext({
    navigator: {}, HTMLElement: Element, URLSearchParams,
    document: {
      querySelector(selector) {
        if (selector === '#detail-pin-button[data-saving="true"]') {
          const button = elements.get('#detail-pin-button');
          return button?.dataset.saving === 'true' ? button : null;
        }
        if (selector.includes('#update-form[data-dirty')) {
          const form = elements.get('#update-form');
          return form?.dataset.dirty === 'true' ? form : null;
        }
        return selector === "#app" ? app : elements.get(selector) ?? null;
      },
      querySelectorAll(selector) {
        return selector === '.lifecycle-status-button'
          ? [...elements].filter(([key]) => key.startsWith('lifecycle-')).map(([, value]) => value)
          : [];
      },
      addEventListener() {}, body: new Element(),
    },
    window: {
      addEventListener() {}, clearTimeout() {}, setTimeout() {},
      confirm(message) { confirmations.push(message); return accept; },
    },
  });
  vm.runInContext(source.replace(/^void initializeAuthentication\(\);$/m, ""), context);
  context.fixture = { item, reminder, inputs: [] };
  context.testApi = async (path, options = {}) => {
    if (options.method === "PATCH") {
      const patch = JSON.parse(options.body);
      requests.push({ path, patch });
      Object.assign(item, patch);
      return item;
    }
    assert.equal(path, "/api/items/7");
    return context.fixture;
  };
  vm.runInContext('api = testApi; authentication.user = { timezone: "Asia/Shanghai" };', context);
  return {
    app, elements, item, requests, confirmations,
    acceptConfirmation(value) { accept = value; },
    setApi(handler) { context.testApi = handler; vm.runInContext('api = testApi;', context); },
    run(code) { return vm.runInContext(code, context); },
    async render() {
      await vm.runInContext("renderDetail(7)", context);
      assert.doesNotMatch(app.innerHTML, /无法读取事项/);
      // Browser textarea.value reflects decoded text content.
      elements.get("#edit-title").value = item.title;
      elements.get("#edit-next").value = item.next_action;
      elements.get("#edit-extra").value = JSON.stringify(item.extra_information);
    },
  };
}

test("Detail and editor ignore every legacy priority level while retaining other fields", async () => {
  for (const level of ["high", "medium", "low", "unknown", null, undefined]) {
    const h = harness(legacyItem(level), { id: 8, status: "needs_confirmation", source_expression: "过几天" });
    await h.render();
    assert.doesNotMatch(h.app.innerHTML, /importance|urgency|priority|重要性|紧急性|优先级|undefined/);
    for (const text of ["下一步", "查看材料", "截止日期", "2026年9月19日 07:30", "25 分钟", "保留用户背景", "原始记录", "事项状态", "设置时间", "关闭提醒"]) {
      assert.ok(h.app.innerHTML.includes(text), text);
    }
    assert.equal(h.elements.has("#edit-importance"), false);
    assert.equal(h.elements.has("#edit-urgency"), false);
    assert.equal(h.elements.get("#edit-deadline").value, "2026-09-19");
    assert.equal(JSON.stringify(h.run("changedPatch(fixture.item)")), "{}");
  }
});

test("Detail Pin uses target booleans and server responses without replacing draft DOM", async () => {
  const h = harness({ ...legacyItem(), is_pinned: false });
  await h.render();
  const button = h.elements.get('#detail-pin-button');
  const draft = h.elements.get('#update-text');
  draft.value = '尚未保存的原文';
  const editor = h.elements.get('#edit-title');
  editor.value = '尚未保存的标题';
  await button.click();
  assert.deepEqual(h.requests[0].patch, { is_pinned: true });
  assert.equal(button.textContent, '取消置顶');
  assert.match(h.elements.get('#detail-pin-state').textContent, /已置顶/);
  await button.click();
  assert.deepEqual(h.requests[1].patch, { is_pinned: false });
  assert.equal(button.textContent, '置顶显示');
  assert.equal(h.elements.get('#update-text'), draft);
  assert.equal(draft.value, '尚未保存的原文');
  assert.equal(editor.value, '尚未保存的标题');
  assert.deepEqual(h.confirmations, []);
});

test("Detail Pin does not optimistically claim success, prevents duplicate requests, and allows retry", async () => {
  const h = harness({ ...legacyItem(), is_pinned: false });
  await h.render();
  let rejectRequest;
  let calls = 0;
  h.setApi(() => { calls++; return new Promise((_, reject) => { rejectRequest = reject; }); });
  const button = h.elements.get('#detail-pin-button');
  const saving = button.click();
  assert.equal(button.disabled, true);
  assert.equal(button.dataset.saving, 'true');
  assert.equal(h.run('Boolean(detailRefreshBlocked())'), true);
  assert.equal(h.item.is_pinned, false);
  await button.click();
  assert.equal(calls, 1);
  rejectRequest(new Error('network unavailable'));
  await saving;
  assert.equal(h.item.is_pinned, false);
  assert.equal(button.disabled, false);
  assert.equal(button.dataset.saving, 'false');
  assert.equal(h.elements.get('#detail-pin-status').dataset.kind, 'error');
  h.setApi(async () => ({ ...h.item, is_pinned: false }));
  await button.click();
  // The response is authoritative even if it differs from the requested target.
  assert.equal(button.textContent, '置顶显示');
  assert.equal(h.item.is_pinned, false);
});

test("Lifecycle completion hides Pin even when an unsaved draft defers the page refresh", async () => {
  const h = harness({ ...legacyItem(), is_pinned: true });
  await h.render();
  h.elements.get('#update-form').dataset.dirty = 'true';
  h.elements.get('#update-text').value = '保留原文';
  await h.elements.get('lifecycle-completed').click();
  assert.deepEqual(h.requests[0].patch, { status: 'completed' });
  const button = h.elements.get('#detail-pin-button');
  assert.equal(button.hidden, true);
  assert.match(h.elements.get('#detail-pin-state').textContent, /已保留置顶设置/);
  await button.click();
  assert.equal(h.requests.length, 1);
  assert.equal(h.elements.get('#update-text').value, '保留原文');
});

test("History and Trash show retained Pin without a mutation control", async () => {
  for (const status of ['completed', 'trash']) {
    for (const is_pinned of [true, false]) {
      const h = harness({ ...legacyItem(), status, is_pinned });
      await h.render();
      assert.equal(h.elements.has('#detail-pin-button'), false);
      assert.match(h.app.innerHTML, is_pinned ? /已保留置顶设置/ : /未置顶/);
    }
  }
});

test("Detail title edit never submits or clears legacy values and preserves aware Deadline", async () => {
  const h = harness();
  await h.render();
  h.elements.get("#edit-title").value = "修改标题";
  await h.elements.get("#edit-form").submit();
  assert.deepEqual(h.requests, [{ path: "/api/items/7", patch: { title: "修改标题", confirmed_important_fields: false } }]);
  assert.deepEqual(h.confirmations, []);
  assert.equal(h.item.importance, "high");
  assert.equal(h.item.urgency, "high");
  assert.equal(h.item.deadline, "2026-09-18T23:30:00Z");
});

test("Deadline edit and removal still require explicit confirmation with the legacy API flag", async () => {
  for (const value of ["2026-09-21", ""]) {
    const h = harness();
    await h.render();
    h.elements.get("#edit-deadline").value = value;
    h.acceptConfirmation(false);
    await h.elements.get("#edit-form").submit();
    assert.equal(h.requests.length, 0);
    h.acceptConfirmation(true);
    await h.elements.get("#edit-form").submit();
    assert.deepEqual(h.requests[0].patch, { deadline: value || null, confirmed_important_fields: true });
    assert.deepEqual(h.confirmations, ["确认修改截止日期吗？", "确认修改截止日期吗？"]);
  }
});

test("Priority-only helpers and styles are gone while overdue and Reminder styles remain", () => {
  assert.doesNotMatch(source, /item\.(importance|urgency)|edit-importance|edit-urgency|quickPriorityButtons|function tag\(/);
  assert.doesNotMatch(styles, /quick-priority|priority-choice|card-signal\.(importance|urgency|unknown)|\.tag[ .{]/);
  assert.match(styles, /\.card-signal\.overdue/);
  assert.match(styles, /\.quick-reminder-choice/);
});

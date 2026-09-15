import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import path from "node:path";
import test from "node:test";
import vm from "node:vm";
import { fileURLToPath } from "node:url";


const testsDirectory = path.dirname(fileURLToPath(import.meta.url));
const frontendPath = path.join(testsDirectory, "..", "app", "static", "app.js");
const frontendSource = readFileSync(frontendPath, "utf8");


function createRow(itemId = 7) {
  const listeners = new Map();
  let capturedPointer = null;
  return {
    dataset: { itemId: String(itemId) },
    origin: "https://selfecho.example",
    pathname: `/items/${itemId}`,
    search: "",
    addEventListener(type, listener) {
      if (!listeners.has(type)) listeners.set(type, []);
      listeners.get(type).push(listener);
    },
    closest(selector) {
      if (selector === "[data-selectable-item]" || selector === "a[data-link]") {
        return this;
      }
      return null;
    },
    dispatch(type, properties = {}) {
      const event = { target: this, currentTarget: this, ...properties };
      for (const listener of listeners.get(type) || []) listener(event);
      return event;
    },
    getAttribute(name) {
      return name === "href" ? this.pathname : null;
    },
    hasPointerCapture(pointerId) {
      return capturedPointer === pointerId;
    },
    releasePointerCapture(pointerId) {
      if (capturedPointer === pointerId) capturedPointer = null;
    },
    setPointerCapture(pointerId) {
      capturedPointer = pointerId;
    },
  };
}


function createHarness() {
  const documentListeners = new Map();
  const timers = new Map();
  const rows = [];
  const historyPushes = [];
  let nextTimerId = 1;
  const genericElement = {
    addEventListener() {},
    classList: { add() {}, contains() { return false; }, remove() {} },
    dataset: {},
    focus() {},
    hidden: false,
    innerHTML: "",
    isConnected: true,
    querySelector() { return genericElement; },
    querySelectorAll() { return []; },
    removeAttribute() {},
    setAttribute() {},
    textContent: "",
    value: "",
  };
  const appElement = { focus() {}, innerHTML: "", replaceChildren() {} };
  const document = {
    activeElement: null,
    body: { classList: { add() {}, remove() {} } },
    cookie: "selfecho_csrf=lifecycle-token",
    addEventListener(type, listener) {
      if (!documentListeners.has(type)) documentListeners.set(type, []);
      documentListeners.get(type).push(listener);
    },
    querySelector(selector) {
      if (selector === "#app") return appElement;
      if (selector === "#primary-navigation") return { hidden: false };
      return null;
    },
    querySelectorAll(selector) {
      if (selector === "[data-selectable-item]") return rows;
      return [];
    },
  };
  const window = {
    addEventListener() {},
    clearTimeout(id) { timers.delete(id); },
    confirm() { return true; },
    location: {
      origin: "https://selfecho.example",
      pathname: "/dashboard",
      search: "",
    },
    scrollTo() {},
    setTimeout(callback) {
      const id = nextTimerId++;
      timers.set(id, callback);
      return id;
    },
  };
  const context = vm.createContext({
    console,
    document,
    fetch: async () => ({ ok: true, status: 200, async json() { return {}; } }),
    Headers,
    history: {
      pushState(_state, _unused, destination) { historyPushes.push(destination); },
      replaceState() {},
    },
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
  vm.runInContext(
    "globalThis.__dashboardRenders = 0; renderDashboard = async () => { globalThis.__dashboardRenders += 1; };",
    context,
  );

  return {
    context,
    documentListeners,
    historyPushes,
    rows,
    timers,
    evaluate(source) { return vm.runInContext(source, context); },
  };
}


test("Current and History are primary while Trash is a secondary destination", () => {
  const harness = createHarness();
  const primaryNavigation = harness.evaluate("lifecycleNavigation('active')");
  const trashNavigation = harness.evaluate("lifecycleNavigation('trash')");

  assert.match(primaryNavigation, /当前/);
  assert.match(primaryNavigation, /历史/);
  assert.doesNotMatch(primaryNavigation, /回收站/);
  assert.equal(trashNavigation, "");
  assert.match(frontendSource, /class="trash-entry"[^>]+status=trash/);
});


test("History and Trash use truthful lightweight lifecycle text", () => {
  const harness = createHarness();

  assert.equal(harness.evaluate("historyCompletionText(null)"), "完成时间未知");
  assert.equal(
    harness.evaluate("historyCompletionText('2026-09-13T00:00:00+00:00')"),
    "9 月 13 日完成",
  );
  assert.equal(
    harness.evaluate(
      "trashRemainingText('2026-09-01T00:00:00+00:00', Date.parse('2026-09-08T00:00:00+00:00'))",
    ),
    "23 天后永久删除",
  );
  const historyRow = harness.evaluate(`lifecycleListRow({
    id: 4,
    title: 'Quiet history',
    completed_at: null,
    trashed_at: null,
  }, 'completed')`);
  assert.match(historyRow, /lifecycle-list-row/);
  assert.match(historyRow, /完成时间未知/);
  assert.doesNotMatch(historyRow, /card-signals|priority_score|reminder/);
  assert.match(frontendSource, /事项将在移入回收站 30 天后永久删除。/);
});


test("long press selects its row and suppresses the following navigation click", () => {
  const harness = createHarness();
  const row = createRow(7);
  harness.rows.push(row);
  harness.evaluate("bindDashboardLongPressRows('active')");

  row.dispatch("pointerdown", {
    button: 0,
    pointerId: 1,
    clientX: 10,
    clientY: 20,
  });
  const longPressTimerId = Math.min(...harness.timers.keys());
  harness.timers.get(longPressTimerId)();

  assert.equal(harness.evaluate("dashboardSelection.active"), true);
  assert.equal(harness.evaluate("dashboardSelection.ids.has(7)"), true);
  assert.equal(harness.evaluate("globalThis.__dashboardRenders"), 1);
  const contextMenu = row.dispatch("contextmenu", {
    prevented: false,
    preventDefault() { this.prevented = true; },
  });
  assert.equal(contextMenu.prevented, true);

  const click = {
    target: row,
    prevented: false,
    stopped: false,
    preventDefault() { this.prevented = true; },
    stopPropagation() { this.stopped = true; },
  };
  for (const listener of harness.documentListeners.get("click") || []) listener(click);
  assert.equal(click.prevented, true);
  assert.equal(click.stopped, true);
  assert.deepEqual(harness.historyPushes, []);
});


test("movement and pointer cancellation cancel long press while normal tap navigates", () => {
  const harness = createHarness();
  const row = createRow(8);
  harness.rows.push(row);
  harness.evaluate("bindDashboardLongPressRows('completed')");

  row.dispatch("pointerdown", {
    button: 0,
    pointerId: 2,
    clientX: 0,
    clientY: 0,
  });
  row.dispatch("pointermove", { pointerId: 2, clientX: 30, clientY: 0 });
  assert.equal(harness.timers.size, 0);
  assert.equal(harness.evaluate("dashboardSelection.active"), false);

  row.dispatch("pointerdown", {
    button: 0,
    pointerId: 3,
    clientX: 0,
    clientY: 0,
  });
  row.dispatch("pointercancel", { pointerId: 3 });
  assert.equal(harness.timers.size, 0);
  assert.equal(harness.evaluate("dashboardSelection.active"), false);

  row.dispatch("pointerdown", {
    button: 0,
    pointerId: 4,
    clientX: 0,
    clientY: 0,
  });
  row.dispatch("pointerup", { pointerId: 4 });
  assert.equal(harness.timers.size, 0);

  row.dispatch("pointerdown", {
    button: 0,
    pointerId: 5,
    clientX: 0,
    clientY: 0,
  });
  row.dispatch("lostpointercapture", { pointerId: 5 });
  assert.equal(harness.timers.size, 0);
  assert.equal(harness.evaluate("dashboardSelection.active"), false);

  const click = {
    target: row,
    prevented: false,
    preventDefault() { this.prevented = true; },
  };
  for (const listener of harness.documentListeners.get("click") || []) listener(click);
  assert.equal(click.prevented, true);
  assert.deepEqual(harness.historyPushes, ["/items/8"]);
});


test("a pending long press cannot leak across a route or lifecycle-view change", () => {
  const harness = createHarness();
  const row = createRow(10);
  harness.rows.push(row);
  harness.evaluate("bindDashboardLongPressRows('active')");
  row.dispatch("pointerdown", {
    button: 0,
    pointerId: 6,
    clientX: 0,
    clientY: 0,
  });
  const timerId = Math.min(...harness.timers.keys());
  harness.evaluate("resetDashboardSelection()");
  harness.timers.get(timerId)();

  assert.equal(harness.evaluate("dashboardSelection.active"), false);
  assert.equal(harness.evaluate("dashboardSelection.ids.size"), 0);
});


test("mouse right click stays native while touch suppression covers press, release, and grace", () => {
  const harness = createHarness();
  const row = createRow(21);
  harness.rows.push(row);
  harness.evaluate("bindDashboardLongPressRows('active')");

  const bareContextMenu = row.dispatch("contextmenu", {
    prevented: false,
    preventDefault() { this.prevented = true; },
  });
  assert.equal(bareContextMenu.prevented, false);

  const mouseContextMenu = row.dispatch("contextmenu", {
    button: 2,
    pointerType: "mouse",
    prevented: false,
    preventDefault() { this.prevented = true; },
  });
  assert.equal(mouseContextMenu.prevented, false);

  const penBarrelContextMenu = row.dispatch("contextmenu", {
    button: 2,
    pointerType: "pen",
    prevented: false,
    preventDefault() { this.prevented = true; },
  });
  assert.equal(penBarrelContextMenu.prevented, false);

  row.dispatch("pointerdown", {
    button: 0,
    pointerId: 21,
    pointerType: "touch",
    clientX: 5,
    clientY: 6,
  });
  assert.equal(harness.evaluate("activeSelectableTouchPress?.pointerId"), 21);
  assert.equal(harness.evaluate("activeSelectableTouchPress?.startX"), 5);

  const duringPressContextMenu = row.dispatch("contextmenu", {
    button: 0,
    pointerType: "touch",
    prevented: false,
    preventDefault() { this.prevented = true; },
  });
  assert.equal(duringPressContextMenu.prevented, true);

  row.dispatch("pointerup", { pointerId: 21 });
  for (const listener of harness.documentListeners.get("pointerup") || []) {
    listener({ pointerId: 21 });
  }
  assert.equal(harness.evaluate("activeSelectableTouchPress"), null);
  assert.equal(harness.evaluate("recentSelectableTouchPress?.itemId"), 21);

  const afterPointerUpContextMenu = row.dispatch("contextmenu", {
    prevented: false,
    preventDefault() { this.prevented = true; },
  });
  assert.equal(afterPointerUpContextMenu.prevented, true);

  const mouseDuringGraceContextMenu = row.dispatch("contextmenu", {
    button: 2,
    pointerType: "mouse",
    prevented: false,
    preventDefault() { this.prevented = true; },
  });
  assert.equal(mouseDuringGraceContextMenu.prevented, false);

  const touchDuringGraceContextMenu = row.dispatch("contextmenu", {
    button: 0,
    pointerType: "touch",
    prevented: false,
    preventDefault() { this.prevented = true; },
  });
  assert.equal(touchDuringGraceContextMenu.prevented, true);

  const penDuringGraceContextMenu = row.dispatch("contextmenu", {
    button: 0,
    pointerType: "pen",
    prevented: false,
    preventDefault() { this.prevented = true; },
  });
  assert.equal(penDuringGraceContextMenu.prevented, true);

  const graceTimerId = Math.min(...harness.timers.keys());
  harness.timers.get(graceTimerId)();
  assert.equal(harness.evaluate("recentSelectableTouchPress"), null);

  const afterGraceContextMenu = row.dispatch("contextmenu", {
    prevented: false,
    preventDefault() { this.prevented = true; },
  });
  assert.equal(afterGraceContextMenu.prevented, false);

  const mouseAfterGraceContextMenu = row.dispatch("contextmenu", {
    button: 2,
    pointerType: "mouse",
    prevented: false,
    preventDefault() { this.prevented = true; },
  });
  assert.equal(mouseAfterGraceContextMenu.prevented, false);
});


test("pointer-id aware clearing arms the grace only for the matching pointer", () => {
  const harness = createHarness();
  const row = createRow(22);
  harness.rows.push(row);
  harness.evaluate("bindDashboardLongPressRows('active')");

  row.dispatch("pointerdown", {
    button: 0,
    pointerId: 31,
    pointerType: "touch",
    clientX: 0,
    clientY: 0,
  });
  assert.equal(harness.evaluate("activeSelectableTouchPress?.pointerId"), 31);

  for (const listener of harness.documentListeners.get("pointerup") || []) {
    listener({ pointerId: 99 });
  }
  assert.equal(harness.evaluate("activeSelectableTouchPress?.pointerId"), 31);
  assert.equal(harness.evaluate("recentSelectableTouchPress"), null);

  for (const listener of harness.documentListeners.get("pointercancel") || []) {
    listener({ pointerId: 31 });
  }
  assert.equal(harness.evaluate("activeSelectableTouchPress"), null);
  assert.equal(harness.evaluate("recentSelectableTouchPress?.itemId"), 22);

  const afterCancelContextMenu = row.dispatch("contextmenu", {
    prevented: false,
    preventDefault() { this.prevented = true; },
  });
  assert.equal(afterCancelContextMenu.prevented, true);

  const graceTimerId = Math.max(...harness.timers.keys());
  harness.timers.get(graceTimerId)();
  assert.equal(harness.evaluate("recentSelectableTouchPress"), null);

  const afterGraceContextMenu = row.dispatch("contextmenu", {
    prevented: false,
    preventDefault() { this.prevented = true; },
  });
  assert.equal(afterGraceContextMenu.prevented, false);
});


test("touch suppression survives the selection re-render and still suppresses the delayed touch contextmenu", () => {
  const harness = createHarness();
  const row = createRow(32);
  harness.rows.push(row);
  harness.evaluate("bindDashboardLongPressRows('active')");

  row.dispatch("pointerdown", {
    button: 0,
    pointerId: 32,
    pointerType: "touch",
    clientX: 0,
    clientY: 0,
  });
  const longPressTimerId = Math.min(...harness.timers.keys());
  harness.timers.get(longPressTimerId)();
  assert.equal(harness.evaluate("dashboardSelection.active"), true);
  assert.equal(harness.evaluate("dashboardSelection.ids.has(32)"), true);

  const rerenderedRow = createRow(32);
  harness.rows.push(rerenderedRow);
  harness.evaluate("bindDashboardLongPressRows('active')");

  const duringPressContextMenu = rerenderedRow.dispatch("contextmenu", {
    prevented: false,
    preventDefault() { this.prevented = true; },
  });
  assert.equal(duringPressContextMenu.prevented, true);

  row.dispatch("pointerup", { pointerId: 32 });
  for (const listener of harness.documentListeners.get("pointerup") || []) {
    listener({ pointerId: 32 });
  }
  assert.equal(harness.evaluate("activeSelectableTouchPress"), null);
  assert.equal(harness.evaluate("recentSelectableTouchPress?.itemId"), 32);

  const delayedContextMenu = rerenderedRow.dispatch("contextmenu", {
    prevented: false,
    preventDefault() { this.prevented = true; },
  });
  assert.equal(delayedContextMenu.prevented, true);

  const graceTimerId = Math.max(...harness.timers.keys());
  harness.timers.get(graceTimerId)();
  assert.equal(harness.evaluate("recentSelectableTouchPress"), null);

  const afterGraceContextMenu = rerenderedRow.dispatch("contextmenu", {
    prevented: false,
    preventDefault() { this.prevented = true; },
  });
  assert.equal(afterGraceContextMenu.prevented, false);
});


test("movement past the scroll threshold leaves no delayed blocker on the trash view", () => {
  const harness = createHarness();
  const trashRow = createRow(41);
  harness.rows.push(trashRow);
  harness.evaluate("bindDashboardLongPressRows('trash')");

  trashRow.dispatch("pointerdown", {
    button: 0,
    pointerId: 41,
    pointerType: "touch",
    clientX: 0,
    clientY: 0,
  });
  trashRow.dispatch("pointermove", {
    pointerId: 41,
    pointerType: "touch",
    clientX: 0,
    clientY: 30,
  });
  assert.equal(harness.evaluate("activeSelectableTouchPress"), null);
  assert.equal(harness.timers.size, 0);
  assert.equal(harness.evaluate("dashboardSelection.active"), false);

  trashRow.dispatch("pointerup", { pointerId: 41 });
  for (const listener of harness.documentListeners.get("pointerup") || []) {
    listener({ pointerId: 41 });
  }
  assert.equal(harness.evaluate("recentSelectableTouchPress"), null);

  const scrolledContextMenu = trashRow.dispatch("contextmenu", {
    prevented: false,
    preventDefault() { this.prevented = true; },
  });
  assert.equal(scrolledContextMenu.prevented, false);

  trashRow.dispatch("pointerdown", {
    button: 0,
    pointerId: 42,
    pointerType: "touch",
    clientX: 0,
    clientY: 0,
  });
  trashRow.dispatch("pointermove", {
    pointerId: 42,
    pointerType: "touch",
    clientX: 3,
    clientY: 4,
  });
  assert.equal(harness.evaluate("activeSelectableTouchPress?.pointerId"), 42);
  assert.equal(harness.timers.size, 1);

  for (const listener of harness.documentListeners.get("pointercancel") || []) {
    listener({ pointerId: 42 });
  }
  assert.equal(harness.evaluate("activeSelectableTouchPress"), null);
  assert.equal(harness.evaluate("recentSelectableTouchPress?.itemId"), 41);

  const afterCancelContextMenu = trashRow.dispatch("contextmenu", {
    prevented: false,
    preventDefault() { this.prevented = true; },
  });
  assert.equal(afterCancelContextMenu.prevented, true);

  const graceTimerId = Math.max(...harness.timers.keys());
  harness.timers.get(graceTimerId)();
  assert.equal(harness.evaluate("recentSelectableTouchPress"), null);

  const afterGraceContextMenu = trashRow.dispatch("contextmenu", {
    prevented: false,
    preventDefault() { this.prevented = true; },
  });
  assert.equal(afterGraceContextMenu.prevented, false);
});


test("selection tap, keyboard Space, accessible entry, and route leave semantics exist", () => {
  const harness = createHarness();
  const row = createRow(9);
  harness.evaluate(`
    dashboardSelection.active = true;
    dashboardSelection.status = 'trash';
    dashboardSelection.ids.add(9);
  `);
  const click = {
    target: row,
    preventDefault() { this.prevented = true; },
  };
  for (const listener of harness.documentListeners.get("click") || []) listener(click);
  assert.equal(harness.evaluate("dashboardSelection.ids.has(9)"), false);

  const keydown = {
    target: row,
    key: " ",
    preventDefault() { this.prevented = true; },
  };
  for (const listener of harness.documentListeners.get("keydown") || []) listener(keydown);
  assert.equal(keydown.prevented, true);
  assert.equal(harness.evaluate("dashboardSelection.ids.has(9)"), true);

  assert.match(frontendSource, /id="selection-entry"/);
  assert.match(frontendSource, /aria-selected=/);
  assert.match(frontendSource, /aria-live="polite"/);
  harness.evaluate("renderRoute()");
  assert.equal(harness.evaluate("dashboardSelection.active"), false);
  assert.equal(harness.evaluate("dashboardSelection.ids.size"), 0);
  harness.evaluate(`
    dashboardSelection.active = true;
    dashboardSelection.status = 'active';
    dashboardSelection.ids.add(11);
    reconcileDashboardSelectionView('completed');
  `);
  assert.equal(harness.evaluate("dashboardSelection.active"), false);
  assert.equal(harness.evaluate("dashboardSelection.ids.size"), 0);
});


test("each lifecycle view exposes only its frozen batch actions", () => {
  const harness = createHarness();
  assert.deepEqual(
    JSON.parse(harness.evaluate("JSON.stringify(selectionActions('active').map((entry) => entry.slice(0, 2)))")),
    [["complete", "标记为已完成"], ["move_to_trash", "移入回收站"]],
  );
  assert.deepEqual(
    JSON.parse(harness.evaluate("JSON.stringify(selectionActions('completed').map((entry) => entry.slice(0, 2)))")),
    [["restore_to_current", "恢复为当前"], ["move_to_trash", "移入回收站"]],
  );
  assert.deepEqual(
    JSON.parse(harness.evaluate("JSON.stringify(selectionActions('trash').map((entry) => entry.slice(0, 2)))")),
    [["restore_from_trash", "恢复"], ["permanently_delete", "永久删除"]],
  );
  assert.match(frontendSource, /data-select-all/);
  assert.match(frontendSource, /全选本页/);
  harness.evaluate(`
    dashboardSelection.visibleIds = [4, 5, 6];
    selectAllVisibleDashboardItems();
  `);
  assert.equal(harness.evaluate("dashboardSelection.ids.size"), 3);
  assert.equal(harness.evaluate("dashboardSelection.ids.has(5)"), true);
  assert.match(frontendSource, /const snapshotIds = \[\.\.\.dashboardSelection\.visibleIds\]/);
  assert.match(frontendSource, /submitBulkLifecycle\("permanently_delete", snapshotIds\)/);
  assert.doesNotMatch(frontendSource, /Promise\.all\([^)]*\/api\/items/);
});


test("moving to Trash has no confirmation while destructive actions keep it", () => {
  const lifecycleHandler = frontendSource.split(
    'document.querySelectorAll(".lifecycle-status-button")',
    2,
  )[1].split('const deleteButton = document.querySelector', 1)[0];

  assert.doesNotMatch(lifecycleHandler, /confirmTrashMove|window\.confirm/);
  assert.match(frontendSource, /确认永久删除已选择的/);
  assert.match(frontendSource, /确认清空回收站中的/);
  assert.match(frontendSource, /确认永久删除本页显示的/);
  assert.match(frontendSource, /确认永久删除这个事项及其原始记录吗/);
  assert.doesNotMatch(frontendSource, /id="edit-status"/);
});

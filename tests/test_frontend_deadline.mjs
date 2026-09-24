import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("../app/static/app.js", import.meta.url), "utf8");
function harness(timezone = "Asia/Shanghai") {
  const fields = new Map();
  const context = vm.createContext({
    navigator: {},
    document: { querySelector: (selector) => fields.get(selector), addEventListener() {} },
    window: { addEventListener() {} },
  });
  vm.runInContext(source.replace(/^void initializeAuthentication\(\);$/m, ""), context);
  context.zone = timezone;
  vm.runInContext("authentication.user = { timezone: zone };", context);
  return { fields, run: (code) => vm.runInContext(code, context) };
}

test("Deadline projection and overdue use profile timezone, preserving precision", () => {
  const { run } = harness();
  for (const [value, localDate, localTime, overdue] of [
    ["2026-09-19", "2026-09-19", null, false],
    ["2026-09-18", "2026-09-18", null, true],
    ["2026-09-18T23:30:00+00:00", "2026-09-19", "07:30", true],
    ["2026-09-19T00:00:00Z", "2026-09-19", "08:00", false],
    ["2026-09-19T00:30:00", "2026-09-19", "00:30", true],
    ["2026-09-19T08:00:00", "2026-09-19", "08:00", false],
  ]) {
    const state = run(`deadlineTemporalState(${JSON.stringify(value)}, new Date("2026-09-19T00:00:00Z"))`);
    assert.equal(state.localDate, localDate);
    assert.equal(state.localTime?.slice(0, 5) ?? null, localTime);
    assert.equal(state.overdue, overdue);
  }
  assert.equal(run('deadlineDisplay("2026-09-18T23:30:00Z")'), "2026年9月19日 07:30");
  assert.equal(run('deadlinePresentation("2026-09-19", new Date("2026-09-19T15:59:59Z")).text'), "今天截止");
  assert.equal(run('deadlinePresentation("2026-09-19", new Date("2026-09-19T16:00:00Z")).kind'), "overdue");
});

test("Date-only stays floating west of UTC and aware instants cross calendar days", () => {
  const { run } = harness("America/Los_Angeles");
  assert.equal(run('deadlineTemporalState("2026-09-20").localDate'), "2026-09-20");
  assert.equal(run('deadlineTemporalState("2026-09-20T01:00:00Z").localDate'), "2026-09-19");
});

test("DST repeated hour compares aware instants and naive wall values separately", () => {
  const { run } = harness("America/New_York");
  assert.equal(run('deadlineTemporalState("2026-11-01T01:30:00-04:00", new Date("2026-11-01T06:15:00Z")).overdue'), true);
  assert.equal(run('deadlineTemporalState("2026-11-01T01:30:00", new Date("2026-11-01T06:15:00Z")).overdue'), false);
});

test("Editing another field preserves the original aware deadline", () => {
  const { fields, run } = harness();
  const item = {
    title: "原题", type: "note", importance: "unknown", urgency: "unknown",
    deadline: "2026-09-18T23:30:00Z", estimated_time: null, next_action: null, extra_information: null,
  };
  for (const [name, value] of Object.entries({ title: "新题", type: "note", deadline: "2026-09-19", estimate: "", next: "", extra: "{}" })) {
    fields.set(`#edit-${name}`, { value });
  }
  assert.equal(JSON.stringify(run(`changedPatch(${JSON.stringify(item)})`)), JSON.stringify({ title: "新题" }));
  fields.get("#edit-deadline").value = "2026-09-21";
  assert.equal(run(`changedPatch(${JSON.stringify(item)})`).deadline, "2026-09-21");
});

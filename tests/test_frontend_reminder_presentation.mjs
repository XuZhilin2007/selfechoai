import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import test from "node:test";
import vm from "node:vm";

const source = readFileSync(new URL("../app/static/app.js", import.meta.url), "utf8");
function harness() {
  const context = vm.createContext({
    navigator: {},
    document: { querySelector() {}, addEventListener() {} },
    window: { addEventListener() {} },
  });
  vm.runInContext(source.replace(/^void initializeAuthentication\(\);$/m, ""), context);
  vm.runInContext(`
    authentication.user = { timezone: "Asia/Shanghai" };
    Date.now = () => Date.parse("2026-09-22T04:00:00Z");
  `, context);
  return (code) => vm.runInContext(code, context);
}

test("Reminder elapsed days use complete 24-hour periods, preserving sub-day text", () => {
  const run = harness();
  for (const status of ["due", "scheduled"]) {
    for (const [elapsed, expected] of [
      [0, "刚刚到了提醒时间"],
      [59_999, "刚刚到了提醒时间"],
      [60_000, "1 分钟前已到提醒时间"],
      [86_399_999, "1439 分钟前已到提醒时间"],
      [86_400_000, "已过期 1 天"],
      [172_799_999, "已过期 1 天"],
      [172_800_000, "已过期 2 天"],
      [25_347 * 60_000, "已过期 17 天"],
      [-60_000, status === "due" ? "1 分钟前已到提醒时间" : "1 分钟后会微提醒你"],
    ]) {
      assert.equal(run(`reminderNaturalText({status: ${JSON.stringify(status)},
        remind_at: new Date(Date.now() - ${elapsed}).toISOString()})`), expected);
    }
  }
  assert.equal(run('reminderNaturalText({status: "needs_confirmation"})'), "还需要选择一个具体时间");
  // Crossing local midnight alone is not a full elapsed day.
  assert.equal(run('reminderNaturalText({status: "due", remind_at: "2026-09-21T23:59:00+08:00"})'), "721 分钟前已到提醒时间");
});

test("Detail retains exact Reminder time, status and actions beside elapsed-day text", () => {
  const run = harness();
  for (const status of ["due", "scheduled"]) {
    const html = run(`reminderDetailSection({status: "active"}, {
      status: "${status}", remind_at: "2026-09-04T22:23:00+08:00"
    })`);
    assert.match(html, /已过期 17 天/);
    assert.match(html, /class="reminder-exact">9 月 4 日 22:23<\/p>/);
    assert.ok(html.includes(`data-reminder-status="${status}"`));
    assert.ok(html.includes(status === "due" ? "再次设置提醒" : "修改时间"));
  }
  assert.equal(run('reminderCardText({status: "due", surfaced_time: null})'), "已到提醒时间");
  assert.equal(run('reminderCardText({status: "due", surfaced_time: "2026-09-22T04:00:00Z"})'), null);
});

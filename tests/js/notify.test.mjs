// 通知判定逻辑的 node 单测（闭环 6）
//
// 为什么用 node 跑：`diffStatus` 是纯函数，但原来埋在依赖 DOM 的 app.js 里，
// node 加载不了 → 等于没测。抽到 notify.js 之后用 vm 直接加载即可。
// 由 tests/test_notify_js.py 调用；node 不在就跳过。

import assert from "node:assert";
import { readFileSync } from "node:fs";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";
import vm from "node:vm";

const here = dirname(fileURLToPath(import.meta.url));
const source = readFileSync(join(here, "..", "..", "app", "web", "notify.js"), "utf8");

function load() {
  const store = new Map();
  const context = {
    Notification: { permission: "granted" },
    localStorage: {
      getItem: (k) => (store.has(k) ? store.get(k) : null),
      setItem: (k, v) => store.set(k, String(v)),
    },
    module: { exports: {} },
  };
  context.globalThis = context;
  vm.createContext(context);
  vm.runInContext(source, context);
  return context.JobPilotNotify;
}

const api = load();
let passed = 0;
function check(name, fn) {
  fn();
  passed += 1;
  console.log(`  ✓ ${name}`);
}

check("首次熔断会通知", () => {
  const events = api.diffStatus({ pacing: {} }, { pacing: { halted_until: "2026-11-13T16:05:59" } });
  assert.equal(events.length, 1);
  assert.equal(events[0].tag, "halt");
});

check("同一个熔断时间不重复通知", () => {
  const status = { pacing: { halted_until: "2026-11-13T16:05:59" } };
  assert.equal(api.diffStatus(status, status).length, 0);
});

check("登录失效会通知", () => {
  const events = api.diffStatus({ pacing: { next_reason: "interval" } }, { pacing: { next_reason: "auth" } });
  assert.equal(events[0].tag, "auth");
});

check("目标达成只在跨过那一刻通知一次", () => {
  const first = api.diffStatus({ pacing: { used: 23, target: 25 } }, { pacing: { used: 25, target: 25 } });
  assert.equal(first.length, 1);
  assert.equal(first[0].tag, "target");
  const again = api.diffStatus({ pacing: { used: 25, target: 25 } }, { pacing: { used: 25, target: 25 } });
  assert.equal(again.length, 0);
});

check("拿到微信号会通知，且报出新增数量", () => {
  const events = api.diffStatus({ funnel: { wechat: 1 } }, { funnel: { wechat: 2 } });
  assert.equal(events[0].tag, "wechat");
  assert.ok(events[0].body.includes("1"));
});

check("只是多发了招呼不通知（否则一天 50 条弹 50 次）", () => {
  const events = api.diffStatus({ pacing: { used: 10, target: 25 } }, { pacing: { used: 11, target: 25 } });
  assert.equal(events.length, 0);
});

check("空状态不抛", () => {
  assert.deepEqual(api.diffStatus(null, null), []);
  assert.deepEqual(api.diffStatus({}, {}), []);
});

check("开关状态读写", () => {
  assert.equal(api.isNotifyOn(), false);
  api.setNotifyOn(true);
  assert.equal(api.isNotifyOn(), true);
  assert.equal(api.notifyEnabled(), true, "权限 granted + 开关打开 才算启用");
});

console.log(`\nnotify.js: ${passed} passed`);

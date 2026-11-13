/* jobpilot 通知逻辑（闭环 6）
 *
 * 为什么单独一个文件：`diffStatus` 是纯函数，最该被测；而它原来埋在 app.js 里
 * （一个几千行、依赖 DOM 的脚本），node 根本没法加载。
 * 抽出来之后：浏览器用 `window.JobPilotNotify`，测试用 `vm` 直接跑这个文件。
 *
 * 这里的函数**不碰 DOM**，只读 `globalThis` 上的 Notification / localStorage。
 */
(function (global) {
  "use strict";

  const NOTIFY_KEY = "jobpilot.notify";

  function notifyEnabled() {
    return (
      typeof global.Notification !== "undefined" &&
      global.Notification.permission === "granted" &&
      global.localStorage.getItem(NOTIFY_KEY) === "1"
    );
  }

  function isNotifyOn() {
    return global.localStorage.getItem(NOTIFY_KEY) === "1";
  }

  function setNotifyOn(on) {
    global.localStorage.setItem(NOTIFY_KEY, on ? "1" : "0");
  }

  /**
   * 比较前后两次 `/api/status`，给出需要通知的事件。
   *
   * 只报四类：
   * - 熔断（要做人做的事）
   * - 登录失效（要扫码）
   * - 今日目标达成（阶段性成果）
   * - 拿到微信号（真金白银）
   *
   * 刻意**不报**"又发了一条"：一天 50 条会弹 50 次，用户第一时间就会关掉通知，
   * 关掉之后就再也叫不醒人了。
   */
  function diffStatus(prev, next) {
    const events = [];
    if (!next || typeof next !== "object") return events;

    const before = (prev && prev.pacing) || {};
    const after = next.pacing || {};
    const prevFunnel = (prev && prev.funnel) || {};
    const nextFunnel = next.funnel || {};

    if (after.halted_until && after.halted_until !== before.halted_until) {
      events.push({
        tag: "halt",
        title: "⛔ 已熔断",
        body: "节奏引擎停了：" + (after.last_error || "命中风控"),
      });
    } else if (after.next_reason === "auth" && before.next_reason !== "auth") {
      events.push({
        tag: "auth",
        title: "🔑 登录失效",
        body: "去专用浏览器扫码，然后重新起跑",
      });
    }

    const target = Number(after.target);
    if (target > 0 && Number(after.used) >= target && Number(before.used) < target) {
      events.push({
        tag: "target",
        title: "✅ 今日目标达成",
        body: after.used + "/" + after.target + " 条，引擎会自动等到明天",
      });
    }

    const gained = Number(nextFunnel.wechat || 0) - Number(prevFunnel.wechat || 0);
    if (gained > 0) {
      events.push({
        tag: "wechat",
        title: "💬 拿到微信号",
        body: "新增 " + gained + " 个，共 " + nextFunnel.wechat + " 个；去「微信号」页查看",
      });
    }

    return events;
  }

  const api = { NOTIFY_KEY, diffStatus, notifyEnabled, isNotifyOn, setNotifyOn };
  global.JobPilotNotify = api;
  if (typeof module !== "undefined" && module.exports) module.exports = api;
})(typeof globalThis !== "undefined" ? globalThis : this);

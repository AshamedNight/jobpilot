// jobpilot 单页前端：原生 JS，无构建步骤（任务书 §0.1）
// 约定：
//   - 所有额度数字都从 /api/status 实时读，页面不写死任何额度（§4）。
//   - 页面加载与「刷新」只发 GET；不做自动轮询（§0.6）。
//   - 本批不做连接流程（用户裁定 A 方案），无连接按钮。
//   - 不展示内部任务标识（§0.7）。

const $ = (id) => document.getElementById(id);

// ---------------------------------------------------------------- 工具
function esc(value) {
  if (value === null || value === undefined) return "";
  return String(value).replace(/[&<>"']/g, (c) => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]
  ));
}

function log(text) {
  const now = new Date().toLocaleTimeString();
  const line = `[${now}] ${text}`;
  const raw = $("log");
  raw.textContent = `${line}\n${raw.textContent}`.slice(0, 4000);
  const latest = $("log-latest");
  if (latest) latest.textContent = line;
}

async function api(path, options) {
  const res = await fetch(path, options);
  const text = await res.text();
  let data;
  try { data = JSON.parse(text); } catch { data = { raw: text }; }
  if (!res.ok) throw new Error(`${res.status} ${res.statusText} :: ${text.slice(0, 300)}`);
  return data;
}

function show(el, visible) {
  if (!el) return;
  el.classList.toggle("hidden", !visible);
}

// 进度条：<90% 主色；>=90% 警告；100% 成功（§2.2）
function paintBar(id, used, max) {
  const el = $(id);
  if (!el) return;
  const u = Number(used) || 0;
  const m = Number(max) || 0;
  const pct = m > 0 ? Math.min(100, Math.round((u / m) * 100)) : 0;
  el.style.width = `${pct}%`;
  el.classList.remove("is-warn", "is-full");
  if (m > 0 && u >= m) el.classList.add("is-full");
  else if (pct >= 90) el.classList.add("is-warn");
}

function emptyState(text, hint) {
  const svg = `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.5"
      stroke-linecap="round" stroke-linejoin="round" aria-hidden="true">
      <rect x="3" y="4" width="18" height="16" rx="2"></rect>
      <path d="M3 9h18M8 4v5"></path>
    </svg>`;
  return `<div class="empty">${svg}<div>${esc(text)}${hint ? `<br>${esc(hint)}` : ""}</div></div>`;
}

/** cooldown_until 是否**还在生效**。
 *  2026-11-13 事故：16:09 的冷却早就过期，页头仍显示「冷却中」，
 *  而当日其实已经 25/25 跑满 —— 一个过期的字段被当成当前状态，最容易误导人。 */
function isCooling(pace) {
  if (!pace || !pace.cooldown_until) return false;
  return new Date(pace.cooldown_until).getTime() > Date.now();
}

function table(headers, rows, emptyText, emptyHint) {
  if (!rows.length) return emptyState(emptyText, emptyHint);
  const head = headers.map((h) => `<th>${esc(h)}</th>`).join("");
  const body = rows.join("");
  return `<table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>`;
}

// 会话状态 → 文案 + 语义 pill 类（§2.3）
function statePill(state) {
  const s = String(state || "").toUpperCase();
  const map = {
    GREETED: ["已打招呼", "tag-ok"],
    WECHAT_GOT: ["已加微信", "tag-accent"],
    PENDING_MANUAL: ["待人工", "tag-warn"],
    REJECTED: ["未通过", "tag-danger"],
    EXCHANGE_INCOMING: ["对方请求交换", "tag-accent"],
    EXCHANGE_REQUESTED: ["已申请交换", "tag-warn"],
    EXCHANGE_UNCONFIRMED: ["交换待确认", "tag-warn"],
  };
  const [text, cls] = map[s] || [state || "—", "tag-neutral"];
  return `<span class="tag ${cls}">${esc(text)}</span>`;
}

// ---------------------------------------------------------------- status
// 最后一次成功的 /api/status：离线时用它撑住界面（否则一断网整页变骨架）。
const OFFLINE_CACHE_KEY = "jobpilot.lastStatus";
let offlineMode = false;

function readStatusCache() {
  try {
    return JSON.parse(localStorage.getItem(OFFLINE_CACHE_KEY) || "null");
  } catch {
    return null;
  }
}

async function loadStatus() {
  let s;
  try {
    s = await api("/api/status");
    offlineMode = false;
    try {
      localStorage.setItem(OFFLINE_CACHE_KEY, JSON.stringify(s));
    } catch {
      /* 存不进去就算了，不影响在线 */
    }
  } catch (err) {
    // 断网 ≠ 服务坏了。区分开：断网用缓存撑界面并显式标注"离线"，
    // 真的连不上服务才把错误抛给上层（那里会把红条显示出来）。
    const cached = readStatusCache();
    if (!navigator.onLine && cached) {
      s = cached;
      offlineMode = true;
    } else {
      throw err;
    }
  }
  const q = s.quota || {};
  checkNotifications(s);

  // 页头摘要：adapter · adapter_class · 上游版本（§2.2-1）
  const acct = s.account || {};
  const summaryBits = [];
  if (offlineMode) summaryBits.push("离线 · 显示上次数据");
  if (acct.name) summaryBits.push(acct.name);
  summaryBits.push(s.adapter, s.adapter_class);
  if (s.upstream_version) summaryBits.push(`上游 ${s.upstream_version}`);
  $("s-summary").textContent = summaryBits.filter(Boolean).join(" · ") || "状态未知";

  // 旧 statusbar 数据落点（右栏小字行）
  $("s-adapter").textContent = `${s.adapter || "—"} / ${s.adapter_class || "—"}`;
  $("s-quota").textContent = `${q.daily_greet ?? "?"} 条`;
  $("s-quota").title = `daily_greet=${q.daily_greet ?? "?"} / daily_greet_max=${q.daily_greet_max ?? "?"}`;
  $("s-paused").textContent = s.paused ? "已暂停" : "未暂停";
  $("s-llm").textContent = s.llm_configured ? "已配置" : "未配置（降级：FakeLLM）";
  if ($("side-db")) $("side-db").textContent = s.db || "—";
  const caps = s.capabilities || [];

  // ---- KPI 1：今日进度（以**节奏引擎**为准，不是配置里的静态额度）----
  // 为什么改：原来显示 quota.daily_greet(50)，而引擎当天目标是爬坡档位（15/25/40/50）
  // 叠加当天 cap。同一屏两个真相，用户没法判断"今天还要不要干预"。
  const pace = s.pacing || {};
  const paceOn = Boolean(pace.running);
  const used = pace.used ?? q.used_today ?? 0;
  const target = pace.target ?? q.daily_greet ?? 0;
  $("kpi-greet-used").textContent = used;
  $("kpi-greet-max").textContent = target || "?";
  const left = Math.max((Number(target) || 0) - (Number(used) || 0), 0);
  const reason = PACE_REASON[pace.next_reason] || pace.next_reason || "";
  let paceNote;
  if (pace.halted_until) paceNote = `已熔断至 ${new Date(pace.halted_until).toLocaleString()}`;
  else if (isCooling(pace)) paceNote = `冷却中 · ${new Date(pace.cooldown_until).toLocaleTimeString()} 后重试`;
  else if (paceOn) paceNote = `还差 ${left} 条${reason ? ` · ${reason}` : ""}`;
  else paceNote = "节奏未启动 · 点下方「开始按节奏发」";
  $("kpi-greet-note").textContent = paceNote;
  paintBar("kpi-greet-bar", used, target);

  // ---- KPI 2：转化（闭环 2 的成果指标，取代原来没人用的"岗位详情"）----
  const funnel = s.funnel || {};
  $("kpi-replied").textContent = funnel.replied ?? 0;
  $("kpi-wechat").textContent = `微信 ${funnel.wechat ?? 0}`;
  $("kpi-rejected").textContent = `拒绝 ${funnel.rejected ?? 0}`;
  // 会话/消息只在「跟进一轮」时才入库，所以这里 0 不代表平台真没回复
  $("kpi-funnel-note").textContent = funnel.wechat
    ? "已拿到联系方式，去「会话」页查看"
    : (funnel.replied
      ? "有回复但还没到加微信"
      : "跑一次「跟进一轮」才会统计回复");

  // ---- KPI 3：运行状态 + 能力徽标 ----
  let runText = "未启动";
  if (pace.halted_until) runText = "已熔断";
  else if (isCooling(pace)) runText = "冷却中";
  else if (pace.running) runText = pace.inflight ? "发送中" : "运行中";
  else if (s.paused) runText = "已暂停";
  $("kpi-run-state").textContent = runText;
  $("s-caps").innerHTML = caps.length
    ? caps.map((c) => `<span class="pill">${esc(c)}</span>`).join("")
    : `<span class="pill">—</span>`;
  $("kpi-run-note").textContent = pace.next_at
    ? `下一步 ${new Date(pace.next_at).toLocaleTimeString()}`
    : "";
  $("btn-pause").textContent = s.paused ? "恢复" : "暂停";

  if ($("side-pace-target")) {
    $("side-pace-target").textContent = paceOn ? `${used}/${target}` : "未启动";
    $("side-pace-target").title = "节奏引擎的当日目标（爬坡档位 + 当天 cap）";
  }

  // 风控锁：红色横幅，不可关闭（§0.5 / §2.3）
  const locked = Boolean(s.risk_locked || s.upstream_risk_lock);
  show($("banner-risk"), locked);
  if (locked) $("right-risk").textContent = "已锁定 · 需要人工处理";
  else $("right-risk").textContent = "正常";

  // 告警：黄色横幅（沿用 #warn）
  const lines = [];
  (s.warnings || []).forEach((w) => { if (!lines.includes(w)) lines.push(w); });
  const warnBox = $("warn");
  if (lines.length) {
    warnBox.innerHTML = lines.map((l) => `<div class="banner-line">· ${esc(l)}</div>`).join("");
    show(warnBox, true);
  } else {
    warnBox.innerHTML = "";
    show(warnBox, false);
  }

  // 右栏：LLM / 上游版本 / 待接模块 / 最近上游错误
  const llm = s.llm || {};
  const llmBits = [];
  if (llm.mode) llmBits.push(`mode=${llm.mode}`);
  if (llm.model) llmBits.push(`model=${llm.model}`);
  $("right-llm").textContent = llmBits.join(" · ");

  $("right-upstream").textContent = s.upstream_version || "—";

  const mods = s.pending_modules || [];
  $("right-pending-mods").textContent = mods.length ? `待接模块：${mods.join("、")}` : "";

  const errBox = $("right-last-error");
  const lastErr = s.last_upstream_error;
  errBox.innerHTML = "";
  if (lastErr && Object.keys(lastErr).length) {
    const human = lastErr.message || lastErr.error || lastErr.code || "上游返回错误";
    const detail = esc(JSON.stringify(lastErr, null, 2));
    errBox.innerHTML = `<details><summary>${esc(human)}</summary>
      <pre class="log-raw">${detail}</pre>
      <div>含上游错误码，不含账号凭据</div></details>`;
  }

  return s;
}

// ---------------------------------------------------------------- jobs
async function loadJobs() {
  const data = await api("/api/jobs");
  const rows = (data.items || []).map((j) => {
    const scored = j.match_score !== null && j.match_score !== undefined;
    const num = scored ? Number(j.match_score) : null;
    const scoreText = scored ? num.toFixed(0) : "—";
    let cls = "";
    if (scored && num <= -999) cls = "score-reject";
    else if (scored && num >= 60) cls = "score-pass";
    // 未打分的行不重复那句长文案：整张表 13 行都写"还没跑过跑一轮"纯粹是噪音。
    // 有理由才显示，没有就给一个安静的小标签（详情挂在 title 上，鼠标悬停能看全）。
    const hasReason = Boolean(j.match_reason);
    const reason = hasReason
      ? esc(j.match_reason)
      : `<span class="tag tag-neutral" title="这条是节奏引擎直接搜到并打招呼的，没经过评分">未评分</span>`;
    const gap = (j.gap_flags || []).length
      ? `<div class="side-note">${esc((j.gap_flags || []).join("、"))}</div>`
      : "";
    return `<tr>
      <td>${esc(j.title)}${gap}</td>
      <td>${esc(j.company)}</td>
      <td class="num">${esc(j.salary)}</td>
      <td class="city-cell">${esc(j.city)}</td>
      <td class="num ${cls}">${esc(scoreText)}</td>
      <td class="reason-cell">${reason}</td>
    </tr>`;
  });
  $("jobs").innerHTML = table(
    ["标题", "公司", "薪资", "城市", "分数", "淘汰原因 / 理由"],
    rows,
    "还没有岗位。点上面的「刷新」看看。"
  );
  return data;
}

// -------------------------------------------------------- conversations
async function loadConversations() {
  const data = await api("/api/conversations");
  const rows = (data.items || []).map((c) => `<tr>
      <td>${esc(c.hr_name)}</td>
      <td>${esc(c.company)}</td>
      <td>${statePill(c.state)}</td>
      <td>${esc(c.last_msg_from)}</td>
      <td class="num">${esc(c.last_msg_at || "—")}</td>
    </tr>`);
  $("conversations").innerHTML = table(
    ["HR", "公司", "状态", "最后一条来自", "时间"],
    rows,
    "还没有会话。打过招呼后才会出现在这里。"
  );
  return data;
}

// ---------------------------------------------------------------- 微信号清单（闭环 2.4）
// 隐私口径：号码**不进列表接口**（架构 §1.4），所以这里只列会话，
// 点「显示号码」时才按 uid 单独取一次详情。一次性把所有人的号码吐到前端属于设计事故。
const WECHAT_STATES = ["WECHAT_GOT", "EXCHANGE_INCOMING", "EXCHANGE_REQUESTED"];

async function loadWechat() {
  const data = await api("/api/conversations");
  const items = (data.items || []).filter((c) => WECHAT_STATES.includes(String(c.state || "").toUpperCase()));
  const got = items.filter((c) => String(c.state).toUpperCase() === "WECHAT_GOT").length;
  const badge = $("tab-wechat-count");
  if (badge) badge.textContent = got ? String(got) : "";

  const rows = items.map((c) => `<tr>
      <td>${esc(c.hr_name)}</td>
      <td>${esc(c.company)}</td>
      <td>${statePill(c.state)}</td>
      <td class="wx-cell"><button class="btn btn-ghost wx-show" type="button" data-uid="${esc(c.uid)}">显示号码</button></td>
    </tr>`);
  $("wechat").innerHTML = table(
    ["HR", "公司", "状态", "微信号"],
    rows,
    "还没有拿到微信号",
    "等 HR 发起交换、或我们主动申请后，跑一次「跟进一轮」就会出现在这里"
  );
  return data;
}

function bindWechatActions() {
  $("wechat").addEventListener("click", async (event) => {
    const copyBtn = event.target.closest(".wx-copy");
    if (copyBtn) {
      try {
        await navigator.clipboard.writeText(copyBtn.dataset.wx || "");
        copyBtn.textContent = "已复制";
        setTimeout(() => { copyBtn.textContent = "复制"; }, 1500);
      } catch {
        log("复制失败：浏览器不允许剪贴板访问");
      }
      return;
    }
    const showBtn = event.target.closest(".wx-show");
    if (!showBtn) return;
    const cell = showBtn.closest("td");
    showBtn.disabled = true;
    showBtn.textContent = "读取中…";
    try {
      const detail = await api(`/api/conversations/${encodeURIComponent(showBtn.dataset.uid)}`);
      const wx = (detail.conversation || {}).wechat_id || "";
      if (!wx) {
        cell.innerHTML = `<span class="side-note">服务端还没有记录号码（再跑一次跟进）</span>`;
        return;
      }
      cell.innerHTML = `<span class="wx-number">${esc(wx)}</span>` +
        `<button class="btn btn-ghost wx-copy" type="button" data-wx="${esc(wx)}">复制</button>`;
    } catch (err) {
      cell.innerHTML = `<span class="side-note">读取失败：${esc(err.message)}</span>`;
    }
  });
}

// ---------------------------------------------------------------- pending
async function loadPending() {
  const data = await api("/api/pending");
  const rows = (data.items || []).map((p) => `<tr>
      <td>${esc(p.hr_name)}</td>
      <td>${esc(p.company)}</td>
      <td><span class="tag tag-warn">${esc(p.state)}</span> <span class="pill pill-manual">人工核对</span></td>
      <td>${esc(p.note)}</td>
    </tr>`);
  const html = table(
    ["HR", "公司", "状态", "备注"],
    rows,
    "没有需要人工处理的项目。"
  );
  const note = data.note ? `<div class="side-note">${esc(data.note)}</div>` : "";
  $("pending").innerHTML = html + note;
  return data;
}

// ---------------------------------------------------------------- 节奏引擎
// 这是页面里**唯一**会自己定时刷新的块，理由是它显示的是"下一次什么时候发"，
// 不刷新就变成一句废话。轮询边界：只在 running 时每 15s 拉一次 /api/pacing/status，
// 停止后立刻停轮询；倒计时在本地每秒重算，不再请求服务端。
let paceState = null;
let paceTickTimer = null;
let pacePollTimer = null;

const PACE_REASON = {
  interval: "按间隔等下一发",
  round_gap: "本轮发完，休息一会儿",
  window: "不在时段内，等下一个窗口",
  done: "今日目标已完成",
  cooldown: "冷却中",
  halt: "已熔断",
  paused: "全局已暂停",
  blocked: "连接任务进行中，暂时让位",
  risk_locked: "风控锁定",
  auth: "登录态失效，需扫码",
  skipped: "暂时没有新的可发岗位",
  stopped: "已停止",
  idle: "—",
  start: "即将开始",
};

function fmtGap(ms) {
  const total = Math.max(Math.round(ms / 1000), 0);
  if (total < 60) return `${total} 秒`;
  const m = Math.floor(total / 60);
  if (m < 60) return `${m} 分 ${String(total % 60).padStart(2, "0")} 秒`;
  const h = Math.floor(m / 60);
  return `${h} 小时 ${m % 60} 分`;
}

function setPaceBadge(text, cls) {
  const el = $("pace-badge");
  if (!el) return;
  el.textContent = text;
  el.className = `tag ${cls}`;
}

function paintPacing(p) {
  paceState = p;
  const now = Date.now();
  const used = Number(p.used) || 0;
  const target = Number(p.target) || 0;

  $("pace-used").textContent = used;
  $("pace-target").textContent = target || "—";
  paintBar("pace-bar", used, target);

  const halted = p.halted_until && new Date(p.halted_until).getTime() > now;
  const cooling = p.cooldown_until && new Date(p.cooldown_until).getTime() > now;
  // 【闭环 1 · 2026-11-04】`last_verdict` 只在一轮**结束**后才有值，轮内必然是 null。
  // 之前"正在发（可能卡住）"和"在等间隔"在页面上长得一模一样，真机排障被这一点拖住。
  const inflight = Boolean(p.inflight);
  const passAge = inflight && p.pass_started_at
    ? Math.max(now - new Date(p.pass_started_at).getTime(), 0)
    : 0;

  // 徽标：配置错 > 熔断 > 冷却 > 发送中 > 运行中 > 未启动
  if (p.config_error) setPaceBadge("配置错误", "tag-danger");
  else if (halted) setPaceBadge("已熔断", "tag-danger");
  else if (cooling) setPaceBadge("冷却中", "tag-warn");
  else if (inflight) setPaceBadge(passAge > 120000 ? "发送中（偏久）" : "发送中", passAge > 120000 ? "tag-warn" : "tag-ok");
  else if (p.running) setPaceBadge("运行中", "tag-ok");
  else setPaceBadge("未启动", "tag-neutral");

  // 当前时段：三段里高亮正在跑的那一段
  const segs = p.segments || [];
  if (!segs.length) {
    $("pace-segment").textContent = p.enabled ? "—" : "未启用";
  } else {
    $("pace-segment").textContent = p.segment
      ? `${p.segment}（第 ${(p.segment_index ?? 0) + 1}/${segs.length} 段）`
      : `时段外（${segs.join(" / ")}）`;
  }

  // 下一次
  if (!p.running) {
    $("pace-next").textContent = "—";
  } else if (halted) {
    $("pace-next").textContent = `${fmtGap(new Date(p.halted_until).getTime() - now)} 后解冻`;
  } else if (cooling) {
    $("pace-next").textContent = `${fmtGap(new Date(p.cooldown_until).getTime() - now)} 后恢复`;
  } else if (inflight) {
    $("pace-next").textContent = `正在发送（已 ${fmtGap(passAge)}）`;
  } else if (p.next_at) {
    const gap = new Date(p.next_at).getTime() - now;
    $("pace-next").textContent = gap > 0 ? `${fmtGap(gap)} 后` : "即将发送";
  } else {
    $("pace-next").textContent = "—";
  }

  // 本调节奏（数字全部来自后端配置，页面不写死）
  const rhythmBits = [];
  if ((p.round || []).length) rhythmBits.push(`一轮 ${p.round[0]}–${p.round[1]} 条`);
  if ((p.interval_s || []).length) rhythmBits.push(`间隔 ${p.interval_s[0]}–${p.interval_s[1]} 秒`);
  $("pace-rhythm").textContent = rhythmBits.join(" · ") || "—";

  // 说明行
  const noteBits = [`今日还差 ${Math.max(target - used, 0)} 条`];
  if (p.pool_size) noteBits.push(`文案池 ${p.pool_size} 条`);
  if (p.last_sent_at) noteBits.push(`上一条 ${new Date(p.last_sent_at).toLocaleTimeString()}`);
  // 裁决是持久化的：不带时间戳的话，"上次运行的 error" 会被当成"这一次刚失败"。
  if (p.last_verdict_at) noteBits.push(`上次结论 ${new Date(p.last_verdict_at).toLocaleTimeString()}`);
  if (p.next_reason && p.next_reason !== "idle") noteBits.push(PACE_REASON[p.next_reason] || p.next_reason);
  $("pace-note").textContent = noteBits.join(" · ");

  // 提示条
  const warn = $("pace-warn");
  let warnText = "";
  let warnCls = "";
  if (p.config_error) { warnText = p.config_error; warnCls = "is-danger"; }
  else if (halted) { warnText = `已熔断：${p.last_error || "命中平台风控"}。解冻时间 ${new Date(p.halted_until).toLocaleString()}，重启服务也不会提前解除。`; warnCls = "is-danger"; }
  else if (cooling) { warnText = `冷却中：${p.last_error || "上游限流或结果不确定"}。${new Date(p.cooldown_until).toLocaleTimeString()} 之后自动恢复。`; warnCls = ""; }
  else if (inflight && passAge > 120000) { warnText = `这一轮已经发了 ${fmtGap(passAge)}，可能卡在平台请求上。到点会被上游超时切断（不会一直挂着），到点后看 last_verdict 判断原因。`; warnCls = "is-danger"; }
  else if (p.next_reason === "done") { warnText = "今日目标已完成。明天第一个时段自动继续。"; warnCls = "is-ok"; }
  else if (p.next_reason === "auth") { warnText = `登录态已失效：${p.last_error || "请在浏览器里扫码重新登录"} 扫码后点「开始」继续（不会自动重试）。`; warnCls = "is-danger"; }
  else if (p.running && p.next_reason === "window") { warnText = "当前不在发送时段内，到点会自动开始。"; warnCls = ""; }

  if (warnText) {
    warn.textContent = warnText;
    warn.className = `pace-warn ${warnCls}`;
  } else {
    warn.textContent = "";
    warn.className = "pace-warn hidden";
  }

  // 按钮
  const startBtn = $("btn-pace-start");
  const stopBtn = $("btn-pace-stop");
  if (startBtn) startBtn.disabled = Boolean(p.running) || !p.enabled;
  if (stopBtn) stopBtn.disabled = !p.running;
  $("pace-stop-note").textContent = p.running ? "随时可停，不打断已发出的消息" : "";

  if (p.running) startPaceTick();
  else stopPaceTick();
}

function startPaceTick() {
  if (paceTickTimer) return;
  paceTickTimer = setInterval(() => {
    if (paceState && paceState.running) paintPacing(paceState);
  }, 1000);
  if (pacePollTimer) return;
  pacePollTimer = setInterval(async () => {
    if (!paceState || !paceState.running) return;
    if (document.hidden) return;
    try {
      paintPacing(await api("/api/pacing/status"));
    } catch (err) {
      log(`节奏状态刷新失败：${err.message}`);
    }
  }, 15000);
}

function stopPaceTick() {
  if (paceTickTimer) { clearInterval(paceTickTimer); paceTickTimer = null; }
  if (pacePollTimer) { clearInterval(pacePollTimer); pacePollTimer = null; }
}

async function loadPacing() {
  try {
    paintPacing(await api("/api/pacing/status"));
  } catch (err) {
    log(`节奏状态读取失败：${err.message}`);
  }
}

async function startPacing() {
  await withBusy($("btn-pace-start"), "启动中…", async () => {
    const data = await api("/api/pacing/start", { method: "POST" });
    if (data.ok) {
      log(`节奏引擎已启动：今日目标 ${data.status?.target ?? "?"} 条，间隔 ${(data.status?.interval_s || []).join("–")} 秒`);
    } else {
      log(`启动失败：${data.error || "未知错误"}`);
    }
    await loadPacing();
    await loadStatus();
  });
}

async function stopPacing() {
  await withBusy($("btn-pace-stop"), "停止中…", async () => {
    const data = await api("/api/pacing/stop", { method: "POST" });
    log(data.ok ? "节奏引擎已停止" : `停止失败：${data.error || "未知错误"}`);
    await loadPacing();
  });
}

// ---------------------------------------------------------------- actions
// 点一次就禁用并改文案，拿到响应后恢复（§4）；不改任何后端语义。
async function withBusy(btn, busyText, fn) {
  const original = btn.textContent;
  btn.disabled = true;
  btn.textContent = busyText;
  try {
    await fn();
  } catch (err) {
    log(err.message);
  } finally {
    btn.disabled = false;
    btn.textContent = original;
  }
}

async function runSweep() {
  await withBusy($("btn-sweep"), "运行中…", async () => {
    const data = await api("/api/run/sweep", { method: "POST" });
    if (data.ok) {
      const st = data.stats || {};
      log(`跑一轮完成：搜到 ${st.found ?? 0} 个岗位，新入库 ${st.inserted ?? 0} 个，打招呼 ${st.greeted ?? 0} 个。${st.note || ""}`);
    } else {
      log(`跑一轮失败：${data.error || "未知错误"}`);
    }
    await refreshAll();
  });
}

async function runFollowup() {
  await withBusy($("btn-followup"), "跟进中…", async () => {
    const data = await api("/api/run/followup", { method: "POST" });
    if (data.ok) {
      const st = data.stats || {};
      log(`跟进一轮完成：会话 ${st.conversations ?? 0} 个，入库消息 ${st.stored_messages ?? 0} 条，回复 ${st.replied ?? 0} 条。${st.note || ""}`);
    } else {
      log(`跟进一轮失败：${data.error || "未知错误"}`);
    }
    await refreshAll();
  });
}

async function togglePause() {
  const s = await api("/api/status");
  const next = !s.paused;
  await api(`/api/pause?paused=${next}`, { method: "POST" });
  log(next ? "已暂停" : "已恢复");
  await refreshAll();
}

// ---------------------------------------------------------------- 连接 BOSS（闭环 0）
// 设计取舍（写在这里，避免下次又去"加二维码图片"）：
//   本项目用 `browser_source=existing-browser`，登录**发生在你自己那个带调试端口的
//   Chrome 窗口里** —— 二维码在那个窗口上，不在本页。所以这里只做三件事：
//   发起连接、显示状态、把"要人去哪儿操作"讲清楚。伪装一个假二维码比没有更糟。
const CONNECT_STATE_TEXT = {
  disconnected: "未连接",
  connecting: "连接中",
  connected: "已连接",
  failed: "失败",
  needs_user_action: "需人工处理",
  timeout: "超时（旧任务仍在跑）",
};

let connectTimer = null;

function renderConnect(snap) {
  const state = snap.state || "disconnected";
  const badge = $("conn-state");
  badge.textContent = CONNECT_STATE_TEXT[state] || state;
  badge.className = `pill conn-${state}`;

  const bits = [];
  if (snap.message) bits.push(snap.message);
  if (snap.updated_at) bits.push(`更新于 ${new Date(snap.updated_at).toLocaleTimeString()}`);
  if (snap.binding && snap.binding !== "not_applicable") bits.push(`绑定：${snap.binding}`);
  $("conn-note").textContent = bits.join(" · ") || "—";

  const btn = $("btn-connect");
  const busy = state === "connecting" || snap.task_active;
  btn.disabled = busy;
  btn.textContent = busy ? "连接中…" : (state === "connected" ? "重新连接" : "连接 BOSS");
}

async function loadConnect() {
  try {
    const snap = await api("/api/connect/status");
    renderConnect(snap);
    // 连接中才轮询（用户裁定：平时只发 GET、不自动轮询）
    if ((snap.state === "connecting" || snap.task_active) && !connectTimer) {
      connectTimer = setInterval(loadConnect, 3000);
    } else if (!(snap.state === "connecting" || snap.task_active) && connectTimer) {
      clearInterval(connectTimer);
      connectTimer = null;
    }
    return snap;
  } catch (err) {
    // 断网时说"离线"，别吓人：服务多半是好的，只是这台设备上不去。
    if (!navigator.onLine) {
      $("conn-state").textContent = "离线";
      $("conn-note").textContent = "本机已断网：恢复网络后会自动重新连接";
    } else {
      $("conn-state").textContent = "不可用";
      $("conn-note").textContent = `连接接口不可用：${err.message}`;
    }
    return null;
  }
}

async function startConnect() {
  const btn = $("btn-connect");
  btn.disabled = true;
  try {
    const res = await fetch("/api/connect/start", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ confirm_bind: false }),
    });
    const text = await res.text();
    if (!res.ok) {
      // 409 = 已有连接任务；503 = 路径/风控锁/恢复待办被拒。detail 是给人看的原因。
      let detail = text;
      try { detail = JSON.parse(text).detail || detail; } catch { /* 保留原文 */ }
      $("conn-note").textContent = `连接未开始：${detail}`;
      log(`连接被拒绝：${detail}`);
      return;
    }
    const snap = JSON.parse(text);
    renderConnect(snap);
    log("连接任务已启动：请在弹出的 Chrome 窗口里完成扫码/平台验证。");
    if (!connectTimer) connectTimer = setInterval(loadConnect, 3000);
  } catch (err) {
    $("conn-note").textContent = `连接请求失败：${err.message}`;
  } finally {
    btn.disabled = false;
  }
}

async function refreshAll() {
  try {
    await loadConnect();
    await loadStatus();
    await loadPacing();
    await loadJobs();
    await loadConversations();
    await loadWechat();
    await loadSetup();
    await loadPending();
  } catch (err) {
    log(`刷新出错：${err.message}`);
  } finally {
    // 用 #wechat 之类深链打开时，浏览器会在内容渲染后再次按锚点滚动，
    // 导致页面顶上一大片空白。渲染完成后再回一次顶部，才是稳定位置。
    if (location.hash) window.scrollTo(0, 0);
  }
}

// ---------------------------------------------------------------- 标签页
function initTabs() {
  const tabs = document.querySelectorAll(".tab[data-panel]");
  const activate = (tab) => {
    tabs.forEach((t) => {
      const active = t === tab;
      t.classList.toggle("is-active", active);
      const panel = document.getElementById(t.getAttribute("data-panel"));
      if (panel) panel.classList.toggle("hidden", !active);
    });
    // 标签写进 URL hash：可直接分享/收藏某个页签，也方便截图与自动化
    const name = (tab.getAttribute("data-panel") || "").replace("panel-", "");
    if (name) history.replaceState(null, "", `#${name}`);
  };
  tabs.forEach((tab) => tab.addEventListener("click", () => activate(tab)));

  const wanted = (location.hash || "").replace("#", "");
  if (wanted) {
    const target = Array.from(tabs).find(
      (t) => (t.getAttribute("data-panel") || "") === `panel-${wanted}`
    );
    if (target) {
      activate(target);
      // 浏览器在加载时会先按 hash 原生滚动到那个元素，导致页面顶上留一大块空白。
      // 页签切换不该改变滚动位置 → 切完回到顶部。
      window.scrollTo(0, 0);
    }
  }
}

// ---------------------------------------------------------------- 绑定
// ---------------------------------------------------------------- 首次启动向导（2026-11-13）
// 只有"没配置好"时才显示。老用户永远看不到这块 —— 它服务的是"从 GitHub 下载下来的人"。
let setupState = null;

async function postJson(url, body) {
  const resp = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body || {}),
  });
  return resp.json();
}

function setupLog(text, good) {
  const box = $("setup-log");
  if (!box) return;
  box.innerHTML = good === undefined ? esc(text) : `<span class="${good ? "ok" : "bad"}">${esc(text)}</span>`;
}

function setSetupStep(n) {
  const el = $("setup-step");
  if (el) el.textContent = `第 ${n} 步 / 共 3 步`;
}

async function loadSetup() {
  const card = $("setup-card");
  if (!card) return;
  try {
    setupState = await api("/api/setup/status");
  } catch {
    return;
  }
  if (!setupState.needed) {
    card.hidden = true;
    return;
  }
  card.hidden = false;
  const cur = setupState.current || {};
  const looksPlaceholder = (v) => !v || v.indexOf("<you>") !== -1;

  $("setup-chrome").value = looksPlaceholder(cur.chrome_exe) ? (setupState.detected_chrome || [])[0] || "" : cur.chrome_exe;
  $("setup-profile").value = looksPlaceholder(cur.chrome_profile) ? "" : cur.chrome_profile;
  $("setup-data").value = looksPlaceholder(cur.data_dir) ? "" : cur.data_dir;
  setSetupStep(1);
  setupLog(`还缺：${(setupState.missing || []).join("、")}`, false);
}

function bindSetupActions() {
  const detect = $("setup-detect");
  if (detect) {
    detect.addEventListener("click", () => {
      const found = (setupState && setupState.detected_chrome) || [];
      if (!found.length) {
        setupLog("没找到 Chrome。请手动填 chrome.exe 的完整路径", false);
        return;
      }
      $("setup-chrome").value = found[0];
      setupLog(`已填入：${found[0]}`, true);
    });
  }

  const suggest = $("setup-suggest");
  if (suggest) {
    suggest.addEventListener("click", () => {
      if (!setupState) return;
      $("setup-profile").value = setupState.suggest_profile || "";
      $("setup-data").value = setupState.suggest_data_dir || "";
      setupLog("已填入推荐目录（它们会在下一步被创建）", true);
    });
  }

  const save = $("setup-save");
  if (save) {
    save.addEventListener("click", async () => {
      save.disabled = true;
      setupLog("正在写入配置…");
      try {
        const res = await postJson("/api/setup/apply", {
          chrome_exe: $("setup-chrome").value.trim(),
          profile_dir: $("setup-profile").value.trim(),
          data_dir: $("setup-data").value.trim(),
          cdp_port: 9333,
        });
        if (!res.ok) {
          setupLog(res.error || "写入失败", false);
          return;
        }
        setupLog("配置已写入 config.yaml ✅ 下一步：启动专用浏览器", true);
        $("setup-launch").disabled = false;
        setSetupStep(2);
      } finally {
        save.disabled = false;
      }
    });
  }

  const launch = $("setup-launch");
  if (launch) {
    launch.addEventListener("click", async () => {
      launch.disabled = true;
      setupLog("正在启动浏览器…");
      try {
        const res = await postJson("/api/setup/launch-browser", {});
        setupLog(res.ok ? res.hint : (res.error || "启动失败"), res.ok);
        if (res.ok) {
          $("setup-verify").disabled = false;
          setSetupStep(3);
        }
      } finally {
        launch.disabled = false;
      }
    });
  }

  const verify = $("setup-verify");
  if (verify) {
    verify.addEventListener("click", async () => {
      setupLog("正在检查调试端口…");
      try {
        const res = await api("/api/setup/verify-browser");
        setupLog(res.hint, res.ok);
        if (res.ok) {
          setupLog("全部就绪。现在可以回到顶部点「起跑」，或等下一个时段自动开始。", true);
        }
      } catch (err) {
        setupLog(`检查失败：${err.message}`, false);
      }
    });
  }
}

// ---------------------------------------------------------------- 通知（闭环 6）
// 纯逻辑在 /static/notify.js（`window.JobPilotNotify`）—— 抽出去是为了能被 node 单测，
// 这里只留"和 DOM/权限打交道"的部分。
const Notify = () => window.JobPilotNotify;

// 上一次的 /api/status 快照：diffStatus 靠它判断「变化了没有」。
// 2026-11-12 事故：这个声明在抽 notify.js 时被顺手删掉，checkNotifications 每次刷新
// 都抛 ReferenceError（只有浏览器控制台看得见，页面本身照常渲染）。
let statusSnapshot = null;

function notifyEnabled() {
  return Boolean(Notify()) && Notify().notifyEnabled();
}

async function pushNotification(title, body, tag) {
  if (!notifyEnabled()) return;
  const options = { body, tag, icon: "/static/icon.svg", badge: "/static/icon.svg" };
  try {
    const reg = navigator.serviceWorker ? await navigator.serviceWorker.getRegistration() : null;
    if (reg && reg.showNotification) {
      await reg.showNotification(title, options);
      return;
    }
  } catch {
    /* 退回页面级通知 */
  }
  try {
    new Notification(title, options);
  } catch {
    /* 浏览器拒绝就算了，不能因此打断刷新 */
  }
}

function initNotify() {
  const btn = $("btn-notify");
  const testBtn = $("btn-notify-test");
  if (!btn || !Notify()) return;
  if (typeof Notification === "undefined") {
    btn.disabled = true;
    btn.textContent = "浏览器不支持通知";
    return;
  }
  const render = () => {
    const granted = Notification.permission === "granted";
    const on = granted && Notify().isNotifyOn();
    btn.textContent = on ? "通知已开" : granted ? "开启通知" : "允许通知";
    btn.classList.toggle("is-on", on);
    if (testBtn) testBtn.classList.toggle("hidden", !on);
  };
  btn.addEventListener("click", async () => {
    if (Notification.permission !== "granted") {
      const permission = await Notification.requestPermission();
      if (permission !== "granted") {
        log("通知权限被拒绝：可在地址栏左侧的站点设置里重新允许");
        render();
        return;
      }
    }
    const on = Notify().isNotifyOn();
    Notify().setNotifyOn(!on);
    if (!on) {
      await pushNotification("jobpilot 通知已开启", "熔断、目标达成、拿到微信号时会提醒你", "notify-on");
    }
    render();
  });
  if (testBtn) {
    // 测试入口：不用等真实事件（熔断/拿号），5 秒验证整条链路通不通
    testBtn.addEventListener("click", () => {
      pushNotification("🔔 这是一条测试通知", "看到它就说明通知链路是通的", "notify-test");
    });
  }
  render();
}

async function checkNotifications(status) {
  // 【离线别报"新增"】断网时 status 来自缓存，而 statusSnapshot 可能是 null（首次加载），
  // diff 一算就得出"微信号新增 1 个"这种假事件（2026-11-12 实测）。
  // 断网期间只更新快照、不产生通知 —— 通知应该代表"刚刚发生的事"。
  if (offlineMode) {
    statusSnapshot = status;
    return;
  }
  const events = Notify() ? Notify().diffStatus(statusSnapshot, status) : [];
  statusSnapshot = status;
  for (const event of events) {
    await pushNotification(event.title, event.body, event.tag);
  }
}

initTabs();
bindWechatActions();
initNotify();
bindSetupActions();
$("btn-sweep").addEventListener("click", runSweep);
$("btn-followup").addEventListener("click", runFollowup);
$("btn-pause").addEventListener("click", togglePause);
$("btn-refresh").addEventListener("click", refreshAll);
$("btn-connect").addEventListener("click", startConnect);
$("btn-pace-start").addEventListener("click", startPacing);
$("btn-pace-stop").addEventListener("click", stopPacing);

refreshAll();

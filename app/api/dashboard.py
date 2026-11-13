"""/api/status /api/jobs /api/run/* /api/pause /api/pending（任务书 §6.1）。

C4：接口少，先都放这个文件，超过 200 行再拆成 actions.py。
API 层只做参数校验 + 调 core/adapters，**不写业务逻辑**（任务书 §3）。

e2e 批：这里承担 Phase 1 的**薄编排**——把 `matcher` / `state_machine` / `extractor` /
`composer` / `idempotency` 串起来调用。规则本身一条都没有复制进来，全在 `app/core/`。
"""

from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, Request

from app import models
from app.api import get_adapter, get_config, get_db, is_paused
from app.core import enrich
from app.core import exchange as exchange_mod
from app.core import idempotency as idem
from app.core import matcher
from app.core import state_machine as sm
from app.core.composer import compose_opener
from app.core.extractor import extract_wechat

router = APIRouter(tags=["dashboard"])

PHASE = "Phase 1 · e2e 批"

# 【F5】`phase` / `note` 不许写死"fake 全链路"：接了真机还写 fake，是
#   "看起来在跑其实是假的"最刺眼的变体，而且就在页面最显眼的位置。
#: `/api/status` 里暴露的节奏字段（白名单）。
#: 为什么不做整包透传：`pacer.status()` 含 `state_path`（绝对路径），会把本机目录结构
#: 写进响应；`test_llm_gateway` 的"响应不许出现敏感串"用例当场就抓到了。
_PACE_FIELDS = (
    "running",
    "used",
    "target",
    "remaining",
    "next_at",
    "next_reason",
    "inflight",
    "last_verdict",
    "last_verdict_at",
    "last_error",
    "halted_until",
    "cooldown_until",
    "risk_hits_today",
    "cap_today",
    "pool_size",
    "next_search_url",
    "next_search_page",
)


def _pace_view(status: dict[str, Any]) -> dict[str, Any]:
    """把 `pacer.status()` 收敛成页面需要的白名单视图（不含任何本机路径）。"""
    return {key: status.get(key) for key in _PACE_FIELDS}


def _mode_label(request: Request) -> str:
    """当前实际使用的适配器名（`main.create_app` 必然写入，见 F7 记录）。"""
    return f"adapter={getattr(request.app.state, 'adapter_name', None) or 'fake'}"


def _upstream_version_of(adapter: Any) -> str | None:
    """【F6】`upstream_version` 可能是 `@staticmethod` 也可能是普通属性。

    直接 `(...)()` 调，将来谁改成字符串属性就会 `TypeError: 'str' object is not
    callable`，而它在 `/api/status` 上——页面轮询就是一路 500。
    """
    value = getattr(adapter, "upstream_version", None)
    value = value() if callable(value) else value
    return str(value) if value else None


def _upstream_error_of(adapter: Any) -> dict[str, Any] | None:
    """【F2】取适配器最近一次上游失败，**只留可诊断的三字段**。

    `found=0` 至少有四种不同原因（没登录 / 参数非法 / 被风控 / 真的没岗位），
    不记下来用户只能看到一个 0。原始信封**绝不整个塞进响应**——里面有
    `security_id` / `raw_json`。

    `FakeAdapter` 没有 `last_error` 属性 → `None`（它不碰上游，没有"上游失败"）。
    """
    error = getattr(adapter, "last_error", None)
    if not error:
        return None
    if not isinstance(error, dict):
        return None
    return {
        "command": error.get("command"),
        "code": error.get("code"),
        "message": error.get("message"),
        "normalized": error.get("normalized") or error.get("error_code"),
    }

# e2e 批已把四个 core 模块全部接入 sweep / followup，这里不再有待接入项。
# 前端 `app.js` 在列表为空时自然隐藏提示条（任务书 §6）。
PENDING_MODULES: tuple[str, ...] = ()

# 交换请求关键词（§5.2 第 3 步：至少这三个）
EXCHANGE_KEYWORDS: tuple[str, ...] = ("加个微信", "加微信", "微信聊")

#: 【闭环 2.1】HR 明确拒绝的**强特征短语**。
#:
#: 刻意不用裸的「抱歉」「不合适」这类宽词：真人也会说"抱歉，我明天回复您"。
#: 只收"拒绝语义明确、几乎不会出现在其他语境"的短语 —— 宁可漏判（会话继续保持，
#: 下一轮还有机会）也不误杀（误杀 = 把一个还在聊的 HR 拉进黑名单，不可逆）。
#:
#: 真机样本（会话 753824113）：
#:   "抱歉，尽管您的简历很优秀，但是我们的职位需要其他资质的候选人。
#:    祝您在BOSS直聘找到您的理想工作！"
#:   → 命中「需要其他资质」「祝您找到」「找到理想」
REJECT_PHRASES: tuple[str, ...] = (
    "需要其他资质",
    "不合适",
    "不太合适",  # 「不合适」匹配不到「不太合适」，两个都要留（真机里这个更常见）
    "不太匹配",
    "不符合我们",
    "暂不考虑",
    "已经招到",
    "职位已关闭",
    "岗位已关闭",
    "祝您找到",
    "找到理想",
)


def is_rejection(text: str) -> bool:
    """这条 HR 消息是不是明确的拒绝。纯函数，方便单测与事后复盘。"""
    body = (text or "").strip()
    if not body:
        return False
    return any(phrase in body for phrase in REJECT_PHRASES)


def last_inbound_text(messages: list[dict[str, Any]]) -> str:
    """取**最后一条** HR 消息的正文。

    为什么不是拼接全部：拒绝是终态，但会话可能先寒暄、后拒绝；反过来若用
    "历史上出现过拒绝"判定，一条旧拒绝会把后来重新热络的会话误杀。
    只看最后一条，语义才和"这段关系现在的状态"对齐。
    """
    for message in reversed(messages):
        if str(message.get("direction") or "") != "in":
            continue
        return str(message.get("content") or "").strip()
    return ""

# 终态：不再动、不重试
DONE_STATES: frozenset[str] = frozenset(
    {
        "WECHAT_GOT",
        "EXCHANGE_UNCONFIRMED",
        "RISK_LOCKED",
        "REJECTED",
        "EXPIRED",
        "HANDED_OFF",
        "PENDING_MANUAL",
    }
)

# 风控错误码（§5.1 第 6 步）
RISK_ERROR_CODE = "ACCOUNT_RISK"
GREET_LIMIT_ERROR_CODE = "GREET_LIMIT"

# 【P2-B §6.6】detail 失败的**三档**判据。三种后果完全不同，绝不许糊成一档：
#   RISK_ERROR_CODES → 锁风险 + 整轮终止
#   REQUEST_BUDGET_EXHAUSTED → 请求**没发出去**，停止 enrich，不自动重试
#   JOB_NOT_FOUND → 这岗没了/下架了，**正常终态**，算 enriched_failed 但继续抓下一个
RISK_ERROR_CODES: tuple[str, ...] = (
    "ACCOUNT_RISK",
    "ENVIRONMENT_RISK",
    "ENVIRONMENT_RISK_LOCKED",
    "LOGIN_RISK_CONTROL",
)
BUDGET_ERROR_CODE = "REQUEST_BUDGET_EXHAUSTED"
GONE_ERROR_CODE = "JOB_NOT_FOUND"

#: 连续几次 detail 失败就熔断本轮。**3 次是有依据的**：上游 `detail` 在 httpx 失败后
#: 还会降级走浏览器兜底（我们没传 `--lid`，兜底基本拿不到东西，但要白花几十秒 + 一次预算），
#: 于是 3 次失败最坏 = 6 次请求（补页 §C）。
DETAIL_CIRCUIT_FAILURES = 3


def _detail_key(job_id: str, day: str) -> str:
    """detail 账本键：`detail:{job_id}:{YYYYMMDD}`（§6.5）。

    键里带日期 → **跨天自动可重试**，不需要新列（`jd_fetched_at` 已被裁定否决）；
    同一天同一岗靠 `actions.idempotency_key` 的 UNIQUE 白拿"最多一次"。

    > 放在这里而不是 `core/idempotency.py`：`app/core/` 五个既有文件本批**逐字节不变**
    > （冻结清单第 12 条），新增常量必须留在编排层。
    """
    return f"detail:{job_id}:{day}"


# --------------------------------------------------------------------------- #
# 编排用的内部小工具（不是业务规则，规则都在 core/）
# --------------------------------------------------------------------------- #
def risk_locked(request: Request) -> bool:
    """风控锁定标记。与 `paused` **相互独立**（§6）。"""
    return bool(getattr(request.app.state, "risk_locked", False))


def _lock_risk(request: Request) -> None:
    request.app.state.risk_locked = True


def _uid_of_job(adapter, job_id: str) -> str | None:
    """打招呼后按 `job_id` 反查 uid。

    `greet()` 的返回值里没有 uid（它只有 sent/error_code/detail/raw），
    `chat()` 才是正确来源；**不硬编码 `fake_{job_id}`，也不读 adapter 私有字段**。
    """
    for conversation in adapter.chat():
        if conversation.get("job_id") == job_id:
            uid = conversation.get("uid")
            return str(uid) if uid else None
    return None


@router.get("/api/status")
def api_status(
    request: Request,
    db=Depends(get_db),
    cfg: dict[str, Any] = Depends(get_config),
) -> dict[str, Any]:
    """当前 adapter、capabilities、今日额度用量、风控锁定状态。

    额度数字**从配置实时读**（C6），页面不许写死。
    """
    adapter = get_adapter(request)
    quota = cfg.get("quota") or {}
    daily_greet = quota.get("daily_greet")
    used = models.count_actions_today(db, "greet", ok_only=True)

    return {
        "phase": f"{PHASE} · {_mode_label(request)}",
        "adapter": getattr(request.app.state, "adapter_name", None) or cfg.get("adapter", "fake"),
        "adapter_class": type(adapter).__name__,
        "fake_script": getattr(adapter, "script", None),
        "capabilities": sorted(getattr(adapter, "capabilities", set())),
        "paused": is_paused(request),
        # 【e2e §6】只新增顶层字段，现有字段与类型不动
        "risk_locked": risk_locked(request),
        "llm_configured": bool(cfg.get("llm_configured", False)),
        # 【P2-C】生成策略状态：{mode, calls_today, daily_cap, remaining, cache_hit}
        # 另加 billable_calls_today（同值同源）与 last_fallback_reason（可观测降级）
        "llm": request.app.state.llm_gateway.stats_today(),
        "quota": {
            "daily_greet": daily_greet,
            "daily_greet_max": quota.get("daily_greet_max"),
            "used_today": used,
            "remaining": max(int(daily_greet or 0) - used, 0),
            # 【P2-B §6.7】新增 3 键（只增不改）。`used_detail_today` 从 `actions`
            # **实时读**，不许写死——页面上的额度进度是用户判断"今天还能不能跑"的唯一依据。
            "daily_detail": quota.get("daily_detail"),
            "detail_batch": quota.get("detail_batch"),
            "used_detail_today": models.count_actions_today(db, "detail"),
        },
        # 【P2-A 补丁 §1.3】新增字段：上游版本，与 requirements.txt 钉的 3.0.1 不符时给 warning。
        # 只新增，不改现有键。
        "upstream_version": _upstream_version_of(adapter),
        # 【2026-11-11 前端 v2】把"今天到底要发多少条"一次说清。
        #
        # 起因：页面原来只显示 `quota.daily_greet`（配置里的静态 50），而节奏引擎用的是
        # **爬坡目标**（ramp [15,25,40,50] + 当天 cap）。同一屏两个真相：页面说"还能发 38 条"，
        # 引擎其实只差 3 条就停了 → 用户没法判断今天要不要干预。
        # `pacing` 直接复用 `pacer.status()`，页面以它为准；`quota` 保留给没开节奏的旧路径。
        #
        # **必须白名单**：`pacer.status()` 里有 `state_path`（绝对路径），整包透传会把
        # 本机目录结构写进响应。`test_llm_gateway` 那条"响应里不许出现敏感串"的用例
        # 当场就抓到了（它的临时目录名里恰好含 api_key 字样）。
        "pacing": _pace_view(request.app.state.pacer.status()),
        # 闭环 3：当前生效的账号（只有 id/name/端口，不含任何路径）。页面标题行要显示它，
        # 否则多账号场景下"我现在打的是哪个号"根本看不出来，这是最危险的误操作来源。
        "account": cfg.get("active_account"),
        # 诊断用：服务到底读的是哪个库。2026-11-11 我自己踩过 —— 环境变量给了 POSIX 路径，
        # 静默建了个空库、页面全是 0，查了半小时。只给文件名，不给全路径。
        "db": Path(str(request.app.state.db_path)).name,
        # 闭环 2 的两个"成果指标"，比"岗位详情 0/50"（direct_greet 根本不抓详情）有意义得多
        "funnel": {
            "replied": models.count_conversations_with_reply(db),
            "wechat": models.count_conversations_with_wechat(db),
            "rejected": models.count_conversations_in_state(db, "REJECTED"),
        },
        # 【F2】上游失败不许静默：`found=0` 时必须能看出是"没登录/参数非法/被风控/真的没岗"。
        # 只记归一码与上游码，不含原始信封（防 security_id / raw_json 外泄）。
        "last_upstream_error": _upstream_error_of(adapter),
        # 【P2-B F6】上游那把锁在磁盘上（`<data_dir>/cdp_risk_lock.json`），我们这把在内存里。
        # 两把锁语义不同、生命周期不同：重启只清掉**我们**的，上游的必须 `boss clean --risk-lock`。
        # 不显示它，页面就会"重启后看起来正常、实际每一次请求都被拒"。纯新增键。
        "upstream_risk_lock": bool(getattr(request.app.state, "upstream_risk_lock", False)),
        "warnings": cfg.get("warnings", []),
        "pending_modules": list(PENDING_MODULES),
    }


@router.get("/api/jobs")
def api_jobs(db=Depends(get_db)) -> dict[str, Any]:
    """岗位列表（带分数和理由）。分数由 `sweep` 的 matcher 阶段写入。"""
    items = models.list_jobs(db)
    return {"count": len(items), "items": items}


@router.post("/api/run/sweep")
def api_run_sweep(
    request: Request,
    db=Depends(get_db),
    cfg: dict[str, Any] = Depends(get_config),
) -> dict[str, Any]:
    """跑一轮：搜索 → 入库 → 打分 → 过滤 → 额度截断 → 生成开场白 → 打招呼。

    顺序固定（§5.1）：`try_claim` 必须在 `compose_opener` / `greet()` **之前**，
    claim 失败就跳过——不生成、不发送。
    """
    if is_paused(request):
        return {"ok": False, "error": "已暂停，先恢复再跑"}
    if risk_locked(request):
        return {
            "ok": False,
            "error": "风控锁定（RISK_LOCKED）：请人工处理后重启，/api/pause 不能解除",
        }

    adapter = get_adapter(request)
    target = cfg.get("target") or {}
    quota = cfg.get("quota") or {}
    run_id = models.start_run(db, "sweep")
    try:
        # 1) 搜索 + 入库
        jobs = adapter.search("法务", cities=target.get("cities") or [])
        inserted = models.upsert_jobs(db, jobs)

        # 2) 【P2-B §5.1 第 3–4 步】预打分 + 选候选。**必须在 score 之前**。
        #    预打分只是纯函数、只为挑候选，**不写库**（写库的是第 7 步的最终分）。
        base_scores: dict[str, float] = {}
        for job in jobs:
            value, _reasons = matcher.score(job, cfg)
            base_scores[str(job["job_id"])] = value

        today = datetime.now(timezone.utc).strftime("%Y%m%d")
        daily_detail = int(quota.get("daily_detail") or 0)
        detail_batch = int(quota.get("detail_batch") or 0)
        used_detail_today = models.count_actions_today(db, "detail")

        decision = enrich.pick_for_detail(
            jobs,
            base_scores,
            adapter,
            cfg=cfg,
            batch_limit=detail_batch,
            daily_remaining=max(daily_detail - used_detail_today, 0),
            is_claimed=lambda job_id: idem.get_claim(db, _detail_key(job_id, today)) is not None,
            is_greeted=lambda job_id: idem.get_claim(db, idem.greet_key(job_id)) is not None,
        )

        # 3) 【P2-B §5.1 第 5–6 步】串行抓 detail → 合并回 jobs。
        #    冻结清单第 16 条：**不许并发**、不许 `asyncio.gather` / 线程池，
        #    也不许自己加 `sleep` 去"优化"上游节流（那是在跟风控赛跑）。
        enriched = 0
        enriched_failed = 0
        enrich_stopped: str | None = None
        consecutive_failures = 0
        risk_hit = False
        index_of = {str(job["job_id"]): position for position, job in enumerate(jobs)}

        for job_id in decision["todo"]:
            position = index_of.get(job_id)
            if position is None:
                continue
            job = jobs[position]
            key = _detail_key(job_id, today)
            if not idem.try_claim(db, key, "detail", job_id):
                continue  # 同岗同日已占位（重入/并发）：UNIQUE 是最终防线

            payload = adapter.detail(str(job.get("security_id") or ""), job_id)
            if payload.get("ok"):
                # 抓到了就合并（"成功但正文为空"也算成功，§6.6）→ 第 7 步分数才会变
                jobs[position] = enrich.merge_detail(job, payload)
                enriched += 1
                consecutive_failures = 0
                idem.finish_claim(db, key, "ok", {"code": 0})
                continue

            # 失败：**三档分开**（补页 §C/§E），不新增 stats 键，靠 runs.error 把原因说清
            error = payload.get("error") or {}
            raw_code = str(error.get("code") or "")
            normalized = str(error.get("normalized") or error.get("error_code") or "")
            idem.finish_claim(db, key, "failed", {"code": raw_code or None})
            enriched_failed += 1
            consecutive_failures += 1

            if normalized == "RISK_LOCKED" or raw_code in RISK_ERROR_CODES:
                _lock_risk(request)
                risk_hit = True
                enrich_stopped = f"detail 命中风控（{raw_code or normalized}）：锁风险，整轮终止"
                break
            if raw_code == BUDGET_ERROR_CODE or normalized == "LIMIT":
                # **请求根本没发出去**；上游 `recovery_action` 明确写着"不要自动重试"
                enrich_stopped = (
                    f"上游请求预算/限流（{raw_code or normalized}）：本轮停止 enrich，不自动重试"
                )
                break
            if raw_code == GONE_ERROR_CODE or normalized == "GONE":
                continue  # 正常终态：这岗没了/下架了。**不是**风控，**不是**预算问题
            if consecutive_failures >= DETAIL_CIRCUIT_FAILURES:
                enrich_stopped = (
                    f"连续 {consecutive_failures} 次 detail 失败：本轮停止 enrich"
                    f"（上游 httpx 失败还会降级浏览器，一次失败可能连吃 2 次预算）"
                )
                break

        # 4) 第 7–10 步：**落库 → 打分（含被淘汰的）→ 过滤 → 额度截断**。
        #
        #    【F7 · 2026-10-27 B-4a 事故】"写 jd_text + 重算 gap_flags" **必须无条件执行**，
        #    只把"算分 / 过滤 / 打招呼"挡在风控后面。原来整段被 `if not risk_hit:` 包住，
        #    后果是双重损失：
        #      ① 抓到的 JD 只在内存里，进程一退就没；
        #      ② `detail:{job_id}:{今天}` 的 claim 已经是 `ok` → 当天 `claimed_today` 不再抓
        #         ⇒ 那 2 条 JD **当天永久丢失**，预算还白花了。
        #    这就是"最糟失败形态"的变种：不报错、`enriched=2` 看着成功、下次跑拿不到东西。
        scored: list[tuple[dict[str, Any], float]] = []
        passed: list[tuple[dict[str, Any], float]] = []
        selected: list[tuple[dict[str, Any], float]] = []
        skipped_by_quota = 0

        for job in jobs:
            job_id = str(job["job_id"])

            # 【P2-B §5.1 第 8 步】`gap_flags` 必须**重算**：P2-A 的 flag 是基于
            # 无 JD 的列表项算的，"要求持证 / 要求 3 年经验"全部缺失。
            # 正文以 `jobs` 为准，但**不能拿空串盖掉库里的正文**（旧轮次抓到的），
            # 所以空时回读一次库里已有的值（本地读，零请求）。
            stored = models.get_job(db, job_id) or {}
            jd_text = str(job.get("jd_text") or stored.get("jd_text") or "")
            gap_flags = matcher.compute_gap_flags(dict(job, jd_text=jd_text), cfg)
            models.update_job_detail(db, job_id, jd_text, gap_flags)

            if risk_hit:
                continue  # 风控后：**只落库**，不算分、不过滤、不打招呼

            # 【跨轮 JD 评分 · 2026-10-28】最终评分与后续发送准备必须用**同一份补齐正文**的岗位。
            #
            # 修的是这条链条：第一轮抓到 JD 并落库（`daily_greet=0` 时不会发送）→ 第二轮 search
            # 仍返回空正文，而当日 claim 挡住了重复 detail ⇒ 原来 `score(job, …)` 拿的是**空正文
            # 的搜索项**，分数从 82/74 掉回 50，`passed` 从 4 变 0。**去重是对的，评分是错的。**
            #
            # 只回退 `jd_text`：`title` / `city` / `salary` / `hr_active` / `security_id` 等仍是
            # **本轮** search 的值。整行换成 stored 会把旧 `security_id` 带进发送入口——
            # 那是敏感标识，串了就是"拿上一轮的号去打这一轮的招呼"。
            scored_job = dict(job, jd_text=jd_text)
            value, reasons = matcher.score(scored_job, cfg)
            models.update_match_result(db, job_id, value, "；".join(reasons))
            scored.append((scored_job, value))

        if not risk_hit:
            # 过滤 + 稳定排序（分数降序，同分保持 adapter 返回顺序）
            passed = [(job, value) for job, value in scored if matcher.should_greet(value, cfg)]
            passed.sort(key=lambda item: -item[1])

            # 额度截断：先算 used 再 claim，避免本轮自己把自己算超
            used_today = models.count_actions_today(db, "greet", ok_only=True)
            remaining = max(int(quota.get("daily_greet") or 0) - used_today, 0)
            selected = passed[:remaining]
            skipped_by_quota = len(passed) - len(selected)

        # 5) 逐个打招呼（enrich 阶段已命中风控时，这里一条都不发）
        greeted = 0
        already_greeted = 0
        for job, _value in selected:
            if risk_hit:
                break
            job_id = str(job["job_id"])
            key = idem.greet_key(job_id)
            if not idem.try_claim(db, key, "greet", job_id):
                continue  # 已打过招呼，跳过：不生成、不发送

            # 【P2-C】换策略 = 换注入对象：core/composer.py 一行未改，
            # 这里只是把 `llm` 换成 `llm_gateway.writer_for(job)`。
            # llm_calls 的写入由 writer 自己完成（一次生成一行，model 反映实际策略）。
            message = compose_opener(job, "", request.app.state.llm_gateway.writer_for(job))

            result = adapter.greet(job["security_id"], job_id, message)
            if result.get("sent"):
                idem.finish_claim(db, key, "ok", result)
                uid = _uid_of_job(adapter, job_id)
                if uid:
                    models.upsert_conversation(
                        db,
                        uid=uid,
                        job_id=job_id,
                        hr_name=job.get("hr_name"),
                        company=job.get("company"),
                        state=sm.State.SCORED.value,
                    )
                    state = sm.transition(sm.State.SCORED.value, sm.Event.GREET_SENT.value)
                    models.set_conversation_state(
                        db, uid, state, greeted_at=models.utcnow(), last_msg_from="me"
                    )
                greeted += 1
                if result.get("already_greeted"):
                    # 上游第二层幂等命中：**我们没重发**，但结果是"已打过"。
                    # 单独计数，否则"真发出 3 条"和"平台早已打过 3 条"在页面上长得一样。
                    already_greeted += 1
            elif result.get("error_code") is None:
                # 发出了但无法确认：按 UNCONFIRMED 处理，**不重试**，不计入 greeted
                idem.finish_claim(db, key, "unconfirmed", result)
            elif result.get("error_code") == RISK_ERROR_CODE:
                idem.finish_claim(db, key, "failed", result)
                _lock_risk(request)
                risk_hit = True
                break
            else:
                idem.finish_claim(db, key, "failed", result)
                if result.get("error_code") == GREET_LIMIT_ERROR_CODE:
                    break

        upstream_error = _upstream_error_of(adapter)  # 【F2】键恒存在，无错为 None
        stats: dict[str, Any] = {
            "found": len(jobs),
            "inserted": inserted,
            "scored": len(scored),
            "greeted": greeted,
            "skipped_by_quota": skipped_by_quota,
            "risk_locked": risk_hit or risk_locked(request),
            "note": (
                f"P2-B：detail 回填 {enriched}/{len(decision['todo'])} 条 + 打分 + 额度截断"
                f" + 打招呼（{_mode_label(request)}）"
            ),
            "upstream_error": upstream_error,
            # 【P2-B §6.7】5 个新键（旧 8 键一个不动：键名与语义都没变）
            "enriched": enriched,
            "skipped_by_detail_quota": int(decision["skipped_by_quota"]),
            # **本批最重要的新增**：有了 `passed`，`greeted=0` 立刻能区分
            # "没过线"还是"过线没发"。P2-A 缺它，只能拿 `skipped_by_quota=0 && greeted=0` 反推。
            "passed": len(passed),
            "enriched_failed": enriched_failed,
            "enrich_prefiltered": int(decision["prefiltered"]),
            # 【附录 B】区分"真发出"与"平台早已打过"
            "already_greeted": already_greeted,
        }
        # 【F2】有错时把原因写进 runs.error，别让页面只看到一个 0。
        # 【P2-B §6.6】enrich 的中断原因也必须留痕，否则"只抓了 2 条"永远查不出为什么。
        run_error_parts: list[str] = []
        if upstream_error:
            run_error_parts.append(f"upstream {upstream_error['code']}: {upstream_error['message']}")
        if enrich_stopped:
            run_error_parts.append(f"enrich: {enrich_stopped}")
        if enriched == 0 and not (
            decision["prefiltered"] or decision["skipped_by_quota"] or enriched_failed
        ):
            run_error_parts.append(
                "enrich: 本轮没有任何候选取到 detail，且剪枝/额度/失败三个数都是 0"
                "（说明 search 没返回可用岗位，见 found 与 upstream_error）"
            )
        models.finish_run(db, run_id, stats, "；".join(run_error_parts) or None)
    except Exception as exc:  # 排障：写进 runs.error，不让页面只看到 500
        models.finish_run(db, run_id, None, f"{type(exc).__name__}: {exc}")
        return {"ok": False, "run_id": run_id, "error": f"{type(exc).__name__}: {exc}"}

    return {"ok": True, "run_id": run_id, "stats": stats, "pending_modules": list(PENDING_MODULES)}


@router.get("/api/pwa/health")
def api_pwa_health() -> dict[str, Any]:
    """PWA 自检（闭环 6）：离线与通知的资产是否齐全、关键约束是否还在。

    存在的理由：这类资产失效是**静默**的 —— 页面照常打开，只是不能离线/不会通知。
    把约束写成检查项，比事后猜"为什么没弹通知"划算得多。
    """
    from app.core import pwa as pwa_mod

    web_dir = Path(__file__).resolve().parents[1] / "web"
    return pwa_mod.check(web_dir)


@router.post("/api/run/followup")
def api_run_followup(
    request: Request,
    uid: str | None = None,
    db=Depends(get_db),
    cfg: dict[str, Any] = Depends(get_config),
) -> dict[str, Any]:
    """跟进一轮：读会话 → 消息入库 → HR 回复 → 检测交换 → 抓微信号（§5.2）。

    本批**不自动发回复**（`gate.reply=review`），`replied` 恒为 0。

    `?uid=` 只处理指定会话：完整跟进会遍历当前列表里的全部会话（100 个 ≈ 100 次请求），
    在"刚解风控"或"只想补一个会话"的场景下都太贵。单会话补跟进的请求量是 2 次（chat + chatmsg）。
    """
    if is_paused(request):
        return {"ok": False, "error": "已暂停，先恢复再跑"}
    if risk_locked(request):
        return {
            "ok": False,
            "error": "风控锁定（RISK_LOCKED）：请人工处理后重启，/api/pause 不能解除",
        }

    adapter = get_adapter(request)
    gate_exchange = str((cfg.get("gate") or {}).get("exchange") or "review")
    run_id = models.start_run(db, "followup")
    try:
        stored_messages = 0
        exchanged = 0
        wechat_got = 0
        rejected = 0
        exchange_incoming = 0
        risk_hit = False

        # `uid` 模式**不走列表**：`chat()` 只返回首页 100 条，老会话早就掉出去了，
        # 但 `chatmsg` 能直接按 uid 取（实测可用）。所以这里合成一条最小会话记录。
        single_uid = str(uid or "").strip()
        if single_uid:
            all_conversations = [{"uid": single_uid}]
        else:
            all_conversations = adapter.chat()
        for conversation in all_conversations:
            uid = conversation.get("uid")
            if not uid:
                continue
            # 只处理"有新动静"的会话。
            # 【Step 8】真机 chat() 没有 last_msg_from（那是 fake 的便利字段），
            #   原始写法 `src != "hr"` 会让每条会话都被 continue 掉，而且不报错——
            #   最糟的失败形态。这里改用真机真实存在的 unread 兜底。
            has_hr_signal = bool(single_uid) or (
                conversation.get("last_msg_from") == "hr"
                or int(conversation.get("unread") or 0) > 0
            )
            if not has_hr_signal:
                continue

            row = models.get_conversation(db, str(uid))
            if row is None or row.get("state") in DONE_STATES:
                continue  # 终态不重试

            # §5.2 第 2 步：raw=True（接真实 adapter 时交换卡片识别依赖 raw 数据）
            messages = adapter.chatmsg(str(uid), raw=True)
            stored_messages += _store_messages(db, str(uid), messages)

            state = str(row.get("state") or "")
            if state == sm.State.GREETED.value:
                state = sm.transition(state, sm.Event.HR_REPLIED.value)
                models.set_conversation_state(
                    db, str(uid), state, last_msg_at=models.utcnow(), last_msg_from="hr"
                )
            if state != sm.State.HR_REPLIED.value:
                continue

            # 【闭环 2.3 / 2.4 · 2026-11-12】交换微信的识别**不再依赖我方发起**。
            #
            #   真实流程（会话 759861702 样本）里，是 **HR 先发申请卡**（带"同意/拒绝"按钮），
            #   我们点同意，系统再下发 `XX的微信号：<copy>…</copy>`。
            #   原来这段逻辑被包在 `gate_exchange == auto` 分支里 —— 也就是说只要闸门是 review
            #   （默认就是），**连"号码到手了"都不会被记下来**。用户手动点过好几个交换，
            #   库里却一个微信号都没有，就是这个原因。
            #
            #   现在：识别与"我方是否发起"解耦，只读消息就能推进状态。
            for message in messages:
                if str(message.get("direction") or "") != "in":
                    continue
                content = message.get("content")

                if exchange_mod.is_exchange_request(content):
                    if not row.get("exchange_incoming_at"):
                        state = _advance(db, str(uid), state, sm.Event.EXCHANGE_IN.value)
                        models.set_conversation_state(
                            db, str(uid), state, exchange_incoming_at=models.utcnow()
                        )
                        exchange_incoming += 1

                wechat_id = exchange_mod.extract_wechat(content)
                if wechat_id:
                    # 状态推进：EXCHANGE_INCOMING/EXCHANGE_REQUESTED/HR_REPLIED --EXCHANGE_DONE--> WECHAT_GOT
                    state = _advance(db, str(uid), state, sm.Event.EXCHANGE_DONE.value)
                    models.set_conversation_state(
                        db,
                        str(uid),
                        state,
                        wechat_id=wechat_id,
                        wechat_source="chatmsg",
                        exchange_agreed_at=models.utcnow(),
                    )
                    wechat_got += 1
                    break  # 一个会话只记一次号码

            # 【闭环 2.1】明确拒绝 → 终态，不再跟、也不试探交换。
            #   顺序必须在交换检测**之前**：拒绝信里常带"祝您找到"，而"祝您找到"也可能
            #   出现在别的语境；先判拒绝可以让这类会话直接停下，不去多做一次写操作评估。
            if is_rejection(last_inbound_text(messages)):
                state = sm.transition(state, sm.Event.REJECTED_BY_HR.value)
                models.set_conversation_state(db, str(uid), state, last_msg_from="hr")
                rejected += 1
                continue

            # 检测交换请求（只看 HR 发来的内容）
            inbound = " ".join(
                str(m.get("content") or "") for m in messages if m.get("direction") == "in"
            )
            if not any(word in inbound for word in EXCHANGE_KEYWORDS):
                continue  # 普通回复：只入库、不自动回复
            if gate_exchange != "auto":
                continue  # review 模式：只记录"检测到交换请求"，状态停在 HR_REPLIED

            key = idem.exchange_key(str(uid))
            if not idem.try_claim(db, key, "exchange", str(uid)):
                continue  # 已经发起过，不重复

            result = adapter.exchange(str(uid), type="wechat")
            if result.get("sent"):
                state = sm.transition(state, sm.Event.EXCHANGE_REQ_OK.value)
                models.set_conversation_state(
                    db, str(uid), state, exchange_requested_at=models.utcnow()
                )
                # §5.2 顺序：sent=True → finish_claim(ok) → 再回读抓号。
                # 先 finish_claim：回读若抛异常，也不会把 action 留在 pending。
                idem.finish_claim(db, key, "ok", result)
                exchanged += 1

                # 回读抓号：交换成功后 chatmsg 才会带出号码那条
                again = adapter.chatmsg(str(uid))
                stored_messages += _store_messages(db, str(uid), again)
                body = " ".join(str(m.get("content") or "") for m in again if m.get("direction") == "in")
                wechat_id = extract_wechat(body)
                if wechat_id:
                    state = sm.transition(state, sm.Event.EXCHANGE_DONE.value)
                    models.set_conversation_state(
                        db,
                        str(uid),
                        state,
                        wechat_id=wechat_id,
                        wechat_source="chatmsg",
                        exchange_agreed_at=models.utcnow(),
                    )
                    wechat_got += 1
            elif result.get("error_code") is None:
                # 发出了但无法确认：EXCHANGE_REQUESTED --UNCONFIRMED--> EXCHANGE_UNCONFIRMED，不重试
                idem.finish_claim(db, key, "unconfirmed", result)
                state = sm.transition(state, sm.Event.EXCHANGE_REQ_OK.value)
                models.set_conversation_state(
                    db, str(uid), state, exchange_requested_at=models.utcnow()
                )
                state = sm.transition(state, sm.Event.UNCONFIRMED.value)
                models.set_conversation_state(db, str(uid), state)
            elif result.get("error_code") == RISK_ERROR_CODE:
                idem.finish_claim(db, key, "failed", result)
                _lock_risk(request)
                state = sm.transition(state, sm.Event.RISK_HIT.value)
                models.set_conversation_state(db, str(uid), state)
                risk_hit = True
                break
            else:
                idem.finish_claim(db, key, "failed", result)

        stats: dict[str, Any] = {
            # 与骨架批口径一致：`chat()` 返回的会话总数（不是本轮处理的 HR 会话数）
            "conversations": len(all_conversations),
            "stored_messages": stored_messages,
            "replied": 0,  # gate.reply=review：本批不发回复
            "exchanged": exchanged,
            "wechat_got": wechat_got,
            "rejected": rejected,
            "exchange_incoming": exchange_incoming,
            "risk_locked": risk_hit or risk_locked(request),
            "note": f"e2e 批：chatmsg 入库 + 交换检测 + 微信号提取已接入（{_mode_label(request)}）",
            "upstream_error": _upstream_error_of(adapter),
        }
        _followup_error = stats["upstream_error"]
        models.finish_run(
            db,
            run_id,
            stats,
            f"upstream {_followup_error['code']}: {_followup_error['message']}"
            if _followup_error
            else None,
        )
    except Exception as exc:
        models.finish_run(db, run_id, None, f"{type(exc).__name__}: {exc}")
        return {"ok": False, "run_id": run_id, "error": f"{type(exc).__name__}: {exc}"}

    return {"ok": True, "run_id": run_id, "stats": stats}


def _normalize_created_at(value: Any) -> str | None:
    """上游 `created_at` 是**毫秒整数**（如 `1794193397030`），库里按 ISO 8601 UTC 文本存。

    【2026-11-09 真机实测】不归一有两个后果：
    ① 同一列既有 `"2026-11-09T12:00:00Z"` 又有 `1794193397030`，按字符串排序时数字排在
       字母前面 → 时间线乱；
    ② 页面/报告把数字当 ISO 解析 → 显示到 1970 年附近。
    """
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        stamp = float(text)
        if stamp > 1e12:  # 毫秒
            stamp /= 1000.0
        return datetime.fromtimestamp(stamp, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    return text  # 已经是 ISO，原样保留


def _advance(db, uid: str, state: str, event: str) -> str:
    """尽力推进状态：这条边不合法就原样返回，不抛。

    为什么不抛：消息识别是"读"的产物，状态机是"写"的约束。一条历史消息不该因为
    当前状态不接受这条边，就把整轮跟进打断 —— 记日志 + 跳过才是对的行为。
    """
    if sm.can_transition(state, event):
        return sm.transition(state, event)
    return state


def _readable_content(message: dict[str, Any]) -> str | None:
    """把卡片类 `content` 换成一句人话（委托给 `app.core.exchange.readable`）。

    卡片识别与"换人话"必须**同一份实现**：两处各写一套，迟早出现
    "列表上写着『卡片消息』但识别器认不出来"这种自相矛盾。
    """
    return exchange_mod.readable(message.get("content"))


def _store_messages(db, uid: str, messages: list[dict[str, Any]]) -> int:
    """把一批消息入库，返回**实际新增**条数（重复的靠 UNIQUE 忽略）。"""
    added = 0
    for message in messages:
        platform_mid = message.get("platform_mid")
        if not platform_mid:
            continue  # 【F1】拿不到 mid 的消息不入库，不许编假 mid
        if models.insert_message(
            db,
            uid=uid,
            platform_mid=str(platform_mid),
            direction=str(message.get("direction") or "in"),
            msg_type=str(message.get("msg_type") or "text"),
            content=_readable_content(message),
            # 原始载荷必须留：闭环 2 的"交换微信卡片识别"依赖 raw 字段，丢了只能重新拉一遍
            raw_json=message,
            created_at=_normalize_created_at(message.get("created_at")),
        ):
            added += 1
    return added


@router.post("/api/pause")
def api_pause(request: Request, paused: bool = True) -> dict[str, Any]:
    """暂停 / 恢复。同一个端点，`?paused=false` 恢复（保持 §6.1 的 9 个接口不扩容）。

    只动 `paused`，**绝不清 `risk_locked`**（§6：两者相互独立）。
    """
    request.app.state.paused = bool(paused)
    return {
        "ok": True,
        "paused": request.app.state.paused,
        "risk_locked": risk_locked(request),
    }


@router.get("/api/pending")
def api_pending(db=Depends(get_db)) -> dict[str, Any]:
    """待人工处理队列。Phase 1 是空列表，但结构要在（§6.2）。"""
    items = models.list_pending_manual(db)
    return {
        "count": len(items),
        "items": items,
        "note": "PENDING_MANUAL 在 Phase 1 无任何入边（§2.2），空列表是正确结果",
    }

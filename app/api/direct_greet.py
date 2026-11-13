"""搜索直发：`POST /api/run/direct-greet`（任务书-搜索直发-最小沟通闭环 §2）。

口径与 `sweep` **刻意不同**：用户裁定"实际沟通成功即可"，所以这条路不经过
detail 回填、不经过 matcher 评分、不经过 LLM/模板 —— 搜索回来直接发固定消息。

保留的共用部分一条不少（否则就是另建一套系统）：

- **去重**：`idem.greet_key()` + `idem.try_claim()`，与 `sweep` 共用同一张 `actions` 表；
- **额度**：`models.count_actions_today(db, \"greet\")` 与 `quota.daily_greet`，
  所有发送路径共用同一本账；
- **风险/暂停**：直接复用 `dashboard.risk_locked()` / `_lock_risk()` 与 `RISK_ERROR_CODES`，
  不复制一套风险状态；
- **反查与落库**：复用 `dashboard._uid_of_job()` 的 chat 反查口径与
  `upsert_conversation` + `state_machine` 的 GREETED 规则。

fix9 收口（`修单-Phase2-B_fix9-直发停轮与错误记录.md`）四条：

- **F1** 搜索失败（普通 / 风险，**与 jobs 空不空无关**）一律先停：零 greet、零 greet claim；
  风险族或 `normalized=RISK_LOCKED` 另置共用内存锁，下次调用在 search 之前就拒绝。
- **F2** 停轮判据**同时**看 raw 与 normalized（`_classify_error`）：任一命中即归类，
  不许 `raw or normalized` 之后只匹配单一码；已成功的新发不因反查失败改判。
- **F3** search / greet / chat **每次调用后**取一次"本次是否新失败"的快照，保存
  "最后一次真实失败摘要"（普通成功不擦），响应 `stats.upstream_error`、顶层
  `upstream_error`、`runs.stats_json` 三处同源；只有规范 `error_code` 时合成最小安全摘要。
- **F4** 反查 / 会话同步抛异常：保留 claim=ok 与 `sent_new`，写可读告警、停轮、不重试；
  `runs.error` 与响应**不拼接 `str(exc)`**，只留安全阶段 + 异常类型。

不改 `sweep`、不改 core / adapter / models / config，也不新增表或列。

【闭环 1 改动】两条，其余口径一律不动：

- **文案不再固定**：`FIXED_MESSAGE` 已删除，改由 `app/core/greet_pool.py` 的文案池
  生成（≥20 条模板 + {title}/{company} 变量 + 同轮避让）。原因见
  `config/prompts/greet_templates.yaml` 顶部：真机 11 条 77 秒一字不差，是风控直接诱因。
  池子缺失/为空 → 当场报错，**不许静默回落到固定文案**。
- **单轮可以只要 1 条**：新增 `limit` 参数，`run_direct_greet_pass()` 供 HTTP 接口与
  后台推进器（`app/services/pacer.py`）共用。HTTP 接口不传 = 发到额度剩余；
  pacer 传 1 = 一次只发一条，间隔由 pacer 控制。
- 返回值新增 `verdict`（`ok|skipped|risk|limit|auth|unconfirmed|error`），
  纯增量字段，供 pacer 决定"要不要冷却/熔断"。既有字段一个没改。
"""

from __future__ import annotations

import random
import time
from typing import Any

from fastapi import APIRouter, Depends, Request

from app import models
from app.adapters.boss import (
    ERROR_CODES_AUTH,
    ERROR_CODES_LIMIT,
    normalize_error_code,
)
from app.api import get_adapter, get_config, get_db, is_paused
from app.api.dashboard import (
    RISK_ERROR_CODES,
    _lock_risk,
    _uid_of_job,
    _upstream_error_of,
    risk_locked,
)
from app.core import greet_pool as greet_pool_module
from app.core import idempotency as idem
from app.core import search_targets
from app.core import state_machine as sm

router = APIRouter(tags=["direct_greet"])

#: 本接口要求的适配器能力（缺一个就明确失败，不默认继续）—— 见 §2
REQUIRED_CAPABILITIES: tuple[str, ...] = ("search", "greet")

#: 归一码（`boss.normalize_error_code` 的输出口径）——停轮判据的两个坐标之一（F2）
NORMALIZED_RISK = "RISK_LOCKED"
NORMALIZED_LIMIT = "LIMIT"
NORMALIZED_AUTH = "AUTH"
NORMALIZED_OK = "OK"

#: 合成摘要的固定 message（F3）：不含任何上游 detail 文本、凭据或 URL
SYNTHETIC_MESSAGE = "上游返回失败（本层无可诊断文本，见 code / normalized）"

#: F4 告警的固定提示语（阶段 / 异常类型 / 安全提示三段）
SYNC_HINT = "已发送/未同步/不重试"


def _stats_template() -> dict[str, Any]:
    """响应统计口径（交付说明里有逐项定义）。

    `eligible` = 同轮去重 + 过滤缺 ID + 过滤历史 claim 之后的候选数；
    `skipped_invalid` = 缺 `job_id` / 缺 `security_id` / 同轮重复 的**列表项**数；
    `skipped_claimed` = 已有 greet claim（含历史轮次）而被剪掉的项数；
    `skipped_by_quota` = 过了去重与 claim 过滤、但被当日额度截掉的项数。

    `upstream_error`（F3）= **本轮最后一次真实上游失败**的最小安全摘要；
    普通成功不清掉它，`ALREADY_GREETED` / `normalized=OK` 不算失败、不会写进来。
    """
    return {
        "found": 0,
        "eligible": 0,
        "sent_new": 0,
        "already_greeted": 0,
        "skipped_claimed": 0,
        "skipped_invalid": 0,
        "skipped_by_quota": 0,
        "failed": 0,
        "unconfirmed": 0,
        "risk_locked": False,
        "upstream_error": None,
        "conversation_warnings": [],
    }


def _capability_gap(adapter: Any) -> list[str]:
    """`capabilities` 里有、且方法真的可调用 —— 两个条件都要满足（双守卫）。"""
    declared = set(getattr(adapter, "capabilities", set()) or set())
    gaps: list[str] = []
    for name in REQUIRED_CAPABILITIES:
        if name not in declared or not callable(getattr(adapter, name, None)):
            gaps.append(name)
    return gaps


def _claim_payload(code: int | None, **extra: Any) -> dict[str, Any]:
    """写进 `actions.response_json` 的东西。

    **只留白名单字段**：`greet().raw` 在本项目只允许 `{\"code\": 0}`，`security_id`
    与原始信封一律不进库（P2-B 冻结清单）。失败只记上游码。

    `tpl` 是本地模板 id（如 `c_02`），用于复盘"哪条文案的回复率更高"，
    不含任何上游数据，属于安全的本地可观测字段。
    """
    payload: dict[str, Any] = {"code": code}
    payload.update(extra)
    return payload


def _greet_pool(request: Request, cfg: dict[str, Any]) -> Any:
    """取文案池。启动时 `main.py` 已装配；测试/脚本直接调本函数时现场装载并缓存。

    **装载失败不许回落到固定文案**——固定文案正是闭环 1 要消灭的东西，
    悄悄降级会让"已经改好了"变成一句空话。
    """
    cached = getattr(request.app.state, "greet_pool", None)
    if cached is not None:
        return cached
    pool = greet_pool_module.load_pool(cfg)
    request.app.state.greet_pool = pool
    return pool


# --------------------------------------------------------------------------- #
# 【fix9 · F2/F3】上游错误的快照、合成与归类 —— 三个纯函数，不碰 I/O
# --------------------------------------------------------------------------- #
def _error_snapshot(adapter: Any) -> Any:
    """调用**前**取一次 `last_error` 快照（F3 的判据基础）。"""
    return getattr(adapter, "last_error", None)


def _new_failure(adapter: Any, before: Any) -> dict[str, Any] | None:
    """本次调用是否**换出来**一个新的上游失败；是则返回脱敏摘要。

    真适配器是长生命周期对象，`last_error` 会跨调用留着。若直接读"最后一次调用的
    `last_error`"，上一阶段的失败会被重复算成本阶段的失败（修单 F3 明令禁止）。
    所以只认"快照**换了对象**"的那一次 —— **不比较内容**（fix10 R1）：上游信封里没有
    错误时间戳，连续两次同样的失败完全可能；按内容相等判"没发生"，会把整轮搜索失败
    吞掉，然后带着一次失败的搜索照常去发招呼。反过来，同一个对象原样留着才算真的
    "没有新失败"（那是上一阶段遗留下来的旧属性，不许误报）。
    """
    after = _error_snapshot(adapter)
    if not after:
        return None
    if after is before:
        return None
    return _upstream_error_of(adapter)


def _synthetic_summary(command: str, code: Any) -> dict[str, Any]:
    """桩只给规范 `result.error_code`、没有 `last_error` 时的**最小安全摘要**。

    只留 `command` / `code` / `normalized` / 固定 `message`；**不写 raw、不写
    `security_id`、不写任何上游 detail 文本**（修单 F3 末段）。归一码走
    `boss.normalize_error_code`——复用既有实现，不在这里复制一套码表。
    """
    text = str(code) if code is not None else None
    return {
        "command": command,
        "code": text,
        "normalized": normalize_error_code(text),
        "message": SYNTHETIC_MESSAGE,
    }


def _classify_error(code: Any, normalized: Any) -> str | None:
    """raw 与 normalized **同时**参与判定（F2）：任一族命中即归类，不许互相遮蔽。

    返回 `"ok"` / `"risk"` / `"limit"` / `"auth"` / `"unconfirmed"` / `"other"` / `None`。
    旧代码是 `str(payload.get("code") or payload.get("normalized"))` 再单码匹配 ——
    raw 一旦有值（哪怕是本层不认识的码），normalized 就被整条遮掉。
    """
    raw = str(code or "")
    norm = str(normalized or "")
    if norm == NORMALIZED_OK:
        # `ALREADY_GREETED` / `ALREADY_APPLIED` 归一后的形状：幂等成功，**不是错误**
        return "ok"
    if raw in RISK_ERROR_CODES or norm == NORMALIZED_RISK:
        return "risk"
    if raw in ERROR_CODES_LIMIT or norm == NORMALIZED_LIMIT:
        return "limit"
    if raw in ERROR_CODES_AUTH or norm == NORMALIZED_AUTH:
        return "auth"
    if norm == "UNCONFIRMED":
        return "unconfirmed"
    return "other" if (raw or norm) else None


def _code_text(summary: dict[str, Any] | None, fallback: Any = None) -> str:
    """给人看的错误码文本（优先 raw，其次归一码）——只用于 `stop_reason` / 告警。"""
    if summary:
        return str(summary.get("code") or summary.get("normalized") or "UNKNOWN")
    return str(fallback or "UNKNOWN")


def run_direct_greet_pass(
    request: Request,
    db: Any,
    cfg: dict[str, Any],
    limit: int | None = None,
    page: int | None = None,
    url: str | None = None,
    send_gap: tuple[int, int] | None = None,
) -> dict[str, Any]:
    """搜索 → 去重/claim 过滤 → 额度截断 → 逐条发文案池生成的招呼 → 记录结果。

    `limit`：本轮最多发几条。`None` = 发到当日额度用完（HTTP 手动入口的旧口径）；
    传 `1` = 一次只发一条（老口径）；传 `N` = 一次搜索领 N 条（请求形状改造）。

    `send_gap`：**批内**两条之间的等待区间（秒）。只在第 2 条起生效 ——
    用户点了"立即沟通"是立刻发，等待应该发生在"看完一个 JD 再去点下一个"之间。
    为什么必须有它：`limit=N` 如果不配 `send_gap`，就是 N 条连发，
    比"每条重载一次列表"危险得多。**间隔不能省，只是省掉重载。**

    `page`：**只搜这一页**（`None` = 按 `adapter_options.pages` 顺序翻）。pacer 每次
    传一个轮转页码：2026-11-04 实测"一个进程里连翻 5 页"会在第 2 页触发
    `ENVIRONMENT_RISK`，而单页请求连续两次都正常。手动入口不传，保持原口径。

    `url`：本次用哪个搜索 URL（不同关键词 = 不同候选池）。`None` = 用配置默认值。
    为什么需要它：claim 是**永久**的，单关键词在淄博只够一天半；换词是扩大候选池
    最省的办法（不增加请求频率，只是把请求摊到不同查询上）。
    """
    if is_paused(request):
        return {"ok": False, "error": "已暂停，先恢复再跑", "verdict": "paused"}
    if risk_locked(request):
        return {
            "ok": False,
            "error": "风控锁定（RISK_LOCKED）：请人工处理后重启，/api/pause 不能解除",
            "verdict": "risk",
        }

    adapter = get_adapter(request)
    gaps = _capability_gap(adapter)
    if gaps:
        return {
            "ok": False,
            "error": f"适配器能力不足：缺少 {gaps}（本接口要求 search + greet 且方法可调用），不继续",
            "verdict": "config",
        }

    # 文案池在打招呼之前取好：池子坏了就不要 search（一次 search 也算一次平台请求）
    try:
        pool = _greet_pool(request, cfg)
    except Exception as exc:  # 配置类错误：文本要给用户看，不能吞
        return {
            "ok": False,
            "error": f"招呼文案池不可用：{exc}",
            "verdict": "config",
        }

    target = cfg.get("target") or {}
    quota = cfg.get("quota") or {}
    stats = _stats_template()
    run_id = models.start_run(db, "direct_greet")
    stop_reason: str | None = None
    #: F4：反查/落库异常走这条口子 —— 顶层 ok=False，但**发送已经发生**
    sync_error: str | None = None
    #: F3：本轮"最后一次真实失败摘要"（普通成功不清掉它）
    failure_summary: dict[str, Any] | None = None
    #: 闭环 1：本轮为什么停（`risk|limit|auth|unconfirmed`），pacer 据此决定冷却/熔断
    halt_kind: str | None = None

    try:
        # 1) 搜索：沿用既有查询口径（`search_url` 接线在适配器内部，这里不多加条件）
        search_before = _error_snapshot(adapter)
        jobs = adapter.search(
            "法务",
            cities=target.get("cities") or [],
            **({"page": page} if page is not None else {}),
            **({"url": url} if url else {}),
        )
        stats["found"] = len(jobs)
        # 把本轮搜索到的岗位落一行（`ON CONFLICT DO NOTHING`，不覆盖已存正文）。
        # 两个理由：① `conversations.job_id` 有外键指向 `jobs`，不落行就无法记录会话；
        # ② 追溯需要"这一轮到底扫到了什么"。**不做评分、不回填 detail、不写 match_score。**
        # 搜索失败时也照落：修单允许"部分岗位保留落库"。
        models.upsert_jobs(db, jobs)

        # 【F1】搜完立刻取本次摘要，**不看 jobs 空不空**：真适配器分页可以先返回
        # 一部分岗位、再报错，所以"有岗位"绝不能当作"搜索成功"。
        search_failure = _new_failure(adapter, search_before)
        if search_failure:
            failure_summary = search_failure
            kind = _classify_error(
                search_failure.get("code"), search_failure.get("normalized")
            )
            code_text = _code_text(search_failure)
            if kind == "risk":
                _lock_risk(request)
                stats["risk_locked"] = True
                stop_reason = (
                    f"search 命中风控（{code_text}）：锁风险并停止本轮，未发送任何招呼"
                )
            else:
                stop_reason = (
                    f"search 失败（{code_text}）：停止本轮，未发送任何招呼（不锁风险）"
                )
            stats["upstream_error"] = failure_summary
            models.finish_run(db, run_id, stats, stop_reason)
            return {
                "ok": False,
                "run_id": run_id,
                "stats": stats,
                "upstream_error": failure_summary,
                "error": stop_reason,
                "verdict": kind if kind in ("risk", "limit", "auth") else "error",
            }

        # 2) 同轮去重 + 过滤缺 ID（顺序：先洗数据，再谈额度）
        seen: set[str] = set()
        valid: list[dict[str, Any]] = []
        for job in jobs:
            job_id = str(job.get("job_id") or "")
            security_id = str(job.get("security_id") or "")
            if not job_id or not security_id:
                stats["skipped_invalid"] += 1
                continue
            if job_id in seen:
                stats["skipped_invalid"] += 1  # 同轮重复：同一岗位只发一次
                continue
            seen.add(job_id)
            valid.append(job)

        # 3) 先过滤历史 claim，**再**截额度：否则前面全是已发过的岗位时，
        #    额度会被白白占掉，后面的新岗位永远轮不到。
        #
        # 【2026-11-12 城市白名单】搜索结果的 `city` 并不总是目标城市：实测
        # "潍坊搜索里混着淄博岗位"、反向也有，导致 124 条招呼里 82 条打给了潍坊。
        # 打错城市既浪费额度，也是对 HR 的骚扰，而且撤回不了。
        # 城市集合走 `search_targets.city_names` —— 兼容 `["淄博"]` 与
        # `[{name: 淄博, code: …}]` 两种配置形态，两处各解析一遍必然打架。
        city_allow = search_targets.city_names(cfg.get("target") or {})
        eligible: list[dict[str, Any]] = []
        for job in valid:
            job_id = str(job["job_id"])
            if city_allow and str(job.get("city") or "").strip() not in city_allow:
                stats["skipped_city"] = int(stats.get("skipped_city") or 0) + 1
                continue
            if idem.get_claim(db, idem.greet_key(job_id)) is not None:
                stats["skipped_claimed"] += 1
                continue
            eligible.append(job)
        stats["eligible"] = len(eligible)

        # 4) 日额度截断（与 sweep 共用同一本账）+ 「本轮上限」
        #    `limit` 只收紧、不放大：额度已经用完时传 limit=5 也不会多发一条。
        daily_left = max(
            int(quota.get("daily_greet") or 0)
            - models.count_actions_today(db, "greet", ok_only=True),
            0,
        )
        remaining = daily_left if limit is None else min(daily_left, max(int(limit), 0))
        selected = eligible[:remaining]
        stats["skipped_by_quota"] = len(eligible) - len(selected)

        # 5) 串行发送：逐条 try_claim，抢不到就跳过（不生成、不发送、不重试）
        #    `used_texts` 跨整轮共享：本轮用过的文案不再给下一个 HR，
        #    这是"同轮不撞句"的唯一实现点（文案池自己不知道本轮的上下文）。
        used_texts: set[str] = set()
        sent_in_pass = 0
        for job in selected:
            # 【2026-11-13】批内间隔：第 2 条起才等待（第 1 条是"刚点开就沟通"）
            if send_gap and sent_in_pass > 0:
                time.sleep(random.uniform(*send_gap))
            job_id = str(job["job_id"])
            security_id = str(job["security_id"])
            key = idem.greet_key(job_id)
            if not idem.try_claim(db, key, "greet", job_id):
                stats["skipped_claimed"] += 1
                continue

            message, template_id = pool.pick_detail(job, avoid=used_texts)
            used_texts.add(message)

            greet_before = _error_snapshot(adapter)
            result = adapter.greet(security_id, job_id, message)
            error_code = result.get("error_code")
            # 【F3】greet 之后立刻取摘要：适配器换了 `last_error` 就用它，
            # 否则（桩只给规范码）合成最小安全摘要，保证"普通失败也可见"。
            failure = _new_failure(adapter, greet_before)
            if failure is None and not result.get("sent") and error_code is not None:
                failure = _synthetic_summary("greet", error_code)
            kind = _classify_error(
                failure.get("code") if failure else error_code,
                failure.get("normalized") if failure else None,
            )

            if result.get("sent"):
                # 上游第二层幂等命中：**我们没重发**，但结果是"平台早已打过"。
                # 单独计数，否则"真发出 N 条"和"平台早已打过 N 条"在结果里长得一样。
                if result.get("already_greeted"):
                    idem.finish_claim(db, key, "ok", _claim_payload(0, already_greeted=True))
                    stats["already_greeted"] += 1
                    continue
                idem.finish_claim(db, key, "ok", _claim_payload(0, tpl=template_id))
                stats["sent_new"] += 1
                sent_in_pass += 1
            elif error_code is None:
                # 发出了但无法确认：按 UNCONFIRMED 处理，**不重试**，且停本轮
                idem.finish_claim(db, key, "unconfirmed", _claim_payload(None, tpl=template_id))
                stats["unconfirmed"] += 1
                if failure:
                    failure_summary = failure
                halt_kind = "unconfirmed"
                stop_reason = "greet 结果无法确认（UNCONFIRMED）：停轮，不自动重试"
                break
            else:
                if kind == "ok":
                    # `ALREADY_GREETED` 走这一支（`FakeAdapter` 直接回 err_result）。
                    # 归一码是 OK → 幂等成功：claim 记 ok、计入 already、**不留失败摘要**。
                    idem.finish_claim(db, key, "ok", _claim_payload(0, already_greeted=True))
                    stats["already_greeted"] += 1
                    continue
                idem.finish_claim(
                    db, key, "failed", _claim_payload(None, error=str(error_code), tpl=template_id)
                )
                stats["failed"] += 1
                if failure:
                    failure_summary = failure
                code_text = _code_text(failure, error_code)
                if kind == "risk":
                    _lock_risk(request)
                    halt_kind = "risk"
                    stop_reason = f"greet 命中风控（{code_text}）：锁风险并停轮"
                    break
                if kind == "limit":
                    halt_kind = "limit"
                    stop_reason = f"greet 命中限流（{code_text}）：停轮"
                    break
                if kind == "auth":
                    halt_kind = "auth"
                    stop_reason = (
                        f"greet 认证失败（{code_text}）：停轮，需重新同步登录态"
                    )
                    break
                continue  # 普通失败：该岗不重试，继续下一条

            # 6) 已成功计入新发之后，才做会话反查与落库。
            #    **反查失败不允许把它改判成失败，也不允许导致重发** —— claim 已经是 ok。
            lookup_before = _error_snapshot(adapter)
            uid: str | None = None
            chat_kind: str | None = None
            chat_text = ""
            try:
                uid = _uid_of_job(adapter, job_id)
            except Exception as exc:  # 【fix9 F4 / fix10 R2】反查抛异常：先摘要+分类，再停轮
                chat_failure = _new_failure(adapter, lookup_before)
                if chat_failure:
                    failure_summary = chat_failure
                    chat_kind = _classify_error(
                        chat_failure.get("code"), chat_failure.get("normalized")
                    )
                    chat_text = _code_text(chat_failure)
                    stats["conversation_warnings"].append(
                        f"{job_id}: 会话反查返回上游错误（{chat_text}）"
                    )
                    if chat_kind == "risk":
                        _lock_risk(request)
                stats["conversation_warnings"].append(
                    f"{job_id}: 会话反查阶段异常（{type(exc).__name__}）：{SYNC_HINT}"
                )
                if chat_failure and chat_kind == "risk":
                    stop_reason = (
                        f"会话反查命中风控（{chat_text}）且反查异常"
                        f"（{type(exc).__name__}）：锁风险并停轮"
                    )
                else:
                    stop_reason = (
                        f"会话反查异常（{type(exc).__name__}）：停止本轮，不自动重试"
                    )
                sync_error = stop_reason
                break

            # 【fix10 R2】chat 之后的摘要与风险分类**立刻**做，不等会话同步成功：
            # 旧实现把这段放在落库之后，落库一抛异常就整段跳过 —— 已知的 RISK_LOCKED
            # / raw 风险族既没锁、也没进摘要。分类只决定"停不停/锁不锁"，不决定
            # "要不要记"，所以必须前置。
            chat_failure = _new_failure(adapter, lookup_before)
            if chat_failure:
                failure_summary = chat_failure
                chat_kind = _classify_error(
                    chat_failure.get("code"), chat_failure.get("normalized")
                )
                chat_text = _code_text(chat_failure)
                stats["conversation_warnings"].append(
                    f"{job_id}: 会话反查返回上游错误（{chat_text}）"
                )
                if chat_kind == "risk":
                    _lock_risk(request)

            if uid:
                try:
                    models.upsert_conversation(
                        db,
                        uid=uid,
                        job_id=job_id,
                        hr_name=job.get("hr_name"),
                        company=job.get("company"),
                        # 状态机要求 SCORED --GREET_SENT--> GREETED。这里用 SCORED 只是
                        # 走既有过渡的中间态，**不是**"这个岗位被评分过"的证据（本接口不算分）。
                        state=sm.State.SCORED.value,
                    )
                    state = sm.transition(sm.State.SCORED.value, sm.Event.GREET_SENT.value)
                    models.set_conversation_state(
                        db, uid, state, greeted_at=models.utcnow(), last_msg_from="me"
                    )
                except Exception as exc:  # 【F4】会话同步异常：同样保留成功账本
                    stats["conversation_warnings"].append(
                        f"{job_id}: 会话落库阶段异常（{type(exc).__name__}）：{SYNC_HINT}"
                    )
                    stop_reason = (
                        f"会话落库异常（{type(exc).__name__}）：停止本轮，不自动重试"
                    )
                    sync_error = stop_reason
                    break
            else:
                # uid 未知：**不伪造会话**、不先造一条 SCORED 冒充执行过评分。
                stats["conversation_warnings"].append(
                    f"{job_id}: 已发送成功，但按 job_id 反查不到 uid（会话未同步，claim 已保留）"
                )

            # 【F2】反查本身也可能撞上游错误：风险/限流/认证一律停轮（已发送的那条不动）
            # 摘要在上面已经记过、风险在上面已经置锁；这里只负责"停轮"这一件事，
            # 放在会话同步之后 —— 同步失败时会走 sync_error 那条早退口子。
            if chat_failure and chat_kind in ("risk", "limit", "auth"):
                if chat_kind == "risk":
                    halt_kind = "risk"
                    stop_reason = f"会话反查命中风控（{chat_text}）：锁风险并停轮"
                else:
                    halt_kind = chat_kind
                    stop_reason = f"会话反查受限/认证失败（{chat_text}）：停轮"
                break

        stats["risk_locked"] = risk_locked(request)
        stats["upstream_error"] = failure_summary
        verdict = _verdict_of(halt_kind, sync_error, stats)
        models.finish_run(db, run_id, stats, sync_error or stop_reason)
        if sync_error:
            # 运行异常但发送已发生：ok=False，同时把可读告警和完整 stats 交回去
            return {
                "ok": False,
                "run_id": run_id,
                "stats": stats,
                "upstream_error": failure_summary,
                "error": sync_error,
                "verdict": verdict,
                "note": "运行异常但发送已发生：以 stats.sent_new 为准确认实际发出量",
            }
    except Exception as exc:  # 【F4】兜底：只记安全阶段 + 类型，**不透传原始异常文本**
        safe_error = f"直发运行异常（{type(exc).__name__}）：已停止本轮，不自动重试"
        stats["risk_locked"] = risk_locked(request)
        stats["upstream_error"] = failure_summary
        models.finish_run(db, run_id, stats, safe_error)
        return {
            "ok": False,
            "run_id": run_id,
            "stats": stats,
            "upstream_error": failure_summary,
            "error": safe_error,
            "verdict": "error",
        }

    return {
        "ok": True,
        "run_id": run_id,
        "stats": stats,
        "upstream_error": stats.get("upstream_error"),
        "verdict": verdict,
        # 明确提示：HTTP 200 不代表发出去了
        "note": "以 stats.sent_new 为准；already_greeted / unconfirmed 都不算新发",
    }


def _verdict_of(
    halt_kind: str | None, sync_error: str | None, stats: dict[str, Any]
) -> str:
    """本轮的"结论码"，给 pacer 决定下一拍要不要等。

    - `risk|limit|auth|unconfirmed`：上游给出了明确信号，优先采用
    - `error`：有失败但没发出任何一条
    - `skipped`：没出错，只是没有可发的东西（额度用完 / 岗位都发过了）
    - `ok`：正常发出（至少一条）
    """
    if halt_kind:
        return halt_kind
    if sync_error:
        return "error"
    if int(stats.get("sent_new") or 0) > 0:
        return "ok"
    if int(stats.get("failed") or 0) > 0:
        return "error"
    return "skipped"


@router.post("/api/run/direct-greet")
def api_run_direct_greet(
    request: Request,
    db=Depends(get_db),
    cfg: dict[str, Any] = Depends(get_config),
) -> dict[str, Any]:
    """手动跑一轮（发到当日额度用完）。**HTTP 200 不等于发送成功**：看 `stats.sent_new`。

    真正"像人一样发"的入口是 `POST /api/pacing/start`（后台推进会按 90–240s 间隔
    一条一条发）。本接口保留给"我想立刻发一批"的手动场景，口径与改造前一致。
    """
    return run_direct_greet_pass(request, db, cfg)

"""Phase 2-B：`detail` 回填的纯逻辑（架构 §19 第 1 条：**零 IO、零 adapter import**）。

为什么这个文件必须存在，而不是把判断写在 `dashboard.sweep` 里：
`detail` 每岗一次请求，会和 `search` / `greet` / 反查 `chat` 抢上游那个
**20 次/分钟**的共享预算（补页 §D）。选谁抓、抓几条，是**规则**，不是编排，
所以按 §9 授权清单第 1 条单独成文件，且保持纯函数——可以脱开数据库与适配器单测。

三个纯函数（只这三个，§9 授权清单第 1 条）：

    needs_detail()     L0 该不该抓（§6.2 六条判据，按顺序判、命中即跳过）
    pick_for_detail()  L0 过滤 → 排序 → L1/L2 截断，产出 todo 与账目
    merge_detail()     把 detail 载荷合回 job（不改入参、不覆盖已有正文）

`enriched=0` 时必须有别的数字能解释它（§6.6）：因此 `pick_for_detail()` 的返回值
里 `prefiltered` / `skipped_by_quota` / `reasons` 是**一等公民**，不是日志。
"""

from __future__ import annotations

from typing import Any, Callable

#: 【§6.2 第 4 条】**必须**从 matcher 读这两个值，不许写死 32 / 60。
#: 刻意用模块引用（`matcher.PRACTICE_AREA_CAP`）而不是 `from ... import`：
#: 拷一份常量进本文件，以后改了 `PRACTICE_AREA_CAP` 预筛不会跟着变，而**单元测试
#: 也抓不到**（它比的是自己那份拷贝）。测试 `test_pick_skips_upper_bound_below_threshold`
#: 就是靠 monkeypatch `matcher.PRACTICE_AREA_CAP` 来守这一点的。
import app.core.matcher as matcher

# --------------------------------------------------------------------------- #
# L0 剪枝原因（字符串常量：`stats` 与 `runs.error` 里直接可用，不翻译）
# --------------------------------------------------------------------------- #
REASON_NO_CAPABILITY = "no_capability"
REASON_ALREADY_HAS_JD = "already_has_jd"
REASON_HARD_REJECT = "hard_reject"
REASON_UPPER_BOUND = "upper_bound"
REASON_CLAIMED_TODAY = "claimed_today"
REASON_ALREADY_GREETED = "already_greeted"

#: 判据顺序（§6.2 的表，**不许调序**）：命中即跳过，后面的判据不再看。
L0_ORDER: tuple[str, ...] = (
    REASON_NO_CAPABILITY,
    REASON_ALREADY_HAS_JD,
    REASON_HARD_REJECT,
    REASON_UPPER_BOUND,
    REASON_CLAIMED_TODAY,
    REASON_ALREADY_GREETED,
)


def _min_match_score(cfg: dict[str, Any]) -> float:
    """过线阈值。**从配置读**，取不到才退 `matcher.DEFAULT_MIN_MATCH_SCORE`。

    与 `matcher.should_greet()` 的取值口径保持一致（同样接受字符串数字、
    同样在坏值时回退默认），否则"预筛上界"和"最终过线"会用两把尺子。
    """
    threshold = (cfg or {}).get("min_match_score", matcher.DEFAULT_MIN_MATCH_SCORE)
    try:
        return float(threshold)
    except (TypeError, ValueError):
        return float(matcher.DEFAULT_MIN_MATCH_SCORE)


def _has_detail_capability(adapter: Any) -> bool:
    """L0-1 **双守卫**：`hasattr` + `in capabilities`。

    只判 `capabilities` 会炸：`FakeAdapter.capabilities` 是 `set(CAPABILITIES)`，
    **含 `"detail"` 却没有 `detail()` 方法**（`fake.py`），于是"有 detail 能力"
    但调用即 `AttributeError`。这条坑在 §14 第一条点名过。
    """
    if adapter is None:
        return False
    method = getattr(adapter, "detail", None)
    if not callable(method):
        return False
    capabilities = getattr(adapter, "capabilities", None) or set()
    return "detail" in capabilities


def needs_detail(
    job: dict[str, Any],
    adapter: Any,
    base_score: float,
    *,
    cfg: dict[str, Any] | None = None,
    is_claimed: Callable[[str], bool] | None = None,
    is_greeted: Callable[[str], bool] | None = None,
) -> tuple[bool, str | None]:
    """L0：**纯本地、零请求**地判断这个岗位该不该抓 detail。

    返回 `(True, None)` = 该抓；`(False, reason)` = 跳过，`reason` 取 `L0_ORDER` 里的值。
    顺序严格照 §6.2：能力 → 已有正文 → 硬淘汰 → 上界 → 当日已抓 → 已打招呼。
    """
    # 1) 没有 detail 能力（双守卫，见 _has_detail_capability）
    if not _has_detail_capability(adapter):
        return (False, REASON_NO_CAPABILITY)

    # 2) 已有正文：重复抓纯浪费一次预算
    if str(job.get("jd_text") or "").strip():
        return (False, REASON_ALREADY_HAS_JD)

    # 3) 预打分是硬淘汰（-999）：JD 只能**新增**淘汰（is_jd_blacklist）和扣分，
    #    救不回 -999。数学上不可能改变结论，抓了也是白花预算。
    if base_score is not None and float(base_score) <= matcher.HARD_REJECT_SCORE:
        return (False, REASON_HARD_REJECT)

    # 4) 上界贴线剪枝：base + PRACTICE_AREA_CAP 都不够线 → 满配也过不了。
    #    老实说只能剪掉 base < 28 的岗（无方向分 + 缺 2 个以上加分项），剪得不多，但零成本。
    upper_bound = float(base_score or 0.0) + float(matcher.PRACTICE_AREA_CAP)
    if upper_bound < _min_match_score(cfg or {}):
        return (False, REASON_UPPER_BOUND)

    job_id = str(job.get("job_id") or "")

    # 5) 当天该岗已抓过（actions 里已有 `detail:{job_id}:{YYYYMMDD}`）
    if is_claimed is not None and is_claimed(job_id):
        return (False, REASON_CLAIMED_TODAY)

    # 6) 已到终点（`greet:{job_id}` 在 actions 里），分数无意义
    if is_greeted is not None and is_greeted(job_id):
        return (False, REASON_ALREADY_GREETED)

    return (True, None)


def pick_for_detail(
    jobs: list[dict[str, Any]],
    base_scores: dict[str, float],
    adapter: Any,
    *,
    cfg: dict[str, Any] | None = None,
    batch_limit: int = 10,
    daily_remaining: int | None = None,
    is_claimed: Callable[[str], bool] | None = None,
    is_greeted: Callable[[str], bool] | None = None,
) -> dict[str, Any]:
    """L0 过滤 + 排序 + L1/L2 截断，产出本轮要抓的 `todo`（§5.1 第 4 步）。

    排序：**分数降序、同分保持传入顺序**（稳定）——`jobs` 是适配器返回的顺序，
    同分时先来的先抓，跑两轮的结果一样，便于复盘对照。

    截断：`batch_limit`（L1，单轮）与 `daily_remaining`（L2，当日剩余）取小；
    `daily_remaining=None` 表示不看 L2（纯逻辑层不关心今天用了几条）。

    返回 dict（**键全给，不省**，编排层直接进 `stats`）：

        todo                  list[str]  本轮要抓的 job_id，已排序、已截断
        prefiltered           int        L0 剪掉的条数（≠ 额度不够）
        skipped_by_quota      int        因 L1/L2 **额度**没抓的条数（剪枝不算）
        reasons               dict       {L0 reason: 条数}，排障用
        limited_by            str|None   "batch" / "daily" / None，说清是哪把刀切的
    """
    reasons: dict[str, int] = {}
    candidates: list[tuple[int, str, float]] = []

    for index, job in enumerate(jobs):
        job_id = str(job.get("job_id") or "")
        if not job_id:
            continue  # 无 ID 的条目（上游形状异常）不参与，交给 found/skipped_no_id 解释
        base_score = float(base_scores.get(job_id, 0.0) or 0.0)
        ok, reason = needs_detail(
            job,
            adapter,
            base_score,
            cfg=cfg,
            is_claimed=is_claimed,
            is_greeted=is_greeted,
        )
        if ok:
            candidates.append((index, job_id, base_score))
        else:
            reasons[reason or "unknown"] = reasons.get(reason or "unknown", 0) + 1

    prefiltered = sum(reasons.values())

    # 分数降序；同分用原顺序（index 升序）兜底 → 排序稳定
    candidates.sort(key=lambda item: (-item[2], item[0]))

    limit = max(int(batch_limit), 0)
    limited_by: str | None = None
    if daily_remaining is not None and int(daily_remaining) < limit:
        limit = max(int(daily_remaining), 0)
        limited_by = "daily"
    elif len(candidates) > limit:
        limited_by = "batch"

    todo = [job_id for _index, job_id, _score in candidates[:limit]]
    skipped_by_quota = len(candidates) - len(todo)
    if skipped_by_quota and limited_by is None:
        limited_by = "batch"

    return {
        "todo": todo,
        "prefiltered": prefiltered,
        "skipped_by_quota": skipped_by_quota,
        "reasons": reasons,
        "limited_by": limited_by,
    }


def merge_detail(job: dict[str, Any], payload: dict[str, Any] | None) -> dict[str, Any]:
    """把 `adapter.detail()` 的载荷合回 job。**纯函数：不改入参，返回新 dict。**

    两条铁律：

    1. **不覆盖已有正文**。`jd_text` 非空时原样保留——已有正文可能是更好的来源
       （人工补录 / 上一轮抓到的），用一次新抓的空串盖掉它是最难查的一种数据损坏。
    2. **抓不到就是没抓到**。`payload` 为 None / `ok=False` / `jd_text` 空串时
       **一个字都不改**：上游"成功但 `description` 为空"是合法结果（§6.6），
       它意味着"这岗没写 JD"，不是"抓到了一份空 JD"。

    `hr_active` 不做合并：那是列表项就有的字段，detail 的 `boss_active` 只是同一件事的
    另一种文案，两处都改会让 `hr_active` 的来源变成"最后一个写的人赢"。
    """
    merged = dict(job)
    if str(merged.get("jd_text") or "").strip():
        return merged
    if not payload or not payload.get("ok"):
        return merged
    text = str(payload.get("jd_text") or "")
    if text.strip():
        merged["jd_text"] = text
    return merged

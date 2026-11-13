"""岗位打分（任务书 §5.1 / §5.6，规则见架构 §7.1.1）。

**纯函数，不读文件**：词表全部来自 `merged_cfg`
（= matcher.yaml 全部内容 + config.yaml 的 target/quota 段，合并逻辑在 `app/config.py`）。

四层规则：
  1. 硬淘汰  employer → title → jd → salary_floor → license（命中即 out，返回 -999）
  2. 方向匹配  标题命中 enabled 方向 title_patterns +40；practice_areas 每词 +8 上限 32
  3. 加分项    白名单 +10 / 薪资重叠 +10 / hr_active +8 / 7 天内发布 +5 / 公司规模 +5
  4. 扣分      销售类标题 -30 / 无经验且薪资偏离 -20 / 合规风险字样 -40
"""

from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any

# 硬淘汰的分数哨兵值：能区分"分低"和"出局"
HARD_REJECT_SCORE: float = -999.0

# 低于此分不打招呼（真值从 merged_cfg["min_match_score"] 读）
DEFAULT_MIN_MATCH_SCORE: int = 60

# 计分常量（任务书 §5.8 的"计分口径"必须能手算复现，改这些数就会让 82/74/74/74 挂掉）
DIRECTION_POINTS = 40
PRACTICE_AREA_POINTS = 8
PRACTICE_AREA_CAP = 32
PREFERRED_EMPLOYER_POINTS = 10
SALARY_OVERLAP_POINTS = 10
HR_ACTIVE_POINTS = 8
FRESH_PUBLISH_POINTS = 5
PREFERRED_SCALE_POINTS = 5

# 扣分
SALES_TITLE_PENALTY = -30
NO_EXPERIENCE_PENALTY = -20
COMPLIANCE_RISK_PENALTY = -40

_HR_ACTIVE_BONUS = ("online", "today")
_SALES_TITLE_WORDS = ("销售", "顾问", "客服")
_COMPLIANCE_RISK_WORDS = ("勾结", "伪造", "规避")


# --------------------------------------------------------------------------- #
# 薪资解析（§5.6）
# --------------------------------------------------------------------------- #
_DAY_RATE_RE = re.compile(r"(元\s*/\s*天|/\s*天|日薪|元\s*/\s*日)")
_RANGE_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*([kKwW千万元]?)\s*(?:-|~|–|—|至|到)\s*(\d+(?:\.\d+)?)\s*([kKwW千万元]?)"
)
_ABOVE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*([kKwW千万元]?)\s*(?:以上|\+)")
_UNIT_MULTIPLIER = {"": 1, "元": 1, "k": 1000, "千": 1000, "w": 10000, "万": 10000}


def _to_yuan(number: str, unit: str) -> int | None:
    try:
        value = float(number)
    except (TypeError, ValueError):
        return None
    multiplier = _UNIT_MULTIPLIER.get((unit or "").lower(), None)
    if multiplier is None:
        return None
    return int(round(value * multiplier))


def parse_salary(text: str) -> tuple[int | None, int | None]:
    """把薪资格式化成 (月薪下限, 月薪上限)，单位元。(None, None) 表示无法解析。

    支持（§5.6 六种）：
        "5-8K" → (5000, 8000)      "15-30K·13薪" → (15000, 30000)
        "8千-1.2万" → (8000, 12000) "面议" → (None, None)
        "200元/天" → (None, None)   "1万以上" → (10000, None)

    **解析失败不淘汰**，只跳过薪资相关打分（面议岗位可能反而更好）。
    日结/时结一律 (None, None)：不能跟月薪直接比。
    """
    if not text:
        return (None, None)
    raw = str(text).strip()
    if not raw:
        return (None, None)

    # 日结/按天：不与月薪比较
    if _DAY_RATE_RE.search(raw):
        return (None, None)

    match = _RANGE_RE.search(raw)
    if match:
        low_num, low_unit, high_num, high_unit = match.groups()
        # "5-8K"：前一个数字没写单位时，继承后一个的单位
        if not low_unit and high_unit:
            low_unit = high_unit
        if not high_unit and low_unit:
            high_unit = low_unit
        low = _to_yuan(low_num, low_unit)
        high = _to_yuan(high_num, high_unit)
        if low is None and high is None:
            return (None, None)
        if low is not None and high is not None and low > high:
            low, high = high, low
        return (low, high)

    above = _ABOVE_RE.search(raw)
    if above:
        return (_to_yuan(above.group(1), above.group(2)), None)

    return (None, None)


# --------------------------------------------------------------------------- #
# 内部工具
# --------------------------------------------------------------------------- #
def _hits(text: str, words: Any) -> list[str]:
    """返回 words 里出现在 text 中的词（保序、去重）。"""
    if not text or not words:
        return []
    hit: list[str] = []
    for word in words:
        if word and str(word) in text and str(word) not in hit:
            hit.append(str(word))
    return hit


def _first_hit(text: str, words: Any) -> str | None:
    hit = _hits(text, words)
    return hit[0] if hit else None


def _is_fresh(publish_time: Any, days: int = 7) -> bool:
    """publish_time 是否在 days 天内。解析失败的当作"不新鲜"，不淘汰。"""
    if not publish_time:
        return False
    try:
        parsed = datetime.fromisoformat(str(publish_time).replace("Z", "+00:00"))
    except ValueError:
        return False
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return datetime.now(timezone.utc) - parsed <= timedelta(days=days)


def pick_direction(job: dict[str, Any], merged_cfg: dict[str, Any]) -> tuple[str | None, dict[str, Any] | None, str | None]:
    """按标题判定方向。只看 `enabled: true` 的档（`enabled: false` 等于不存在）。

    返回 (方向名, 方向配置, 命中的 title_pattern)。无命中返回 (None, None, None)。
    """
    title = str(job.get("title") or "")
    directions = merged_cfg.get("directions") or {}
    if not isinstance(directions, dict):
        return (None, None, None)
    for name, conf in directions.items():
        if not isinstance(conf, dict) or not conf.get("enabled"):
            continue
        pattern = _first_hit(title, conf.get("title_patterns") or [])
        if pattern:
            return (name, conf, pattern)
    return (None, None, None)


def _hard_reject(
    job: dict[str, Any],
    merged_cfg: dict[str, Any],
    direction_name: str | None,
    direction: dict[str, Any] | None,
) -> str | None:
    """硬淘汰层。**顺序固定**（R4-8）：employer → title → jd → salary_floor → license。

    命中即返回规则名，`match_reason` 取第一条命中的那条。
    """
    company = str(job.get("company") or "")
    title = str(job.get("title") or "")
    jd_text = str(job.get("jd_text") or "")

    preferred_employers = merged_cfg.get("preferred_employers") or []

    # ① 招聘方主体黑名单（白名单优先，可豁免）
    word = _first_hit(company, merged_cfg.get("is_employer_blacklist") or [])
    if word and not _hits(company, preferred_employers):
        return f"is_employer_blacklist({word})"

    # ② 标题黑名单
    word = _first_hit(title, merged_cfg.get("is_title_blacklist") or [])
    if word:
        return f"is_title_blacklist({word})"

    # ③ JD 黑名单
    word = _first_hit(jd_text, merged_cfg.get("is_jd_blacklist") or [])
    if word:
        return f"is_jd_blacklist({word})"

    # ④ 薪资下限（只看该方向自己的 salary_floor；方向没写才回落到全局）
    if direction and direction.get("require_salary_floor"):
        floor = direction.get("salary_floor")
        if floor is None:
            floor = merged_cfg.get("salary_floor")
        low, _ = parse_salary(job.get("salary"))
        if floor is not None and low is not None and low < int(floor):
            return f"require_salary_floor({direction_name}: {low} < {floor})"

    # ⑤ 持牌校验
    if direction and direction.get("require_license_check"):
        licensed = merged_cfg.get("licensed_employers") or []
        if not _hits(company, licensed):
            return f"require_license_check({direction_name}: 未命中持牌名单)"

    return None


# --------------------------------------------------------------------------- #
# 主函数
# --------------------------------------------------------------------------- #
def score(job: dict[str, Any], merged_cfg: dict[str, Any]) -> tuple[float, list[str]]:
    """返回 (分数, 理由列表)。理由用于日志和 LLM 素材。

    硬淘汰返回 `(-999, ["hard_reject: <规则名>"])`，不是 0 分——要能区分"分低"和"出局"。
    """
    reasons: list[str] = []
    title = str(job.get("title") or "")
    company = str(job.get("company") or "")
    jd_text = str(job.get("jd_text") or "")

    direction_name, direction, pattern = pick_direction(job, merged_cfg)

    # ---- 第一层：硬淘汰 ---------------------------------------------------- #
    reject_rule = _hard_reject(job, merged_cfg, direction_name, direction)
    if reject_rule:
        return (HARD_REJECT_SCORE, [f"hard_reject: {reject_rule}"])

    # ---- 第二层：方向匹配 -------------------------------------------------- #
    total = 0

    if direction_name and direction:
        total += DIRECTION_POINTS
        reasons.append(f"方向命中 +{DIRECTION_POINTS}：{direction_name}（标题含「{pattern}」）")
    else:
        directions = merged_cfg.get("directions") or {}
        disabled = [
            name for name, conf in directions.items()
            if isinstance(conf, dict) and not conf.get("enabled")
            and _hits(title, conf.get("title_patterns") or [])
        ]
        if disabled:
            reasons.append(
                f"方向未启用：标题只命中 {disabled[0]}，该方向 enabled: false，不计方向分"
            )
        else:
            reasons.append("方向未命中：标题不含任何 enabled 方向的 title_patterns，不计方向分")

    areas = _hits(jd_text, merged_cfg.get("practice_areas") or [])
    if areas:
        area_points = min(len(areas) * PRACTICE_AREA_POINTS, PRACTICE_AREA_CAP)
        total += area_points
        reasons.append(f"执业领域命中 +{area_points}：{'、'.join(areas)}")

    # ---- 第三层：加分项 ---------------------------------------------------- #
    if _hits(company, merged_cfg.get("preferred_employers") or []):
        total += PREFERRED_EMPLOYER_POINTS
        reasons.append(f"白名单雇主 +{PREFERRED_EMPLOYER_POINTS}")

    low, high = parse_salary(job.get("salary"))
    expected = merged_cfg.get("expected_salary") or {}
    exp_low, exp_high = expected.get("low"), expected.get("high")
    if low is not None and exp_low is not None:
        effective_high = high if high is not None else float("inf")
        if max(low, int(exp_low)) <= min(effective_high, int(exp_high or effective_high)):
            total += SALARY_OVERLAP_POINTS
            reasons.append(
                f"薪资区间重叠 +{SALARY_OVERLAP_POINTS}：{job.get('salary')} ∩ {exp_low}-{exp_high}"
            )
    else:
        reasons.append("薪资无法解析，跳过薪资相关打分（不淘汰）")

    hr_active = str(job.get("hr_active") or "").lower()
    if hr_active in _HR_ACTIVE_BONUS:
        total += HR_ACTIVE_POINTS
        reasons.append(f"HR {hr_active} +{HR_ACTIVE_POINTS}")

    if _is_fresh(job.get("publish_time")):
        total += FRESH_PUBLISH_POINTS
        reasons.append(f"7 天内发布 +{FRESH_PUBLISH_POINTS}")

    preferred_scales = merged_cfg.get("preferred_scales") or []
    if job.get("company_scale") and str(job.get("company_scale")) in preferred_scales:
        total += PREFERRED_SCALE_POINTS
        reasons.append(f"公司规模命中 preferred_scales +{PREFERRED_SCALE_POINTS}")

    # ---- 第四层：扣分 ------------------------------------------------------ #
    if not _hits(title, ("法务",)) and _hits(title, _SALES_TITLE_WORDS):
        total += SALES_TITLE_PENALTY
        reasons.append(f"销售类标题扣分 {SALES_TITLE_PENALTY}")

    if "无经验" in jd_text and low is not None and exp_low is not None and low < int(exp_low):
        total += NO_EXPERIENCE_PENALTY
        reasons.append(f"接受无经验且薪资低于期望 {NO_EXPERIENCE_PENALTY}")

    risk_words = _hits(jd_text, _COMPLIANCE_RISK_WORDS)
    if risk_words:
        total += COMPLIANCE_RISK_PENALTY
        reasons.append(f"合规风险字样（{'、'.join(risk_words)}）扣分 {COMPLIANCE_RISK_PENALTY}")

    if not reasons:
        reasons.append("无任何匹配项")

    return (float(total), reasons)


def should_greet(value: float, merged_cfg: dict[str, Any]) -> bool:
    """是否过线。阈值从配置读，硬淘汰永远是 False。"""
    threshold = merged_cfg.get("min_match_score", DEFAULT_MIN_MATCH_SCORE)
    try:
        threshold_num = float(threshold)
    except (TypeError, ValueError):
        threshold_num = float(DEFAULT_MIN_MATCH_SCORE)
    return value > HARD_REJECT_SCORE and value >= threshold_num


# --------------------------------------------------------------------------- #
# gap_flags（P2-A 决策 1 / 任务书 §3.3 Step 6）
#
# **纯提示，不参与打分**：`score()` 一行未动，82/74/74/74 不受影响。
# 规则宁可少报、不可误报：每条都必须能在 JD/标题里指认出匹配到的词（见各分支注释）。
# --------------------------------------------------------------------------- #

#: 持证类关键词。命中即提示"要求持证"（用户无执业证，这是硬门槛）。
_GAP_LICENSE_WORDS = ("律师执业证", "执业证", "法律职业资格证", "资格证", "A证")

#: 独立办案类关键词。
_GAP_INDEPENDENT_WORDS = ("能独立开庭", "独立开庭", "独立办案", "能独立办案")

#: 优先项关键词 → 提示文案。
_GAP_PREFERENCE_WORDS: tuple[tuple[str, str], ...] = (
    ("党员优先", "优先项：党员"),
    ("硕士优先", "优先项：硕士"),
)

#: 经验年限识别：阿拉伯数字 + 常见中文数字。
_EXP_CN = {"一": 1, "两": 2, "二": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9, "十": 10}

#: "3年以上" / "三年以上" / "5 年经验" / "3-5年" 的"下限"那一段。
_GAP_EXP_RE = re.compile(r"(\d+|[一两二三四五六七八九十])\s*年\s*(?:及)?以上")
_GAP_EXP_RANGE_RE = re.compile(r"(\d+|[一两二三四五六七八九十])\s*[-~至到]\s*(\d+|[一两二三四五六七八九十])\s*年")


def _to_int(token: str) -> int | None:
    if token.isdigit():
        return int(token)
    return _EXP_CN.get(token)


def _extract_experience_years(text: str) -> int | None:
    """从 JD 里抽"要求几年经验"的下限。抽不出返回 None（**不猜**）。"""
    match = _GAP_EXP_RE.search(text)
    if match:
        return _to_int(match.group(1))
    match = _GAP_EXP_RANGE_RE.search(text)
    if match:
        return _to_int(match.group(1))
    return None


def compute_gap_flags(job: dict[str, Any], cfg: dict[str, Any]) -> list[str]:
    """`profile_mode=demo` 用：**不淘汰，只提示**。返回如 `["要求持证", "要求3年经验"]`。

    每条 flag 都对应 JD/标题里一个可指认的词（见分支注释），**不允许靠猜**。
    返回顺序固定：持证 → 经验 → 独立办案 → 优先项，便于测试与展示稳定。
    """
    title = str(job.get("title") or "")
    jd_text = str(job.get("jd_text") or "")
    haystack = f"{title}\n{jd_text}"

    flags: list[str] = []

    # ① 要求持证：JD/标题出现"律师执业证 / 执业证 / 法律职业资格证 / 资格证 / A证"
    if _hits(haystack, _GAP_LICENSE_WORDS):
        flags.append("要求持证")

    # ② 要求 N 年经验：需 N > my_experience_years。
    #    `my_experience_years` 为 null（用户没有年限）按 0 处理，任何 N≥1 都算缺口。
    required = _extract_experience_years(haystack)
    if required:
        mine = cfg.get("my_experience_years")
        try:
            mine_num = int(mine) if mine is not None else 0
        except (TypeError, ValueError):
            mine_num = 0
        if required > mine_num:
            flags.append(f"要求{required}年经验")

    # ③ 要求独立办案：JD/标题出现"能独立开庭 / 独立开庭 / 独立办案 / 能独立办案"
    if _hits(haystack, _GAP_INDEPENDENT_WORDS):
        flags.append("要求独立办案")

    # ④ 优先项：只认"党员优先 / 硕士优先"这两个明确写法，不做同义扩展
    for word, flag in _GAP_PREFERENCE_WORDS:
        if word in haystack:
            flags.append(flag)

    return flags

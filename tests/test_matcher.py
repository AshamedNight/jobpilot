"""岗位打分（任务书 §5.1 / §5.6 / §5.8）—— 9 条。

断言必须落到**具体数值与具体行为**：分数精确等于 82 / 74 / 74 / 74，
不接受"只要 ≥60 就行"这种测不出行为的写法。

岗位数据一律从 `app.adapters.fake.JOBS` 取（剧本是唯一真源，别在测试里另抄一份）。
"""

from __future__ import annotations

import pytest

from app.adapters.fake import JOBS
from app.config import load_config
from app.core.matcher import (
    HARD_REJECT_SCORE,
    compute_gap_flags,
    parse_salary,
    score,
    should_greet,
)


@pytest.fixture(scope="module")
def cfg() -> dict:
    """仓库里的真实合并配置（matcher.yaml + config.yaml 的 target/quota）。"""
    return load_config()


def job_of(job_id: str, **overrides) -> dict:
    """取剧本里的岗位副本，可覆盖字段（覆盖时不动原始 JOBS）。"""
    job = dict(next(j for j in JOBS if j["job_id"] == job_id))
    job.update(overrides)
    return job


# --------------------------------------------------------------------------- #
# 硬淘汰三条
# --------------------------------------------------------------------------- #
def test_employer_blacklist_is_hard_reject(cfg):
    """① 招聘方黑名单 → 硬淘汰（用岗位 #4：某信息咨询有限公司）。"""
    value, reasons = score(job_of("job_004"), cfg)

    assert value == HARD_REJECT_SCORE == -999
    assert len(reasons) == 1
    assert reasons[0] == "hard_reject: is_employer_blacklist(信息咨询)"
    assert should_greet(value, cfg) is False


def test_jd_blacklist_waifang_is_hard_reject(cfg):
    """② JD 含"外访" → 硬淘汰（岗位 #5）。

    同时验证 R4-8 的顺序：`#5` 的雇主也不在持牌名单里，但
    `is_jd_blacklist` 在 `require_license_check` **之前**判，所以理由必须是前者。
    """
    value, reasons = score(job_of("job_005"), cfg)

    assert value == -999
    assert "is_jd_blacklist" in reasons[0]
    assert "外访" in reasons[0]
    # 顺序证据：不能报成持牌校验
    assert "require_license_check" not in reasons[0]


def test_salary_floor_below_threshold_is_hard_reject(cfg):
    """③ 薪资下限低于该方向的 salary_floor → 硬淘汰（岗位 #7：实习法务 2-3K）。"""
    value, reasons = score(job_of("job_007"), cfg)

    assert value == -999
    assert "require_salary_floor" in reasons[0]
    # 2-3K 解析为 2000，enterprise_legal.salary_floor 是 4000
    assert "2000" in reasons[0] and "4000" in reasons[0]


# --------------------------------------------------------------------------- #
# 正常与边界
# --------------------------------------------------------------------------- #
def test_enterprise_legal_passes_threshold(cfg):
    """④ 正常企业法务 → 过线（岗位 #1，≥60 且精确 82）。"""
    value, _ = score(job_of("job_001"), cfg)

    assert value == 82
    assert value >= cfg["min_match_score"]
    assert should_greet(value, cfg) is True


def test_empty_jd_does_not_crash_and_scores_low(cfg):
    """⑤ 空 JD → 不崩，返回低分（拿不到 practice_areas 分，过不了 60）。"""
    value, reasons = score(job_of("job_001", jd_text=""), cfg)

    assert isinstance(value, float)
    assert value == 50          # 方向 +40 + 薪资重叠 +10，无领域词
    assert value < cfg["min_match_score"]
    assert reasons                      # 仍然给出理由，不是空列表


def test_reasons_are_non_empty_and_readable(cfg):
    """⑥ 理由列表非空且可读：每一条都是非空字符串，且说人话（带中文）。"""
    _, reasons = score(job_of("job_001"), cfg)

    assert isinstance(reasons, list) and reasons
    for reason in reasons:
        assert isinstance(reason, str)
        assert reason.strip()
        assert any("\u4e00" <= ch <= "\u9fff" for ch in reason), f"理由里应有中文：{reason}"
    # 三条加分理由都要出现，方便人复盘为什么是 82
    joined = " | ".join(reasons)
    assert "方向命中" in joined
    assert "执业领域命中" in joined
    assert "薪资区间重叠" in joined


def test_parse_salary_six_formats(cfg):
    """⑦ `parse_salary` 六种格式（任务书 §5.6 表格，逐个断言）。

    用表驱动而不是六个函数：本批要求 matcher 恰好 9 条，撑住总数 36。
    """
    table = [
        ("5-8K", (5000, 8000)),
        ("15-30K·13薪", (15000, 30000)),
        ("8千-1.2万", (8000, 12000)),
        ("面议", (None, None)),
        ("200元/天", (None, None)),
        ("1万以上", (10000, None)),
    ]
    for text, expected in table:
        assert parse_salary(text) == expected, f"{text!r} 应解析为 {expected}"

    # 解析失败【不淘汰】，只跳过薪资打分
    value, reasons = score(job_of("job_006"), cfg)
    assert value >= HARD_REJECT_SCORE
    assert any("薪资无法解析" in r for r in reasons)


def test_disabled_direction_scores_zero_not_hard_reject(cfg):
    """⑧ `enabled: false` 的方向（partner_track）→ 低分 0，**不是** hard_reject。

    岗位 #6「授薪合伙人」：标题只命中 partner_track（已关），JD 无任何 practice_areas 词。
    这一条守的是"分低淘汰"和"硬淘汰"的区分，写错会把两类淘汰混成一种。
    """
    value, reasons = score(job_of("job_006"), cfg)

    assert value == 0
    assert value != HARD_REJECT_SCORE
    assert should_greet(value, cfg) is False
    joined = " | ".join(reasons)
    assert "方向未启用" in joined
    assert "partner_track" in joined
    assert not any(r.startswith("hard_reject") for r in reasons)


def test_four_passing_jobs_exact_scores(cfg):
    """⑨ 四个过线岗位的分数精确等于 82 / 74 / 74 / 74（§5.8 计分口径）。"""
    expected = {"job_001": 82, "job_002": 74, "job_003": 74, "job_008": 74}

    actual = {job_id: score(job_of(job_id), cfg)[0] for job_id in expected}
    assert actual == expected

    # 四个都必须过线，且理由非空
    for job_id in expected:
        value, reasons = score(job_of(job_id), cfg)
        assert value >= 60
        assert reasons


# --------------------------------------------------------------------------- #
# gap_flags（P2-A 决策 1）—— 3 条
#
# **只提示、不参与打分**：上面 9 条断言 82/74/74/74，一条都不许因为本功能改动。
# --------------------------------------------------------------------------- #
def test_gap_flags_license_required(cfg):
    """JD 命中"律师执业证 / 资格证 / A证" → `要求持证`（必须能在 JD 里指出出处）。"""
    job = job_of("job_001", jd_text="负责合同审核；须持律师执业证，能独立开庭")
    flags = compute_gap_flags(job, cfg)

    assert "要求持证" in flags
    # 不误报：同一 JD 里的其它词不额外生 flag
    assert flags.count("要求持证") == 1


def test_gap_flags_experience_years(cfg):
    """命中"N年以上"且 N > my_experience_years → `要求N年经验`（中文数字也认）。"""
    # 默认 cfg 里 my_experience_years 是 null（用户无年限），按 0 处理
    job = job_of("job_001", jd_text="要求3年以上企业法务经验，负责合同审核")
    assert "要求3年经验" in compute_gap_flags(job, cfg)

    # 中文数字
    job_cn = job_of("job_001", jd_text="五年以上相关经验")
    assert "要求5年经验" in compute_gap_flags(job_cn, cfg)

    # 年限不够时不提示（N <= my_experience_years）
    rich_cfg = dict(cfg, my_experience_years=10)
    assert "要求3年经验" not in compute_gap_flags(job, rich_cfg)


def test_gap_flags_empty_when_no_gap(cfg):
    """无缺口 → `[]`；且 gap_flags **不参与打分**（82/74/74/74 不变）。"""
    job = job_of("job_001", jd_text="负责合同审核与合规风控，团队氛围好")
    assert compute_gap_flags(job, cfg) == []

    # 打分与是否有 gap_flags 无关：同一岗位给了缺口 JD 也不影响既定分数
    expected = {"job_001": 82, "job_002": 74, "job_003": 74, "job_008": 74}
    actual = {job_id: score(job_of(job_id), cfg)[0] for job_id in expected}
    assert actual == expected

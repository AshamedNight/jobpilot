"""配置合并 + 三条校验（任务书 §7 / §8，R4-7 新增，下限 3 条）。

三条校验：
  1. daily_greet > daily_greet_max  → StartupError（中文），启动即退出
  2. share 之和 != 1.0             → 归一化 + warning，**不崩**
  3. .env 缺失                     → **降级运行**，llm_configured=False，不抛
"""

from __future__ import annotations

import copy
import re

import pytest
import yaml

from app import config as appconfig
from app.config import (
    DEFAULT_CONFIG_PATH,
    DEFAULT_MATCHER_PATH,
    StartupError,
    load_config,
    normalize_direction_shares,
    read_yaml,
)


def _write_config_yaml(tmp_path, daily_greet=5, daily_greet_max=50, extra_target=None):
    """造一个最小的 config.yaml（自建，不依赖仓库文件）。"""
    data = {
        "adapter": "fake",
        "target": {
            "cities": ["淄博"],
            "cities_nearby": ["济南", "潍坊"],
            "cities_nearby_enabled": True,
            "allow_remote": True,
            "exclude_title_words": ["兼职", "外包", "驻场"],
            "profile_mode": "demo",
            "my_experience_years": None,
        },
        "quota": {
            "daily_greet": daily_greet,
            "daily_greet_max": daily_greet_max,
            "daily_greet_live_cap": 30,
            "daily_reply": 50,
            "daily_exchange": 10,
        },
    }
    if extra_target:
        data["target"].update(extra_target)
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def test_daily_greet_over_max_raises_startup_error(tmp_path):
    """校验 1：daily_greet > daily_greet_max → StartupError，且信息是中文。"""
    cfg_path = _write_config_yaml(tmp_path, daily_greet=100, daily_greet_max=50)

    with pytest.raises(StartupError) as excinfo:
        load_config(config_path=cfg_path, matcher_path=DEFAULT_MATCHER_PATH, env_path=tmp_path / "no.env")

    message = str(excinfo.value)
    assert "daily_greet" in message
    assert "daily_greet_max" in message
    # 必须是中文信息，不能是英文异常原文
    assert re.search(r"[\u4e00-\u9fff]", message), f"错误信息应为中文：{message}"


def test_share_sum_not_one_is_normalized_with_warning():
    """校验 2：share 合计 1.1 → 归一化到 1.0 + warning，不抛异常。

    用**内联 dict** 构造 1.1，不读仓库 yaml（N7）。
    """
    # 0.6 + 0.2 + 0.1 + 0.2 = 1.1（N7 里那个"故意不合法"的值）
    matcher_cfg = {
        "directions": {
            "enterprise_legal": {"enabled": True, "share": 0.6},
            "debt_resolution": {"enabled": True, "share": 0.2},
            "partner_track": {"enabled": False, "share": 0.1},
            "law_firm_junior": {"enabled": True, "share": 0.2},
        }
    }
    original = copy.deepcopy(matcher_cfg)

    normalized, warnings = normalize_direction_shares(matcher_cfg)

    shares = {name: conf["share"] for name, conf in normalized["directions"].items()}
    # 归一化后全部方向合计 1.0
    assert abs(sum(shares.values()) - 1.0) < 1e-6
    # 按原比例缩放：0.6/1.1、0.2/1.1、0.1/1.1、0.2/1.1
    assert abs(shares["enterprise_legal"] - 0.6 / 1.1) < 1e-5
    assert abs(shares["debt_resolution"] - 0.2 / 1.1) < 1e-5
    assert abs(shares["partner_track"] - 0.1 / 1.1) < 1e-5
    assert abs(shares["law_firm_junior"] - 0.2 / 1.1) < 1e-5
    assert warnings, "归一化必须打 warning"
    assert "1.1" in warnings[0]
    # 纯函数：不改入参
    assert matcher_cfg == original


def test_conflicting_key_raises_startup_error(tmp_path):
    """同名键冲突（matcher.yaml 与 config.yaml 都有）→ StartupError，不静默覆盖。"""
    # salary_floor 只允许定义在 config/matcher.yaml；这里故意在 target 段再写一次
    cfg_path = _write_config_yaml(tmp_path, extra_target={"salary_floor": 5000})

    with pytest.raises(StartupError) as excinfo:
        load_config(config_path=cfg_path, matcher_path=DEFAULT_MATCHER_PATH, env_path=tmp_path / "no.env")

    message = str(excinfo.value)
    assert "salary_floor" in message
    assert "冲突" in message
    assert re.search(r"[\u4e00-\u9fff]", message)


def test_missing_env_degrades_instead_of_crashing(tmp_path, monkeypatch):
    """校验 3：.env 不存在不抛异常，llm_configured=False；有 Key 则为 True。

    【F8】`load_config()` 现在按「`.env` 优先、系统环境变量兜底」取 Key，所以必须用
    monkeypatch 清掉机器上的 `LLM_API_KEY`，否则跑测试那台机器的环境变量会污染断言。
    """
    cfg_path = _write_config_yaml(tmp_path)
    missing_env = tmp_path / "does-not-exist.env"
    monkeypatch.delenv("LLM_API_KEY", raising=False)

    merged = load_config(config_path=cfg_path, matcher_path=DEFAULT_MATCHER_PATH, env_path=missing_env)

    assert merged["llm_configured"] is False
    assert any("降级" in w for w in merged["warnings"])
    # 合并本身是成功的：matcher 与 target 的键都在
    assert merged["salary_floor"] == 4000
    assert merged["cities"] == ["淄博"]
    assert merged["quota"]["daily_greet"] == 5

    # 有 .env 且填了 Key → llm_configured 变 True
    env_file = tmp_path / ".env"
    env_file.write_text("LLM_API_KEY=sk-test-not-a-real-key\n", encoding="utf-8")
    merged2 = load_config(config_path=cfg_path, matcher_path=DEFAULT_MATCHER_PATH, env_path=env_file)
    assert merged2["llm_configured"] is True
    assert not any("降级" in w for w in merged2["warnings"])

    # .env 缺失、只有系统环境变量 → 兜底生效（F8 选了 (a)）
    monkeypatch.setenv("LLM_API_KEY", "sk-from-environ")
    merged3 = load_config(config_path=cfg_path, matcher_path=DEFAULT_MATCHER_PATH, env_path=missing_env)
    assert merged3["llm_configured"] is True


def test_repo_matcher_yaml_shares_sum_to_one():
    """仓库里的 matcher.yaml 必须是**合法**值：enabled 方向 share 合计 == 1.0。

    N7 的约定：非法值（1.1）只出现在测试的内联 dict 里，不能出现在仓库配置里。
    """
    matcher_cfg = read_yaml(DEFAULT_MATCHER_PATH)
    directions = matcher_cfg["directions"]

    # 断言 1：全部方向合计 == 1.0
    total = sum(float(conf["share"]) for conf in directions.values())
    assert abs(total - 1.0) < 1e-9, f"仓库 matcher.yaml 的 share 合计是 {total}，应为 1.0"

    # 断言 2：enabled 方向合计 == 1.0（R4-12 裁定后的口径）
    enabled_total = sum(
        float(conf["share"]) for conf in directions.values() if conf.get("enabled")
    )
    assert abs(enabled_total - 1.0) < 1e-9, f"enabled 方向 share 合计是 {enabled_total}，应为 1.0"

    # 断言 3：每个 enabled: false 的方向 share 必须为 0.0（R4-12 的不变量）
    for name, conf in directions.items():
        if not conf.get("enabled"):
            assert float(conf["share"]) == 0.0, (
                f"方向 {name} 是 enabled: false，share 必须为 0.0，当前是 {conf['share']}"
            )

    # 归一化函数对合法值应当原地不动
    normalized, warnings = normalize_direction_shares(matcher_cfg)
    assert warnings == []
    assert normalized["directions"] == directions

    # 逻辑批要用到的键必须都在，否则 matcher 一跑就 KeyError
    for key in ("min_match_score", "salary_floor", "practice_areas", "expected_salary",
                "preferred_employers", "preferred_scales", "licensed_employers",
                "is_employer_blacklist", "is_title_blacklist", "is_jd_blacklist"):
        assert key in matcher_cfg, f"matcher.yaml 缺少 {key}"
    assert matcher_cfg["preferred_scales"] == []          # R4-11

    # 【F4】exclude_title_words 只在 config.yaml 的 target 段（R4-3），且不得含"实习"。
    # 原来写 matcher_cfg.get("exclude_title_words", []) 是**恒真断言**——这个键在
    # matcher.yaml 里根本不存在，.get() 永远返回 []，测不出任何东西。
    config_cfg = read_yaml(DEFAULT_CONFIG_PATH)
    exclude_words = config_cfg["target"]["exclude_title_words"]
    assert "实习" not in exclude_words
    assert set(exclude_words) == {"兼职", "外包", "驻场"}


# --------------------------------------------------------------------------- #
# 【P2-B §11 第 13–14 条】detail 两档额度的配置校验
# --------------------------------------------------------------------------- #
def _write_detail_config_yaml(tmp_path, **quota_overrides):
    """自建最小 config.yaml（**不动上面那个 helper**，helper 属于既有用例）。"""
    quota = {
        "daily_greet": 3,
        "daily_greet_max": 50,
        "daily_greet_live_cap": 30,
        "daily_reply": 50,
        "daily_exchange": 10,
        "daily_detail": 50,
        "detail_batch": 10,
    }
    quota.update(quota_overrides)
    data = {
        "adapter": "fake",
        "target": {
            "cities": ["淄博"],
            "cities_nearby": ["济南", "潍坊"],
            "cities_nearby_enabled": True,
            "allow_remote": True,
            "exclude_title_words": ["兼职", "外包", "驻场"],
            "profile_mode": "demo",
            "my_experience_years": None,
        },
        "quota": quota,
    }
    path = tmp_path / "config.yaml"
    path.write_text(yaml.safe_dump(data, allow_unicode=True, sort_keys=False), encoding="utf-8")
    return path


def test_detail_batch_must_be_int_nonneg(tmp_path):
    """`detail_batch` 不是整数 / 是负数 → `StartupError`，且信息是中文。"""
    for bad in ("十条", 2.5, [10]):
        cfg_path = _write_detail_config_yaml(tmp_path, detail_batch=bad)
        with pytest.raises(StartupError) as excinfo:
            load_config(
                config_path=cfg_path,
                matcher_path=DEFAULT_MATCHER_PATH,
                env_path=tmp_path / "no.env",
            )
        message = str(excinfo.value)
        assert "detail_batch" in message
        assert re.search(r"[\u4e00-\u9fff]", message), f"错误信息应为中文：{message}"

    cfg_path = _write_detail_config_yaml(tmp_path, detail_batch=-1)
    with pytest.raises(StartupError) as excinfo:
        load_config(
            config_path=cfg_path, matcher_path=DEFAULT_MATCHER_PATH, env_path=tmp_path / "no.env"
        )
    assert "不能是负数" in str(excinfo.value)

    # daily_detail 同款校验（同一把尺子，别只守一半）
    cfg_path = _write_detail_config_yaml(tmp_path, daily_detail=-5)
    with pytest.raises(StartupError) as excinfo:
        load_config(
            config_path=cfg_path, matcher_path=DEFAULT_MATCHER_PATH, env_path=tmp_path / "no.env"
        )
    assert "daily_detail" in str(excinfo.value)


def test_detail_batch_cannot_exceed_20_or_daily(tmp_path):
    """`detail_batch > 20`（硬上限）/ `> daily_detail`（日额度）→ `StartupError`。

    合法值必须能过：`detail_batch=10` + `daily_detail=50` 是实验期口径。
    """
    ok_path = _write_detail_config_yaml(tmp_path)
    merged = load_config(
        config_path=ok_path, matcher_path=DEFAULT_MATCHER_PATH, env_path=tmp_path / "no.env"
    )
    assert merged["quota"]["detail_batch"] == 10
    assert merged["quota"]["daily_detail"] == 50

    # 硬上限：21 > 20
    hard_path = _write_detail_config_yaml(tmp_path, detail_batch=21, daily_detail=50)
    with pytest.raises(StartupError) as excinfo:
        load_config(
            config_path=hard_path, matcher_path=DEFAULT_MATCHER_PATH, env_path=tmp_path / "no.env"
        )
    message = str(excinfo.value)
    assert "20" in message
    assert re.search(r"[\u4e00-\u9fff]", message)

    # 恰好 20 是允许的（边界值不该被误杀）
    edge_path = _write_detail_config_yaml(tmp_path, detail_batch=20, daily_detail=20)
    merged_edge = load_config(
        config_path=edge_path, matcher_path=DEFAULT_MATCHER_PATH, env_path=tmp_path / "no.env"
    )
    assert merged_edge["quota"]["detail_batch"] == 20

    # 单轮 > 单日：日额度那一档保险就是坏的
    daily_path = _write_detail_config_yaml(tmp_path, detail_batch=10, daily_detail=5)
    with pytest.raises(StartupError) as excinfo:
        load_config(
            config_path=daily_path, matcher_path=DEFAULT_MATCHER_PATH, env_path=tmp_path / "no.env"
        )
    assert "daily_detail" in str(excinfo.value)

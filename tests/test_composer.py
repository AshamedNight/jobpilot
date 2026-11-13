"""话术生成 + FakeLLM（任务书 §5.5）—— 3 条。

prompt 正文必须在 `config/prompts/opener.md` 里，缺失抛 `ConfigError`，不许静默降级。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core import composer as composer_module
from app.core.composer import (
    OPENER_MAX_CHARS,
    ConfigError,
    FakeLLM,
    compose_opener,
    read_prompt,
)

JOB = {
    "job_id": "job_001",
    "title": "法务专员",
    "company": "某制造企业",
    "salary": "5-8K",
    "city": "淄博",
    "jd_text": "负责合同审核、合规检查与风控台账",
}
RESUME = "法学本科，两年企业合同审核经验"


def test_fake_llm_returns_text_and_never_touches_network():
    """① FakeLLM 能返回文本，不联网、不花钱。"""
    llm = FakeLLM()

    output = llm.complete("随便一段 prompt")

    assert isinstance(output, str) and output.strip()
    assert llm.call_count == 1

    # 离线证据：实现里没有任何网络/SDK 依赖
    source = Path(composer_module.__file__).read_text(encoding="utf-8")
    for forbidden in ("httpx", "requests", "openai", "urllib", "socket"):
        assert forbidden not in source, f"composer 不许引 {forbidden}"

    # 走完整链路也能拿到话术，且不是空串
    opener = compose_opener(JOB, RESUME, FakeLLM())
    assert opener.strip()
    assert len(opener) <= OPENER_MAX_CHARS


def test_opener_is_truncated_to_60_chars():
    """② 超过 60 字自动截断。"""
    too_long = "法" * 200
    opener = compose_opener(JOB, RESUME, FakeLLM(text=too_long))

    assert len(opener) == OPENER_MAX_CHARS == 60
    assert opener == too_long[:60]

    # 边界：正好 60 字不应被截
    exact = "测" * 60
    assert compose_opener(JOB, RESUME, FakeLLM(text=exact)) == exact


def test_prompt_template_holds_no_fabrication_rule(tmp_path):
    """③ prompt 模板里含"禁止出现工作年限/案件数量/执业证号"这段约束。

    同时验证"缺模板必须抛 ConfigError"，两条都属于 §5.5 的硬性约束，
    合在一个测试里是为了守住本批 3 条的条数。
    """
    template = read_prompt("opener.md")

    assert "禁止出现" in template
    for word in ("工作年限", "案件数量", "执业证号", "胜诉率", "客户名称"):
        assert word in template, f"opener.md 缺少约束词：{word}"
    assert "只陈述简历中明确写出的事实" in template

    # 渲染时这些占位符真的被填进去了（证明是读文件，不是硬编码话术）
    llm = FakeLLM()
    compose_opener(JOB, RESUME, llm)
    assert "法务专员" in llm.calls[0]
    assert "某制造企业" in llm.calls[0]

    # 模板缺失 → ConfigError，且信息是中文，不许静默用内置默认值
    empty_dir = tmp_path / "prompts"
    empty_dir.mkdir()
    with pytest.raises(ConfigError) as excinfo:
        compose_opener(JOB, RESUME, FakeLLM(), prompt_dir=empty_dir)
    assert "opener.md" in str(excinfo.value)
    assert any("\u4e00" <= ch <= "\u9fff" for ch in str(excinfo.value))

    # 模板为空同理
    (empty_dir / "opener.md").write_text("   \n", encoding="utf-8")
    with pytest.raises(ConfigError):
        compose_opener(JOB, RESUME, FakeLLM(), prompt_dir=empty_dir)

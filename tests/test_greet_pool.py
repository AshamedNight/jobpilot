"""招呼文案池（`app/core/greet_pool.py`）—— 闭环 1 的第一道拆解。

这一组守的是三件事：

1. **池子真的够大**（≥20 条，且仓库里那份 yaml 能过全部校验）——用真实资产文件测，
   因为"文案池有没有内容"本身就是被测对象，不是配置漂移。
2. **同一个岗位永远同一句**（sha256 定位，不随机）——重试换文案会让排障无法复现，
   也会让"同一岗位发了两句"变成更像机器的行为。
3. **坏的池子起不来**——少于 20 条、超长、编造资历，全部 `StartupError`，不许静默降级。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from app.core.greet_pool import (
    DEFAULT_MAX_CHARS,
    MIN_TEMPLATES,
    GreetPool,
    GreetPoolError,
    GreetTemplate,
    load_pool,
)

TEMPLATES_PATH = Path(__file__).resolve().parent.parent / "config" / "prompts" / "greet_templates.yaml"


def _job(job_id: str = "job_001", title: str = "法务专员", company: str = "山东某某科技有限公司") -> dict:
    return {"job_id": job_id, "title": title, "company": company}


def _make_pool(count: int = MIN_TEMPLATES) -> GreetPool:
    """造一个刚好 count 条的池子（不依赖仓库文件，用于边界测试）。"""
    items = [
        GreetTemplate(id=f"t{i:02d}", match_words=(), text=f"您好，看到贵司在招{{title}}，想了解下第{i}个问题。")
        for i in range(count)
    ]
    return GreetPool(items)


# --------------------------------------------------------------------------- #
# 1. 仓库资产本身
# --------------------------------------------------------------------------- #
def test_repo_pool_loads_and_is_big_enough():
    """仓库里的文案池必须能装出来，且不少于下限。"""
    pool = GreetPool.from_file(TEMPLATES_PATH)
    assert pool is not None
    assert len(pool) >= MIN_TEMPLATES
    assert len(pool.template_ids) == len(set(pool.template_ids)), "模板 id 不该重复"


def test_every_template_renders_within_limit():
    """每一条模板 + 最长的变量组合都不许超过 max_chars。"""
    pool = GreetPool.from_file(TEMPLATES_PATH)
    long_job = _job(title="法务合规与知识产权高级专员", company="某某省某某市某某科技发展有限公司")
    short_job = _job(title="", company="")

    for tpl_id in pool.template_ids:
        for job in (long_job, short_job, _job()):
            text, used_id = pool.pick_detail(job)
            assert len(text) <= DEFAULT_MAX_CHARS, f"{tpl_id} 渲染超长：{text}"
            assert text.strip(), "文案不能是空串"
            assert "{" not in text and "}" not in text, f"占位符没被替换：{text}"
            assert isinstance(used_id, str)


def test_missing_title_and_company_still_read_naturally():
    """岗位缺字段也要通顺：不许出现"在招，"这种悬空标点。"""
    pool = GreetPool.from_file(TEMPLATES_PATH)
    text = pool.pick(_job(title="", company=""))
    assert "这个岗位" in text or "贵司" in text
    assert "，," not in text
    assert "在招，" not in text


# --------------------------------------------------------------------------- #
# 2. 确定性 + 同轮避让
# --------------------------------------------------------------------------- #
def test_same_job_always_same_text():
    """同一个 job_id 生成 50 次，文案与模板 id 必须完全一致（重试不换话）。"""
    pool = _make_pool()
    job = _job("job_stable")
    first = pool.pick_detail(job)
    for _ in range(50):
        assert pool.pick_detail(job) == first


def test_avoid_prevents_repeat_within_one_round():
    """同一轮里 5 条不撞句（组内还有别的选择时）。"""
    pool = GreetPool.from_file(TEMPLATES_PATH)
    used: set[str] = set()
    for i in range(5):
        text, _ = pool.pick_detail(_job(f"job_{i:03d}", title="法务专员", company="某某公司"), avoid=used)
        assert text not in used, f"第 {i + 1} 条与前面撞句：{text}"
        used.add(text)


def test_avoid_never_deadlocks_when_group_exhausted():
    """整组都用过时退回确定性那一条，而不是抛异常/返回空串。"""
    general = [
        GreetTemplate(id="a", match_words=(), text="您好，看到贵司在招{title}。"),
        GreetTemplate(id="b", match_words=(), text="您好，关注到{company}的{title}。"),
    ]
    extras = [
        GreetTemplate(id=f"x{i}", match_words=("永不命中",), text=f"您好，这是{{title}}的第{i}条。")
        for i in range(MIN_TEMPLATES - len(general))
    ]
    pool = GreetPool(general + extras)
    job = _job()

    first = pool.pick(job)
    second = pool.pick(job, avoid={first})
    assert second != first, "组内还有第二条时应该换一条"

    third = pool.pick(job, avoid={first, second})
    assert third in {first, second}, "全用过后应退回确定性那一条"


def test_keyword_group_wins_over_general():
    """标题命中"合同"时，应该落到合同组（模板 id 前缀 c_），而不是通用组。"""
    pool = GreetPool.from_file(TEMPLATES_PATH)
    _, tpl_id = pool.pick_detail(_job(title="合同审核专员"))
    assert tpl_id.startswith("c_"), f"命中了 {tpl_id}，说明关键词分组没生效"


# --------------------------------------------------------------------------- #
# 3. 坏池子不许启动
# --------------------------------------------------------------------------- #
def test_too_few_templates_rejected():
    with pytest.raises(GreetPoolError) as exc:
        _make_pool(MIN_TEMPLATES - 1)
    assert "少于下限" in str(exc.value)


def test_fabricated_experience_rejected(tmp_path: Path):
    """`3年经验` 这种编造资历必须被拦下（含全角/半角数字）。"""
    path = tmp_path / "bad.yaml"
    rows = "\n".join(
        f'  - id: t{i:02d}\n    match_words: []\n    text: "您好，我在法务岗有3年经验，想了解{{title}}。"'
        for i in range(MIN_TEMPLATES)
    )
    path.write_text(f"version: 1\nmax_chars: 60\ntemplates:\n{rows}\n", encoding="utf-8")
    with pytest.raises(GreetPoolError) as exc:
        GreetPool.from_file(path)
    assert "编造量化资历" in str(exc.value)


def test_too_long_template_rejected(tmp_path: Path):
    path = tmp_path / "long.yaml"
    rows = "\n".join(
        f'  - id: t{i:02d}\n    match_words: []\n    text: "您好，这一段话非常非常长，'
        f'长到超过六十个字的上限，目的就是想触发校验，岗位是{{title}}，公司是{{company}}，谢谢您。"'
        for i in range(MIN_TEMPLATES)
    )
    path.write_text(f"version: 1\nmax_chars: 60\ntemplates:\n{rows}\n", encoding="utf-8")
    with pytest.raises(GreetPoolError) as exc:
        GreetPool.from_file(path)
    assert "超过上限" in str(exc.value)


def test_missing_file_is_loud(tmp_path: Path):
    with pytest.raises(GreetPoolError) as exc:
        GreetPool.from_file(tmp_path / "nope.yaml")
    assert "缺少招呼文案池" in str(exc.value)
    assert GreetPool.from_file(tmp_path / "nope.yaml", required=False) is None


def test_duplicate_ids_rejected():
    items = [
        GreetTemplate(id="dup" if i % 2 == 0 else f"t{i}", match_words=(), text=f"您好，第{i}条。")
        for i in range(MIN_TEMPLATES)
    ]
    with pytest.raises(GreetPoolError) as exc:
        GreetPool(items)
    assert "id 重复" in str(exc.value)


# --------------------------------------------------------------------------- #
# 4. 配置装载
# --------------------------------------------------------------------------- #
def test_load_pool_from_cfg_reads_relative_path():
    cfg = {"greet": {"templates": "config/prompts/greet_templates.yaml", "max_chars": 60}}
    pool = load_pool(cfg)
    assert len(pool) >= MIN_TEMPLATES
    assert pool.max_chars == 60

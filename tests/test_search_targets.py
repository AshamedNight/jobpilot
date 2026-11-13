"""闭环 4：搜索目标的声明式展开（keywords × cities）。

背景：原来是 9 条手写 URL，全带 `city=101120600`（潍坊），而注释写着"淄博=101120300"，
结果 124 条招呼里 82 条打给了潍坊。手写 URL 错一处 = 一处静默故障。
"""

from __future__ import annotations

from app.core import search_targets as st


def test_expands_keywords_by_cities():
    target = {
        "keywords": ["法务", "合规"],
        "cities": [{"name": "淄博", "code": "101120300"}, {"name": "潍坊", "code": "101120600"}],
    }
    urls = st.build_urls(target)
    assert len(urls) == 4, "2 关键词 × 2 城市"
    assert all("city=1011203" in u or "city=1011206" in u for u in urls)
    assert st.describe(target) == "2 关键词 × 2 城市 = 4 条搜索"


def test_explicit_search_urls_win():
    target = {
        "keywords": ["法务"],
        "cities": [{"name": "淄博", "code": "101120300"}],
        "search_urls": ["https://example.com/x?city=1&query=a"],
    }
    assert st.build_urls(target) == ["https://example.com/x?city=1&query=a"]


def test_city_names_accepts_both_shapes():
    assert st.city_names({"cities": ["淄博"]}) == {"淄博"}
    assert st.city_names({"cities": [{"name": "淄博", "code": "1"}]}) == {"淄博"}
    assert st.city_names({}) == set(), "空 = 不限制"


def test_city_without_code_is_not_expanded():
    """只有名字没有码的城市只用于白名单，不能拿去拼 URL。"""
    target = {"keywords": ["法务"], "cities": [{"name": "淄博"}]}
    assert st.build_urls(target) == []
    assert st.city_names(target) == {"淄博"}


def test_real_config_expands():
    from app.config import load_config

    target = load_config().get("target") or {}
    urls = st.build_urls(target)
    assert urls, "真实配置必须能展开出搜索 URL"
    assert all("city=" in u for u in urls), "每条都要带城市码"

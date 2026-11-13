"""页码轮转（2026-11-04 风控实测的应对）。

**现场**：一轮 pass 连着搜 5 页，第 2 页就返回 `ENVIRONMENT_RISK`；
但同样的第 1 页、第 2 页**单独**请求都正常（77 秒 / 177 秒，`ok=true, found=10`）。
结论：触发点不是"访问量大"，而是"一个进程里短时间内连续翻页"这个爬虫特征。

**对策**：pacer 每次 pass 只翻一页，跨 pass 轮转 1..pages。候选池照样能覆盖到第 5 页，
但请求被摊到每条招呼一次翻页。

这组用例钉住两件事：
1. `search(page=N)` 只发一次 CLI 调用，且 `--page` 就是 N（不许偷偷补翻其他页）
2. 游标严格在 1..pages 里循环
"""

from __future__ import annotations

import json

from app import models
from app.adapters import boss as B
from app.main import create_app


class _Stub:
    def __init__(self) -> None:
        self.calls: list[list[str]] = []

    def __call__(self, args=None, prog_name=None, standalone_mode=None) -> None:  # noqa: ARG002
        self.calls.append(list(args or []))
        print(
            json.dumps(
                {
                    "ok": True,
                    "schema_version": "1.0",
                    "command": "search",
                    "data": [],
                    "pagination": {"page": 1, "has_more": True},
                    "error": None,
                }
            )
        )


def test_search_with_page_issues_exactly_one_call(tmp_path, monkeypatch):
    stub = _Stub()
    monkeypatch.setattr(B, "_cli_entry", lambda: stub)
    adapter = B.BossAdapter(
        {"adapter_options": {"data_dir": str(tmp_path), "timeout_s": 5, "pages": 5}}
    )

    adapter.search("法务", page=3)

    assert len(stub.calls) == 1, f"给了 page=3 却发了 {len(stub.calls)} 次请求"
    assert "--page" in stub.calls[0]
    assert stub.calls[0][stub.calls[0].index("--page") + 1] == "3"


def test_search_without_page_still_walks_all_pages(tmp_path, monkeypatch):
    """不传 page 的老口径（sweep / 手动入口）必须保持不变。"""
    stub = _Stub()
    monkeypatch.setattr(B, "_cli_entry", lambda: stub)
    adapter = B.BossAdapter(
        {"adapter_options": {"data_dir": str(tmp_path), "timeout_s": 5, "pages": 3}}
    )

    adapter.search("法务")

    pages = [c[c.index("--page") + 1] for c in stub.calls]
    assert pages == ["1", "2", "3"], f"没给 page 时应顺序翻完，实际 {pages}"


def test_pacer_page_cursor_rotates(tmp_path):
    app = create_app(db_path=tmp_path / "rotate.db")
    models.init_db(tmp_path / "rotate.db").close()
    pacer = app.state.pacer
    pacer.cfg["adapter_options"]["pages"] = 3

    seen = [pacer._next_search_page() for _ in range(7)]
    assert seen == [1, 2, 3, 1, 2, 3, 1]


def test_pacer_page_cursor_survives_single_page_config(tmp_path):
    """`pages: 1` 时游标必须恒为 1（不能出现 page=2 这种越界请求）。"""
    app = create_app(db_path=tmp_path / "single.db")
    models.init_db(tmp_path / "single.db").close()
    pacer = app.state.pacer
    pacer.cfg["adapter_options"]["pages"] = 1
    pacer.cfg["target"].pop("search_urls", None)

    assert [pacer._next_search_page() for _ in range(3)] == [1, 1, 1]


def test_single_keyword_rotation_falls_back_when_urls_missing(tmp_path):
    """没有任何"关键词/城市"配置时，退化成"单 URL × 页码轮转"（老行为不变）。

    【2026-11-12 更新】新增了 `keywords × cities` 声明式展开（闭环 4），优先级为
    显式 `search_urls` > `keywords × cities` > 单条 `search_url`。
    所以这里要**先清掉 keywords/cities**，才测得到最后一档兜底。
    """
    app = create_app(db_path=tmp_path / "fallback.db")
    models.init_db(tmp_path / "fallback.db").close()
    pacer = app.state.pacer
    pacer.cfg["adapter_options"]["pages"] = 2
    pacer.cfg["target"].pop("search_urls", None)
    pacer.cfg["target"].pop("keywords", None)
    pacer.cfg["target"].pop("cities", None)
    pacer.cfg["target"]["search_url"] = "https://a/?q=only"

    seen = [pacer._next_search_target() for _ in range(4)]
    assert seen == [("https://a/?q=only", 1), ("https://a/?q=only", 2)] * 2


def test_keywords_and_cities_expand_into_rotation(tmp_path):
    """闭环 4：给了 keywords + cities 就自动展开成多条 URL 参与轮转。"""
    app = create_app(db_path=tmp_path / "expand.db")
    models.init_db(tmp_path / "expand.db").close()
    pacer = app.state.pacer
    pacer.cfg["adapter_options"]["pages"] = 1
    pacer.cfg["target"].pop("search_urls", None)
    pacer.cfg["target"]["keywords"] = ["法务", "合规"]
    pacer.cfg["target"]["cities"] = [
        {"name": "淄博", "code": "101120300"},
        {"name": "潍坊", "code": "101120600"},
    ]

    urls = [url for url, _ in (pacer._next_search_target() for _ in range(4))]
    assert len(set(urls)) == 4, f"2 词 × 2 城 应展开成 4 条不同 URL：{urls}"
    assert all("city=" in u for u in urls)


def test_rotation_covers_keywords_and_pages(tmp_path):
    """多关键词：先换页、再换词；每组在轮询里只出现一次。"""
    app = create_app(db_path=tmp_path / "multi.db")
    models.init_db(tmp_path / "multi.db").close()
    pacer = app.state.pacer
    pacer.cfg["adapter_options"]["pages"] = 2
    pacer.cfg["target"]["search_urls"] = ["https://a/?q=1", "https://a/?q=2"]

    seen = [pacer._next_search_target() for _ in range(5)]

    assert seen == [
        ("https://a/?q=1", 1),
        ("https://a/?q=1", 2),
        ("https://a/?q=2", 1),
        ("https://a/?q=2", 2),
        ("https://a/?q=1", 1),
    ]


def test_status_peek_does_not_consume_target(tmp_path):
    """`status()` 只能"看一眼"，不许推进游标（否则每次轮询都吃掉一组）。"""
    app = create_app(db_path=tmp_path / "peek.db")
    models.init_db(tmp_path / "peek.db").close()
    pacer = app.state.pacer
    pacer.cfg["adapter_options"]["pages"] = 3
    pacer.cfg["target"]["search_urls"] = ["https://a/?q=1", "https://a/?q=2"]

    first = (pacer.status()["next_search_url"], pacer.status()["next_search_page"])
    second = (pacer.status()["next_search_url"], pacer.status()["next_search_page"])
    assert first == second == ("https://a/?q=1", 1)

"""闭环 6：PWA 自检。

这些检查的价值在于"静默失效"：sw.js 一个语法错误，页面照常打开，
只是不能离线、通知永远不来 —— 不报错，只能靠断言兜住。
"""

from __future__ import annotations

import json
from pathlib import Path

from app.core import pwa

WEB = Path(__file__).resolve().parents[1] / "app" / "web"


def test_real_assets_pass_all_checks():
    result = pwa.check(WEB)
    assert result["ok"] is True, result
    assert all(result["checks"].values()), result


def test_detects_broken_comment_in_sw(tmp_path):
    """`**/api/` 里的 `*/` 会提前闭合块注释 —— 今天真踩过。"""
    (tmp_path / "sw.js").write_text('/* 说明 **/api/ 不缓存 */\nself.addEventListener("fetch", () => {});', encoding="utf-8")
    result = pwa.check(tmp_path)
    assert result["checks"]["sw_comment_safe"] is False
    assert result["ok"] is False


def test_detects_sw_that_caches_api(tmp_path):
    (tmp_path / "sw.js").write_text("self.addEventListener('fetch', () => {});", encoding="utf-8")
    result = pwa.check(tmp_path)
    assert result["checks"]["sw_skips_api"] is False, "没有 /api/ 短路判断就说明可能在缓存实时数据"


def test_detects_broken_manifest(tmp_path):
    (tmp_path / "manifest.webmanifest").write_text("{ not json", encoding="utf-8")
    result = pwa.check(tmp_path)
    assert result["checks"]["manifest_parsable"] is False


def test_manifest_requires_icon_sizes(tmp_path):
    (tmp_path / "manifest.webmanifest").write_text(
        json.dumps(
            {"name": "x", "start_url": "/", "display": "standalone", "icons": [{"src": "a.svg"}]},
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    result = pwa.check(tmp_path)
    assert result["checks"]["manifest_icons"] is False, "没有 sizes 声明 Chrome 不给装"


def test_empty_dir_reports_not_ok(tmp_path):
    result = pwa.check(tmp_path)
    assert result["ok"] is False
    assert result["checks"]["sw_js"] is False


def test_sw_served_from_root_with_scope_header(tmp_path):
    """service worker 必须从根路径返回，且带 `Service-Worker-Allowed: /`。

    否则它的作用域被限制在 `/static/`，**首页根本不受它管** ——
    表现就是"已激活"但离线打不开（2026-11-12 现场）。
    """
    from fastapi.testclient import TestClient

    from app.main import create_app

    app = create_app(db_path=tmp_path / "sw-scope.db")
    with TestClient(app) as client:
        resp = client.get("/sw.js")

    assert resp.status_code == 200
    assert resp.headers.get("service-worker-allowed") == "/", "没有这个头，作用域就只有 /static/"
    assert "javascript" in resp.headers.get("content-type", "")
    assert "no-store" in resp.headers.get("cache-control", ""), "SW 脚本自身不能被缓存住"

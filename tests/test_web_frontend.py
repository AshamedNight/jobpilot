"""前端契约测试（闭环 0 + PWA 前期）。

这层不做像素级断言，只钉住三件容易在改版里"悄悄丢掉"的事：

1. 页面上**有**连接入口（闭环 0 的交付物就是它，掉了等于功能消失）
2. manifest 能被解析、`start_url`/`display` 合法（PWA 装不上往往是这里写错）
3. 静态资源真的挂上了 `/static`（改文件名后忘了同步，页面会静默 404）

为什么值得测：前端没有任何构建步骤，改错一个字就是 200 + 白屏，
没有测试就只能靠人肉刷新发现。
"""

from __future__ import annotations

import json
from pathlib import Path

WEB = Path(__file__).resolve().parents[1] / "app" / "web"


def test_index_has_connect_entry():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert 'id="connect-strip"' in html, "连接面板不见了"
    assert 'id="btn-connect"' in html, "连接按钮不见了"
    assert 'id="conn-state"' in html, "连接状态徽标不见了"


def test_index_links_manifest_and_viewport():
    html = (WEB / "index.html").read_text(encoding="utf-8")
    assert 'rel="manifest"' in html, "PWA 的 manifest 没挂上，手机装不了"
    assert 'name="viewport"' in html, "少了 viewport，手机上会缩成一小块"


def test_manifest_is_valid_and_installable_shape():
    data = json.loads((WEB / "manifest.webmanifest").read_text(encoding="utf-8"))
    assert data["start_url"] == "/"
    assert data["display"] == "standalone"
    assert data["icons"] and data["icons"][0]["src"].startswith("/static/"), (
        "图标必须走 /static，否则 manifest 在子路径下会 404"
    )
    for icon in data["icons"]:
        target = WEB / Path(icon["src"]).name
        assert target.exists(), f"manifest 指的图标不存在：{icon['src']}"


def test_icon_is_svg():
    """不引入二进制图标：manifest 支持 sizes=any 的 SVG，省掉一套 png。"""
    text = (WEB / "icon.svg").read_text(encoding="utf-8").strip()
    assert text.startswith("<svg")

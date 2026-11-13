"""PWA 自检（闭环 6）：把"能不能装、能不能离线、通知脚本在不在"变成可断言的检查。

## 为什么要这个

前端资产的问题是**静默失效**：`sw.js` 里一个多出来的 `*/` 让整个文件语法错误
（今天就踩了），页面照常打开、只是不能离线、通知永远不来。这种问题不会报错，
只会在某天需要它的时候发现"原来一直没生效"。

所以把关键约束写成检查项，`GET /api/pwa/health` 一次问清楚。

## 检查项

| 项 | 为什么 |
|---|---|
| `sw.js` 存在 | 没有它就没有离线与通知点击 |
| `sw.js` **不缓存接口** | 缓存一个过期的 `used` 比页面打不开更糟 |
| `manifest.webmanifest` 能解析 | 坏掉的 manifest 在浏览器里是静默忽略 |
| manifest 有 `name` / `start_url` / `display` | 缺任一都不满足"可安装" |
| 图标声明了 `sizes` | 没有尺寸声明，Chrome 不给装 |
| `notify.js` 暴露 `diffStatus` | 通知的判定逻辑必须可测 |
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

_ICON_OK = {"any", "192x192", "512x512"}


def check(web_dir: Path) -> dict[str, Any]:
    """对 `app/web` 目录做一遍自检，返回 `{ok, checks}`。纯函数，不碰网络。"""
    checks: dict[str, bool] = {}
    details: dict[str, str] = {}

    sw = web_dir / "sw.js"
    sw_text = sw.read_text(encoding="utf-8") if sw.exists() else ""
    checks["sw_js"] = bool(sw_text)
    if sw_text:
        # 接口路径必须绕过缓存；这里查的是那条短路判断还在不在
        checks["sw_skips_api"] = 'startsWith("/api/")' in sw_text
        details["sw_skips_api"] = "service worker 里找不到 /api/ 短路判断" if not checks["sw_skips_api"] else ""
        # 注释里的 `*/` 会提前结束块注释，导致整文件语法错误（今天真踩过）
        checks["sw_comment_safe"] = "**/api/" not in sw_text
        details["sw_comment_safe"] = "注释里出现 **/api/ 会提前闭合块注释" if not checks["sw_comment_safe"] else ""

    manifest_path = web_dir / "manifest.webmanifest"
    manifest: dict[str, Any] = {}
    if manifest_path.exists():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            manifest = {}
    checks["manifest_parsable"] = bool(manifest)
    checks["manifest_fields"] = all(manifest.get(key) for key in ("name", "start_url", "display"))
    icons = manifest.get("icons") or []
    checks["manifest_icons"] = bool(icons) and all(
        str(icon.get("sizes") or "") in _ICON_OK for icon in icons if isinstance(icon, dict)
    )
    if not checks["manifest_icons"]:
        details["manifest_icons"] = "图标缺少 sizes 声明（或不是 any/192x192/512x512）"

    notify = web_dir / "notify.js"
    notify_text = notify.read_text(encoding="utf-8") if notify.exists() else ""
    checks["notify_js"] = "diffStatus" in notify_text and "JobPilotNotify" in notify_text

    index = web_dir / "index.html"
    index_text = index.read_text(encoding="utf-8") if index.exists() else ""
    checks["registers_sw"] = 'register("/sw.js")' in index_text
    checks["loads_notify"] = "/static/notify.js" in index_text

    return {"ok": all(checks.values()), "checks": checks, "details": details}

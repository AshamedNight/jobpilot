"""搜索目标：把「关键词 × 城市」展开成真实可用的搜索 URL。

## 为什么需要这一层

原来 `config.yaml` 里手写了 9 条完整 URL，每条都带 `city=101120600`。两个后果：

1. **城市写错了没人发现**：9 条全是潍坊（101120600），而注释里写着"101120300 就是淄博"，
   结果 124 条招呼里 82 条打给了潍坊。
2. **加城市要手改 8 条 URL**：错一处就是一处静默故障，而且没法一眼看出"我到底在搜哪几个城市"。

现在改成声明式：

```yaml
target:
  keywords: [法律顾问, 法务, ...]
  cities:
    - {name: 淄博, code: "101120300"}
    - {name: 潍坊, code: "101120600"}
```

启动时展开成 `keywords × cities` 条 URL。加一个城市 = 加一行；
显式给了 `search_urls` 则以显式为准（保留向后兼容与手工调试能力）。

## 城市名与城市码

白名单用的是**城市名**（岗位数据里的 `city` 字段是中文名），URL 用的是**城市码**，
两者必须成对维护，所以放在同一个条目里，不允许分别散落。
"""

from __future__ import annotations

from typing import Any
from urllib.parse import quote

#: BOSS 搜索页模板。`position` 是 UI 私有参数、会被丢弃，所以不放进模板。
SEARCH_URL_TEMPLATE = "https://www.zhipin.com/web/geek/jobs?city={code}&query={query}"


def _clean(value: Any) -> str:
    return str(value or "").strip()


def cities(target: dict[str, Any] | None) -> list[dict[str, str]]:
    """归一化 `target.cities` 成 `[{"name":…, "code":…}]`。

    兼容两种写法（老配置里 `cities` 是纯名字列表，只用于白名单）：
    - `["淄博"]`                     → `[{"name": "淄博", "code": ""}]`
    - `[{name: 淄博, code: "101120300"}]` → 原样
    """
    out: list[dict[str, str]] = []
    for item in (target or {}).get("cities") or []:
        if isinstance(item, dict):
            name = _clean(item.get("name"))
            code = _clean(item.get("code"))
        else:
            name, code = _clean(item), ""
        if name or code:
            out.append({"name": name, "code": code})
    return out


def city_names(target: dict[str, Any] | None) -> set[str]:
    """城市白名单（中文名集合）。空集合 = 不限制。"""
    return {item["name"] for item in cities(target) if item["name"]}


def keywords(target: dict[str, Any] | None) -> list[str]:
    return [_clean(k) for k in ((target or {}).get("keywords") or []) if _clean(k)]


def build_urls(target: dict[str, Any] | None) -> list[str]:
    """展开成搜索 URL 列表。

    优先级：显式 `search_urls` > `keywords × cities` > 单条 `search_url`。
    """
    target = target or {}
    explicit = [_clean(u) for u in (target.get("search_urls") or []) if _clean(u)]
    if explicit:
        return explicit

    keys = keywords(target)
    with_code = [c for c in cities(target) if c["code"]]
    if keys and with_code:
        return [
            SEARCH_URL_TEMPLATE.format(code=city["code"], query=quote(key))
            for key in keys
            for city in with_code
        ]

    fallback = _clean(target.get("search_url"))
    return [fallback] if fallback else []


def describe(target: dict[str, Any] | None) -> str:
    """给日志/页面用的一句话摘要：`8 关键词 × 2 城市 = 16 条`。"""
    target = target or {}
    urls = build_urls(target)
    keys, city_list = keywords(target), cities(target)
    if keys and city_list and not (target.get("search_urls") or []):
        return f"{len(keys)} 关键词 × {len(city_list)} 城市 = {len(urls)} 条搜索"
    return f"{len(urls)} 条搜索"

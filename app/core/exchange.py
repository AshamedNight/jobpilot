"""交换微信：卡片识别 + 号码提取（闭环 2.3 / 2.4）。

## 真实样本（2026-11-12 从会话 759861702 王女士 拉回，原文照抄）

1. HR 发来交换申请：
```python
{'dialog': {'backgroundUrl': '', 'clickMore': False,
            'buttons': [{'text': '同意', 'templateId': 0,
                         'url': 'bosszp://bosszhipin.app/openwith?type=sendaction&uid=75…'},
                        {'text': '拒绝', …}]}}
```
2. 我方点击「同意」（`out`）：
```python
{'action': {'extend': '{}', 'aid': 33}, 'type': 4, 'templateId': 1, 'headTitle': ''}
```
3. 系统下发号码（`in`）：
```text
王女士的微信号：&lt;copy&gt;Z13800138000&lt;/copy&gt;
```

## 三条设计判断

- **`content` 不是 JSON，是 Python dict 字面量**（单引号）。用 `ast.literal_eval` 解析，
  绝不用 `json.loads`，也别用 `eval`（字符串来自网络）。
- **号码被 HTML 转义**（`&lt;copy&gt;`）：提取前先 `html.unescape`，否则正则永远抓不到。
- **判据是"结构 + 语义"双条件**：只有 `dialog.buttons` 且按钮里出现"同意"才算交换申请；
  只认 `aid==33` 才算"我方同意"。宁可漏判也不误判 —— 误判会把普通聊天当成交换，
  进而写错状态、甚至触发不该发的请求。
"""

from __future__ import annotations

import ast
import html
import re
from typing import Any

#: 我方"同意交换"的动作码（真机样本 aid=33）。
_AGREE_AID = 33

#: 号码下发文案：`XX的微信号：<copy>ID</copy>`，`copy` 标签可能不存在或已被转义。
_WECHAT_LINE_RE = re.compile(r"的微信(?:号)?[：:]\s*(?:<copy>)?\s*([A-Za-z0-9_\-]{4,32})", re.IGNORECASE)

#: 兜底：只有昵称紧跟一个号、没有 `<copy>` 标签时用（见 `_WECHAT_BARE_RE`）。
_WECHAT_BARE_RE = re.compile(r"微信号[：:]\s*([A-Za-z0-9_\-]{4,32})", re.IGNORECASE)


def parse_card(content: Any) -> dict[str, Any] | None:
    """把卡片类 `content` 解析成 dict。解析不出来返回 `None`（普通文本会走这条）。

    只接受"看起来是结构体"的字符串（以 `{` / `[` 开头），避免把正常聊天内容送进解析器。
    """
    if isinstance(content, dict):
        return content
    if not isinstance(content, str):
        return None
    text = content.strip()
    if not text or text[0] not in "{[":
        return None
    try:
        parsed = ast.literal_eval(text)
    except (ValueError, SyntaxError, MemoryError, RecursionError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _button_texts(card: dict[str, Any]) -> list[str]:
    dialog = card.get("dialog")
    if not isinstance(dialog, dict):
        return []
    buttons = dialog.get("buttons")
    if not isinstance(buttons, list):
        return []
    out: list[str] = []
    for button in buttons:
        if isinstance(button, dict):
            out.append(str(button.get("text") or ""))
    return out


def is_exchange_request(content: Any) -> bool:
    """HR 发来的"交换微信"申请卡片。"""
    card = parse_card(content)
    if not card:
        return False
    texts = _button_texts(card)
    return any("同意" in text for text in texts)


def is_exchange_agreed(content: Any) -> bool:
    """我方点了「同意」的动作回执。"""
    card = parse_card(content)
    if not card:
        return False
    action = card.get("action")
    if not isinstance(action, dict):
        return False
    try:
        return int(action.get("aid")) == _AGREE_AID
    except (TypeError, ValueError):
        return False


def extract_wechat(content: Any) -> str | None:
    """从"号码下发"消息里提取微信号。

    先 `html.unescape`（真机是 `&lt;copy&gt;Z13800138000&lt;/copy&gt;`），
    再按 `的微信号：` 锚定；都失败时退回 `微信号：` 裸锚定。
    """
    if content is None:
        return None
    text = html.unescape(str(content))
    for pattern in (_WECHAT_LINE_RE, _WECHAT_BARE_RE):
        match = pattern.search(text)
        if match:
            return match.group(1)
    return None


def readable(content: Any) -> str | None:
    """把卡片类 content 换成一句人话；普通文本原样返回。

    为什么要做：BOSS 的卡片全是 dict 字面量，直接入库会在会话列表里糊一屏字段名
    （跟 `msg_type=招呼` 的职位卡是同一个坑）。
    """
    card = parse_card(content)
    if card is None:
        return None if content is None else str(content)
    if is_exchange_request(card):
        return "（对方请求交换微信）"
    if is_exchange_agreed(card):
        return "（已同意交换微信）"
    if extract_wechat(content):
        return str(content)  # 号码那行本身就要保留原文（含 <copy> 标签），前端展示时再清洗
    if "action" in card:
        return "（系统动作回执）"
    if "type" in card:
        return "（招呼卡片）"
    return "（卡片消息）"

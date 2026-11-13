"""闭环 2.3：交换微信卡片识别（用 2026-11-12 的真实样本）。

样本全部来自会话 759861702（王女士），一字未改：

| 方向 | content | 含义 |
|---|---|---|
| in  | `{'dialog': {'buttons': [{'text': '同意', …}, {'text': '拒绝', …}]}}` | HR 请求交换 |
| out | `{'action': {'aid': 33}, 'type': 4}` | 我方点了同意 |
| in  | `王女士的微信号：&lt;copy&gt;Z13800138000&lt;/copy&gt;` | 号码下发 |

判定必须是"结构 + 语义"双条件：只认 `dialog.buttons` 且按钮含"同意"、只认 `aid==33`。
误判的代价是写错状态、甚至触发不该发的请求。
"""

from __future__ import annotations

from app.core.exchange import (
    extract_wechat,
    is_exchange_agreed,
    is_exchange_request,
    parse_card,
    readable,
)

REAL_REQUEST = (
    "{'dialog': {'backgroundUrl': '', 'clickMore': False, 'buttons': "
    "[{'text': '同意', 'templateId': 0, 'url': 'bosszp://bosszhipin.app/openwith?type=sendaction&uid=75'}, "
    "{'text': '拒绝', 'templateId': 0, 'url': 'bosszp://bosszhipin.app/openwith?type=sendaction&uid=75'}]}}"
)
REAL_AGREED = "{'action': {'extend': '{}', 'aid': 33}, 'type': 4, 'templateId': 1, 'headTitle': ''}"
REAL_NUMBER = "王女士的微信号：&lt;copy&gt;Z13800138000&lt;/copy&gt;"
REAL_GREETING_CARD = "{'type': 8, 'templateId': 1, 'jobDesc': {'education': '学历不限'}}"


def test_parse_card_handles_dict_literal_not_json():
    """真机给的是 Python dict 字面量（单引号），json.loads 会直接失败。"""
    card = parse_card(REAL_AGREED)
    assert card is not None and card["type"] == 4
    assert parse_card("好的") is None, "普通聊天文本不该被当卡片"
    assert parse_card("{'broken': ") is None, "坏结构返回 None，不许抛"


def test_detects_exchange_request():
    assert is_exchange_request(REAL_REQUEST) is True
    assert is_exchange_request(REAL_AGREED) is False
    assert is_exchange_request("好的") is False
    assert is_exchange_request(None) is False


def test_detects_our_agreement():
    assert is_exchange_agreed(REAL_AGREED) is True
    assert is_exchange_agreed(REAL_REQUEST) is False, "申请卡不是我方同意"


def test_extracts_wechat_from_escaped_copy_tag():
    assert extract_wechat(REAL_NUMBER) == "Z13800138000"

    # 未转义形态也要认（不同账号/版本的渲染不一样）
    assert extract_wechat("王女士的微信号：<copy>Z13800138000</copy>") == "Z13800138000"
    # 左侧列表里出现过这种：`李先生的微信号：wxid_9f2k1`
    assert extract_wechat("李先生的微信号：wxid_9f2k1") == "wxid_9f2k1"
    # 没有号码的寒暄不许乱抓
    assert extract_wechat("您的微信号是多少？") is None
    assert extract_wechat(None) is None


def test_readable_replaces_card_payloads_with_words():
    assert readable(REAL_REQUEST) == "（对方请求交换微信）"
    assert readable(REAL_AGREED) == "（已同意交换微信）"
    assert readable("好的") == "好的"
    assert readable(REAL_GREETING_CARD) == "（招呼卡片）"
    # 号码那行必须保留原文（前端还要从里面提号码）
    assert "Z13800138000" in (readable(REAL_NUMBER) or "")

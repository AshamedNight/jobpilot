"""消息入库的两个真机坑（2026-11-09 实测 `chatmsg` 后补）。

真机样本（会话 753824113）：
```
{"platform_mid": "1794193397050:out:…", "direction": "out", "msg_type": "招呼",
 "content": "您好，打扰了，我想和您聊聊这个职位。", "created_at": "1794193397050"}
{"platform_mid": "1794193397030:in:…",  "direction": "in",  "msg_type": "招呼",
 "content": "{'type': 8, 'templateId': 1, 'jobDesc': {...}}", "created_at": "1794193397030"}
```

① `created_at` 是**毫秒整数**，库里约定是 ISO 文本；不归一会让时间线错乱、显示到 1970。
② 「招呼」卡片的 `content` 是**结构体字面量**，不是人话；直接入库会在会话列表里糊一屏字段名。
"""

from __future__ import annotations

import json

from app import models
from app.api.dashboard import _normalize_created_at, _readable_content, _store_messages


def test_ms_timestamp_is_normalized_to_iso():
    assert _normalize_created_at("1794193397050") == "2026-11-09T03:03:17Z"
    assert _normalize_created_at(1794193397) == "2026-11-09T03:03:17Z", "秒级也要能认"
    assert _normalize_created_at("2026-11-09T12:00:00Z") == "2026-11-09T12:00:00Z"
    assert _normalize_created_at(None) is None
    assert _normalize_created_at("") is None


def test_card_payload_becomes_readable_text():
    card = {"msg_type": "招呼", "content": "{'type': 8, 'templateId': 1, 'jobDesc': {...}}"}
    assert _readable_content(card) == "（招呼卡片）"

    plain = {"msg_type": "text", "content": "您好，方便发一份简历吗？"}
    assert _readable_content(plain) == "您好，方便发一份简历吗？"


def test_store_keeps_raw_and_dedupes(tmp_path):
    db = models.connect(tmp_path / "m.db")
    models.init_db(tmp_path / "m.db").close()
    batch = [
        {
            "platform_mid": "1:in:aa",
            "direction": "in",
            "msg_type": "招呼",
            "content": "{'type': 8}",
            "created_at": "1794193397030",
        },
        {
            "platform_mid": "2:out:bb",
            "direction": "out",
            "msg_type": "text",
            "content": "您好",
            "created_at": "1794193397050",
        },
        {"platform_mid": None, "content": "没有 mid 的消息", "created_at": "1794193397060"},
    ]

    assert _store_messages(db, "u1", batch) == 2, "没有 platform_mid 的要丢弃（不编假 mid）"
    assert _store_messages(db, "u1", batch) == 0, "UNIQUE(uid, platform_mid) 必须去重"

    rows = list(db.execute("SELECT platform_mid, content, raw_json, created_at FROM messages ORDER BY id"))
    assert rows[0]["content"] == "（招呼卡片）"
    assert json.loads(rows[0]["raw_json"])["msg_type"] == "招呼", "原始载荷必须留档"
    assert rows[0]["created_at"].endswith("Z"), "时间必须是 ISO 文本"

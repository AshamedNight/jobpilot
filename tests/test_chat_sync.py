"""`app/core/chat_sync` 的纯逻辑测试（全程离线，不发任何请求）。

覆盖的判据就是"该不该为这个会话花一次 chatmsg 请求"——
判错了要么漏掉回复，要么白打请求（后者会被平台记账）。
"""

from __future__ import annotations

from app.core.chat_sync import pick_targets, to_epoch


# ---------------------------------------------------------------- to_epoch


def test_epoch_handles_millis_and_seconds():
    # 上游两种都给过：毫秒整数和秒整数
    assert to_epoch(1794193397030) == 1794193397.03
    assert to_epoch(1794193397) == 1794193397


def test_epoch_handles_digit_strings():
    assert to_epoch("1794193397030") == 1794193397.03
    assert to_epoch(" 1794193397 ") == 1794193397


def test_epoch_handles_iso():
    assert to_epoch("1970-01-01T00:00:00Z") == 0.0
    assert to_epoch("2026-11-09T12:00:00Z") == to_epoch("2026-11-09T12:00:00+00:00")


def test_epoch_returns_none_for_unknown():
    # None 的语义是"不知道"，绝不能被当成 0 去参与比较
    for value in (None, "", "   ", "昨天", True, False):
        assert to_epoch(value) is None


# ---------------------------------------------------------------- pick_targets


def _conv(uid: str, unread: int = 0, last_ts: int | str | None = None) -> dict:
    return {"uid": uid, "unread": unread, "last_ts": last_ts, "hr_name": f"HR-{uid}"}


def test_unread_is_picked_first():
    remote = [
        _conv("a", unread=0, last_ts=2000),
        _conv("b", unread=3, last_ts=1000),
    ]
    picked = pick_targets(remote, {"a": "1970-01-01T00:10:00Z"}, limit=1)
    assert [c.uid for c in picked] == ["b"]  # 有未读的优先，哪怕时间更早
    assert picked[0].reason == "unread"
    assert picked[0].unread == 3


def test_new_timestamp_beats_older_local():
    remote = [_conv("a", last_ts=2000), _conv("b", last_ts=1000)]
    picked = pick_targets(remote, {"a": None, "b": None}, limit=5)
    # 本地都没有记录（首次同步）→ 两个都要，且按时间倒序
    assert [c.uid for c in picked] == ["a", "b"]
    assert picked[0].reason == "new_ts"


def test_skips_conversation_already_up_to_date():
    # 本地 last_msg_at 已经等于/晚于远端 → 不用再抓
    remote = [_conv("a", last_ts=1000)]
    picked = pick_targets(remote, {"a": "1970-01-01T00:16:40Z"}, limit=5)  # 1000 秒
    assert picked == []


def test_limit_caps_request_count():
    remote = [_conv(f"u{i}", unread=1, last_ts=i) for i in range(20)]
    picked = pick_targets(remote, {}, limit=5)
    assert len(picked) == 5, "limit 是硬上限 —— 一轮的请求数必须可控"


def test_rows_without_uid_are_dropped():
    remote = [{"unread": 5, "last_ts": 999}, {"uid": "  ", "unread": 5}, _conv("ok", unread=1)]
    picked = pick_targets(remote, {}, limit=5)
    assert [c.uid for c in picked] == ["ok"]


def test_bad_unread_does_not_crash():
    remote = [{"uid": "a", "unread": "很多", "last_ts": 1000}]
    picked = pick_targets(remote, {}, limit=5)
    assert [c.uid for c in picked] == ["a"]  # unread 解析失败按 0，仍按时间判


def test_limit_floor_is_one():
    # limit=0 或负数不该让工具"什么都不做"，至少处理 1 个
    assert len(pick_targets([_conv("a", unread=1)], {}, limit=0)) == 1

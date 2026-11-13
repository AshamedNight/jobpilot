"""闭环 2.1 / 2.2：拒绝识别 + `exchange()` 实现。

样本全部来自 2026-11-09 真机 `chatmsg`（会话 753824113），不是编的：

- 拒绝："抱歉，尽管您的简历很优秀，但是我们的职位需要其他资质的候选人。祝您在BOSS直聘找到您的理想工作！"
- 招呼卡片：`msg_type="招呼"` 的 content 是 dict 字面量
- 普通寒暄："您好，方便发一份简历吗？"

设计取向：**宁可漏判，不可误杀**。判错成拒绝 = 把一个还在聊的 HR 永久拉黑（终态不可逆）；
判漏 = 下一轮继续观察，代价只是多看一眼。
"""

from __future__ import annotations

from app.adapters import boss as B
from app.api.dashboard import is_rejection, last_inbound_text

REAL_REJECT = (
    "抱歉，尽管您的简历很优秀，但是我们的职位需要其他资质的候选人。"
    "祝您在BOSS直聘找到您的理想工作！"
)


# --------------------------------------------------------------------------- #
# 2.1 拒绝识别
# --------------------------------------------------------------------------- #
def test_real_rejection_sample_is_detected():
    assert is_rejection(REAL_REJECT) is True


def test_polite_text_is_not_a_rejection():
    """「抱歉」本身不算拒绝 —— 真人也会用它开场。"""
    assert is_rejection("抱歉，我这边稍晚回复您") is False
    assert is_rejection("您好，方便发一份简历吗？") is False
    assert is_rejection("") is False
    assert is_rejection("   ") is False


def test_other_rejection_phrasings():
    for text in ("您的经历与我们岗位不太合适", "该岗位已经招到了", "职位已关闭"):
        assert is_rejection(text) is True, text


def test_last_inbound_text_only_looks_at_latest_hr_message():
    messages = [
        {"direction": "in", "content": REAL_REJECT},   # 旧拒绝
        {"direction": "out", "content": "好的，谢谢您"},
        {"direction": "in", "content": "其实我们这个岗位又开了，有兴趣吗？"},  # 最新
    ]
    assert last_inbound_text(messages).startswith("其实我们")

    # 没有 HR 消息 → 空串（不该被当成拒绝）
    assert last_inbound_text([{"direction": "out", "content": "您好"}]) == ""


# --------------------------------------------------------------------------- #
# 2.2 exchange()
# --------------------------------------------------------------------------- #
class _Recorder:
    def __init__(self, envelope):
        self.envelope = envelope
        self.calls: list[tuple] = []

    def __call__(self, *args):
        self.calls.append(args)
        return self.envelope


def _adapter(monkeypatch, envelope):
    adapter = B.BossAdapter.__new__(B.BossAdapter)  # 不跑 __init__（避开真实配置）
    adapter.last_error = None
    adapter._record_upstream_failure = lambda *a, **k: None
    rec = _Recorder(envelope)
    monkeypatch.setattr(adapter, "_run_cli", rec)
    return adapter, rec


def test_exchange_sends_uid_and_type(monkeypatch):
    adapter, rec = _adapter(monkeypatch, {"ok": True, "data": {"message": "已发送"}})

    result = adapter.exchange("753824113", type="wechat")

    assert rec.calls == [("exchange", "753824113", "--type", "wechat")]
    assert result["sent"] is True
    assert result["raw"] == {"code": 0}, "raw 只放白名单键"


def test_exchange_risk_maps_to_account_risk(monkeypatch):
    adapter, _ = _adapter(
        monkeypatch, {"ok": False, "error": {"code": "ENVIRONMENT_RISK", "message": "风控"}}
    )

    result = adapter.exchange("u1")

    # 编排层靠这个码锁风险并 break，改名等于闸门失效
    assert result["error_code"] == "ACCOUNT_RISK"


def test_exchange_timeout_is_unconfirmed_and_must_not_be_retried(monkeypatch):
    adapter, _ = _adapter(
        monkeypatch, {"ok": False, "error": {"code": "TIMEOUT", "message": "超时"}}
    )

    result = adapter.exchange("u1")

    assert result["error_code"] is None, "结果不确定：error_code=None，调用方不许重试"
    assert result["sent"] is False

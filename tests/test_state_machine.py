"""会话状态机（任务书 §5.2）—— 7 条。

唯一定义在架构 §6.0 / §6.1：13 状态 / 10 事件 / 23 条边。
"""

from __future__ import annotations

import pytest

from app.core.state_machine import (
    EVENTS,
    STATES,
    TERMINAL_STATES,
    VALID_TRANSITIONS,
    IllegalTransition,
    can_reach,
    can_transition,
    edge_count,
    reachable_states,
    transition,
)


def test_normal_path_discovered_to_greeted():
    """① 正常路径：DISCOVERED --MATCHED--> SCORED --GREET_SENT--> GREETED。"""
    assert transition("DISCOVERED", "matched") == "SCORED"
    assert transition("SCORED", "greet_sent") == "GREETED"
    # 继续往下走到终点，证明主链路是通的
    assert transition("GREETED", "hr_replied") == "HR_REPLIED"
    assert transition("HR_REPLIED", "exchange_req_ok") == "EXCHANGE_REQUESTED"
    assert transition("EXCHANGE_REQUESTED", "exchange_done") == "WECHAT_GOT"


def test_illegal_event_raises():
    """② 非法事件抛 `IllegalTransition`（不静默返回原状态）。"""
    # 状态合法、事件合法，但这张表里没有这条边
    with pytest.raises(IllegalTransition) as excinfo:
        transition("DISCOVERED", "greet_sent")
    assert excinfo.value.current_state == "DISCOVERED"
    assert excinfo.value.event == "greet_sent"
    assert "非法转移" in str(excinfo.value)

    # 事件名压根不存在
    with pytest.raises(IllegalTransition):
        transition("SCORED", "no_such_event")

    # 状态名压根不存在
    with pytest.raises(IllegalTransition):
        transition("NO_SUCH_STATE", "matched")

    assert can_transition("DISCOVERED", "greet_sent") is False
    assert can_transition("DISCOVERED", "matched") is True


def test_risk_locked_rejects_every_event():
    """③ 从 RISK_LOCKED 出发的任何事件都抛异常（终态只能人工解除）。"""
    assert "RISK_LOCKED" in TERMINAL_STATES
    assert "RISK_LOCKED" not in {s.value for s in VALID_TRANSITIONS}

    for event in sorted(EVENTS):
        with pytest.raises(IllegalTransition):
            transition("RISK_LOCKED", event)
        assert can_transition("RISK_LOCKED", event) is False


def test_exchange_unconfirmed_rejects_every_event():
    """④ 从 EXCHANGE_UNCONFIRMED 出发的任何事件都抛异常（不做自动重试）。"""
    assert "EXCHANGE_UNCONFIRMED" in TERMINAL_STATES
    assert "EXCHANGE_UNCONFIRMED" not in {s.value for s in VALID_TRANSITIONS}

    for event in sorted(EVENTS):
        with pytest.raises(IllegalTransition):
            transition("EXCHANGE_UNCONFIRMED", event)
        assert can_transition("EXCHANGE_UNCONFIRMED", event) is False


def test_pending_manual_exists_but_has_no_inbound_edge():
    """⑤ PENDING_MANUAL 存在于 STATES，但没有任何路径可达（§2.2 / G9）。

    同时把结构常量钉死：13 状态 / 10 事件 / 23 条边 / 6 终态。
    """
    # 结构常量（防漂移）
    assert len(STATES) == 13
    assert len(EVENTS) == 10
    assert len(TERMINAL_STATES) == 6
    assert edge_count() == 23

    # 存在
    assert "PENDING_MANUAL" in STATES

    # 无入边
    inbound = [
        (state.value, event.value, target.value)
        for state, events in VALID_TRANSITIONS.items()
        for event, target in events.items()
        if target.value == "PENDING_MANUAL"
    ]
    assert inbound == []

    # 所以从任何状态都到不了它
    for state in sorted(STATES - {"PENDING_MANUAL"}):
        assert can_reach(state, "PENDING_MANUAL") is False

    # HANDED_OFF 同口径（R4-4：不加第 11 个 Event）
    assert "HANDED_OFF" in STATES
    assert len(EVENTS) == 10
    for state in sorted(STATES - {"HANDED_OFF"}):
        assert can_reach(state, "HANDED_OFF") is False


def test_can_reach_greeted_to_wechat_got():
    """⑥ `can_reach()` 正确性：GREETED 能走到 WECHAT_GOT，不能倒着走回 DISCOVERED。"""
    assert can_reach("GREETED", "WECHAT_GOT") is True
    assert can_reach("HR_REPLIED", "WECHAT_GOT") is True
    assert can_reach("EXCHANGE_INCOMING", "WECHAT_GOT") is True
    assert can_reach("WECHAT_GOT", "GREETED") is False

    # 派生自 VALID_TRANSITIONS，不是第二张表
    assert reachable_states("GREETED") >= {"HR_REPLIED", "REJECTED", "EXPIRED", "WECHAT_GOT"}

    # 未知状态不崩，返回 False
    assert can_reach("NO_SUCH_STATE", "WECHAT_GOT") is False
    # 【D2】未知状态即使"同状态"也必须 False：STATES 合法性检查在同状态判断之前
    assert can_reach("NO_SUCH_STATE", "NO_SUCH_STATE") is False


def test_self_transition_hr_replied_is_legal():
    """⑦ 同一状态到自身合法：HR_REPLIED --HR_REPLIED--> HR_REPLIED。"""
    assert transition("HR_REPLIED", "hr_replied") == "HR_REPLIED"
    assert can_transition("HR_REPLIED", "hr_replied") is True

    # 其他状态没有这条自环，别把自环写成全局行为
    assert can_transition("GREETED", "hr_replied") is True   # 但它是去 HR_REPLIED
    assert transition("GREETED", "hr_replied") == "HR_REPLIED"
    assert can_transition("SCORED", "hr_replied") is False

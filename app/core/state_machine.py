"""会话状态机（任务书 §5.2，**唯一定义**在架构 §6.0 / §6.1）。

13 个状态 / 10 个事件 / 23 条边。这是照抄架构的表，不许自己简化：

- `PENDING_MANUAL`：存在于 `STATES`，但 Phase 1 **无任何入边**（B 方案 Phase 5 才接）
- `HANDED_OFF`：存在于 `STATES`，同样**无入边**（R4-4：不加第 11 个 Event，
  语义是"交给人工、程序不再动"，由人写状态，不由事件驱动）
- 六个终态不作为 `VALID_TRANSITIONS` 的 key → 对它们任何事件都抛 `IllegalTransition`
"""

from __future__ import annotations

from collections import deque
from enum import Enum
from typing import Iterator


class State(str, Enum):
    DISCOVERED = "DISCOVERED"
    SCORED = "SCORED"
    GREETED = "GREETED"
    HR_REPLIED = "HR_REPLIED"
    REJECTED = "REJECTED"
    EXPIRED = "EXPIRED"
    EXCHANGE_REQUESTED = "EXCHANGE_REQUESTED"
    EXCHANGE_INCOMING = "EXCHANGE_INCOMING"
    PENDING_MANUAL = "PENDING_MANUAL"
    WECHAT_GOT = "WECHAT_GOT"
    HANDED_OFF = "HANDED_OFF"
    RISK_LOCKED = "RISK_LOCKED"
    EXCHANGE_UNCONFIRMED = "EXCHANGE_UNCONFIRMED"


STATES: frozenset[str] = frozenset(s.value for s in State)

TERMINAL_STATES: frozenset[str] = frozenset({
    "REJECTED",
    "EXPIRED",
    "WECHAT_GOT",
    "HANDED_OFF",
    "RISK_LOCKED",
    "EXCHANGE_UNCONFIRMED",
})


class Event(str, Enum):
    MATCHED = "matched"                      # matcher 过线
    GREET_SENT = "greet_sent"                # 打招呼成功且回读确认
    HR_REPLIED = "hr_replied"                # HR 发来消息
    REJECTED_BY_HR = "rejected_by_hr"        # HR 明确拒绝
    TIMEOUT_7D = "timeout_7d"                # 7 天无回复
    EXCHANGE_REQ_OK = "exchange_req_ok"      # 我方发起交换成功
    EXCHANGE_IN = "exchange_in"              # 对方发来交换请求
    EXCHANGE_DONE = "exchange_done"          # 对方同意，号码到手
    UNCONFIRMED = "unconfirmed"              # 动作结果无法确认
    RISK_HIT = "risk_hit"                    # 命中风控


EVENTS: frozenset[str] = frozenset(e.value for e in Event)


class IllegalTransition(Exception):
    """非法状态转移。**不要静默返回原状态**。"""

    def __init__(self, current_state: str, event: str) -> None:
        super().__init__(f"非法转移：状态 {current_state!r} 不接受事件 {event!r}")
        self.current_state = current_state
        self.event = event


# 数据模型是 dict[State, dict[Event, State]]，不是 dict[str, set[str]]（N2）：
# 集合只能表达"能不能去哪"，表达不了"哪个事件去哪"。
VALID_TRANSITIONS: dict[State, dict[Event, State]] = {
    State.DISCOVERED: {
        Event.MATCHED: State.SCORED,
    },
    State.SCORED: {
        Event.GREET_SENT: State.GREETED,
        Event.RISK_HIT: State.RISK_LOCKED,
    },
    State.GREETED: {
        Event.HR_REPLIED: State.HR_REPLIED,
        Event.REJECTED_BY_HR: State.REJECTED,
        Event.TIMEOUT_7D: State.EXPIRED,
        Event.RISK_HIT: State.RISK_LOCKED,
    },
    State.HR_REPLIED: {
        Event.HR_REPLIED: State.HR_REPLIED,
        Event.REJECTED_BY_HR: State.REJECTED,
        Event.TIMEOUT_7D: State.EXPIRED,          # N1 补边
        Event.EXCHANGE_REQ_OK: State.EXCHANGE_REQUESTED,
        Event.EXCHANGE_IN: State.EXCHANGE_INCOMING,
        Event.EXCHANGE_DONE: State.WECHAT_GOT,
        Event.RISK_HIT: State.RISK_LOCKED,
    },
    State.EXCHANGE_REQUESTED: {
        Event.EXCHANGE_DONE: State.WECHAT_GOT,
        Event.UNCONFIRMED: State.EXCHANGE_UNCONFIRMED,
        Event.TIMEOUT_7D: State.EXPIRED,
        Event.RISK_HIT: State.RISK_LOCKED,
    },
    State.EXCHANGE_INCOMING: {
        Event.EXCHANGE_DONE: State.WECHAT_GOT,
        Event.UNCONFIRMED: State.EXCHANGE_UNCONFIRMED,
        Event.REJECTED_BY_HR: State.REJECTED,     # N1 补边
        Event.TIMEOUT_7D: State.EXPIRED,          # N1 补边
        Event.RISK_HIT: State.RISK_LOCKED,
    },
    # PENDING_MANUAL / HANDED_OFF 刻意无入边；
    # 六个终态不出现在这里 → 任何事件都非法。
}


def transition(current_state: str, event: str) -> str:
    """返回下一状态。非法转移抛 `IllegalTransition`。

    入参是 (当前状态, 事件)，**不是** (当前, 目标)。
    """
    try:
        return VALID_TRANSITIONS[State(current_state)][Event(event)].value
    except (KeyError, ValueError) as exc:
        raise IllegalTransition(current_state, event) from exc


def can_transition(current_state: str, event: str) -> bool:
    """这个状态接受这个事件吗（不抛异常版本）。"""
    try:
        transition(current_state, event)
    except IllegalTransition:
        return False
    return True


def _iter_edges(current_state: str) -> Iterator[str]:
    try:
        state = State(current_state)
    except ValueError:
        return
    for next_state in VALID_TRANSITIONS.get(state, {}).values():
        yield next_state.value


def can_reach(current_state: str, target_state: str) -> bool:
    """目标状态是否可达。**从 `VALID_TRANSITIONS` 派生**，不另存第二张表。

    注意：`can_reach(<任意状态>, "HANDED_OFF")` 与 `can_reach(<任意状态>, "PENDING_MANUAL")`
    返回 False 是**正确行为**（两者都无入边，见架构 §6.0 说明）。
    """
    if current_state not in STATES or target_state not in STATES:
        return False
    if current_state == target_state:
        return True

    seen = {current_state}
    queue = deque([current_state])
    while queue:
        node = queue.popleft()
        for nxt in _iter_edges(node):
            if nxt == target_state:
                return True
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return False


def reachable_states(current_state: str) -> set[str]:
    """从某状态出发的所有可达状态（含自身）。排障 / 文档核对用。"""
    if current_state not in STATES:
        return set()
    seen = {current_state}
    queue = deque([current_state])
    while queue:
        node = queue.popleft()
        for nxt in _iter_edges(node):
            if nxt not in seen:
                seen.add(nxt)
                queue.append(nxt)
    return seen


def edge_count() -> int:
    """转移表里边的条数（应为 23）。测试用来防漂移。"""
    return sum(len(events) for events in VALID_TRANSITIONS.values())

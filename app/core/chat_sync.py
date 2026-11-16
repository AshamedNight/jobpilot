"""增量挑出"需要抓消息"的会话 —— 纯逻辑，不联网、不碰库，好测。

## 为什么必须有"增量"

`chat()` 一次能带回 100 条会话。如果逐个 `chatmsg` 全量遍历：
174 个会话 × 2 次请求 ≈ 350 次请求，**这本身就是风控目标**，
而其中绝大多数根本没有新消息。

真机实测 `chat()` 每项都带 `unread` 和 `last_ts`，所以：
**先花 1 次请求拿列表 → 只对"有新动静"的那几个调 chatmsg**，一轮通常 0~5 次。

这也是 2026-11-15 那次排查的核心结论：库里 186 条招呼、`reply_count` 全是 0，
不是没人回（用户在 App 里数到 20+ 条回复），而是**"收"这一半从来没有定时跑过**。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

#: 超过这个数就当成毫秒时间戳（秒级时间戳要到公元 33658 年才会这么大）
_MILLIS_THRESHOLD = 1e12


@dataclass(frozen=True)
class Candidate:
    """一个待跟进会话，以及"为什么挑它"。"""

    uid: str
    reason: str  # "unread"（有未读）| "new_ts"（最后消息时间比本地新）
    unread: int
    last_epoch: float

    def describe(self) -> str:
        if self.reason == "unread":
            return f"{self.unread} 条未读"
        return "最后消息比本地新"


def to_epoch(value: Any) -> float | None:
    """把上游的时间字段归一成秒级 epoch。

    上游同一个字段会给出三种形态（真机都见过）：
    - `1794193397030`（毫秒整数或它的字符串形式）
    - `1794193397`（秒整数）
    - `"2026-11-09T12:00:00Z"`（ISO）

    返回 None 表示"认不出来"，调用方当成"不知道"处理，不要当成 0 去比较。
    """
    if value is None:
        return None

    if isinstance(value, bool):  # bool 是 int 的子类，先挡掉
        return None
    if isinstance(value, (int, float)):
        num = float(value)
        return num / 1000.0 if num > _MILLIS_THRESHOLD else num

    text = str(value).strip()
    if not text:
        return None
    if text.isdigit():
        num = float(text)
        return num / 1000.0 if num > _MILLIS_THRESHOLD else num
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return None


def pick_targets(
    remote: list[dict[str, Any]],
    local_last_at: dict[str, str | None],
    limit: int,
) -> list[Candidate]:
    """从会话列表里挑出要抓消息的 uid。

    判据（两条取或）：
    1. `unread > 0` —— 上游自己说"有新消息"，最可靠的信号
    2. `last_ts` 比本地 `conversations.last_msg_at` 新 —— 兜住"已读但内容是新的"

    排序：有未读的优先，其次按最后消息时间倒序。最新的先处理，
    因为刚回复的 HR 最值得马上看到。`limit` 用来把一轮的请求数钉死在可控范围。
    """
    candidates: list[Candidate] = []

    for conv in remote:
        if not isinstance(conv, dict):
            continue
        uid = str(conv.get("uid") or "").strip()
        if not uid:
            continue

        try:
            unread = int(conv.get("unread") or 0)
        except (TypeError, ValueError):
            unread = 0

        last_epoch = to_epoch(conv.get("last_ts"))
        if last_epoch is None:
            last_epoch = to_epoch(conv.get("last_time"))
        if last_epoch is None:
            last_epoch = 0.0

        local_epoch = to_epoch(local_last_at.get(uid)) or 0.0

        if unread > 0:
            candidates.append(Candidate(uid, "unread", unread, last_epoch))
        elif last_epoch > local_epoch:
            candidates.append(Candidate(uid, "new_ts", 0, last_epoch))

    candidates.sort(key=lambda c: (c.unread > 0, c.last_epoch), reverse=True)
    return candidates[: max(1, limit)]

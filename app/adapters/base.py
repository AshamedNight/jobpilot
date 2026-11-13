"""适配器 Protocol（任务书 §5.7）。

签名注意：`greet(security_id, job_id, message)` —— **两个 ID**，不是 `greet(uid)`。
`batch_greet` **不在** Phase 1 的 Protocol 里（N9：90 分钟轮间隔是 Phase 2 的约束）。
"""

from __future__ import annotations

from typing import Any, Protocol, runtime_checkable

# 能力集合：面板按 capabilities 渲染，不具备的能力按钮不出现（架构 §1.5）
CAPABILITIES: frozenset[str] = frozenset({"search", "detail", "greet", "exchange", "chat"})


@runtime_checkable
class Adapter(Protocol):
    """所有适配器共用的形状。core 层只依赖它，不认识具体实现。"""

    capabilities: set[str]

    def probe(self) -> set[str]:
        """启动时探测能力。返回的能力集合会覆盖 `capabilities`。"""
        ...

    def search(self, query: str, **filters: Any) -> list[dict[str, Any]]:
        """搜岗位。每条结果必须同时带 job_id 与 security_id（greet 要两个）。"""
        ...

    def greet(self, security_id: str, job_id: str, message: str) -> dict[str, Any]:
        """打招呼。返回统一结构（见下）。"""
        ...

    def chat(self, **filters: Any) -> list[dict[str, Any]]:
        """会话列表。"""
        ...

    def chatmsg(self, uid: str, raw: bool = False) -> list[dict[str, Any]]:
        """单个会话的消息历史（回读验证 + 号码提取的唯一来源）。"""
        ...

    def exchange(self, uid: str, type: str = "wechat") -> dict[str, Any]:
        """发起交换请求。"""
        ...


def ok_result(detail: str | None = None, raw: dict[str, Any] | None = None) -> dict[str, Any]:
    """写操作统一返回结构（G8）。`sent=True` 只在回读确认后设。"""
    return {"sent": True, "error_code": None, "detail": detail, "raw": raw}


def err_result(
    error_code: str | None,
    detail: str | None = None,
    raw: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """写操作失败/无法确认。

    `error_code=None` + `sent=False` = **发出了但无法确认**，调用方必须按
    `UNCONFIRMED` 处理，**不能重试**（任务书 §5.7 关键约定）。
    """
    return {"sent": False, "error_code": error_code, "detail": detail, "raw": raw}

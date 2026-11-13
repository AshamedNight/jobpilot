"""/api/conversations 与 /api/conversations/{uid}（任务书 §6.1）。"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Depends, HTTPException

from app import models
from app.api import get_db

router = APIRouter(tags=["conversations"])


@router.get("/api/conversations")
def api_conversations(db=Depends(get_db)) -> dict[str, Any]:
    """会话列表（带状态）。"""
    items = models.list_conversations(db)
    return {"count": len(items), "items": items}


@router.get("/api/conversations/{uid}")
def api_conversation(uid: str, db=Depends(get_db)) -> dict[str, Any]:
    """单个会话的消息历史。"""
    conversation = models.get_conversation(db, uid)
    if conversation is None:
        raise HTTPException(status_code=404, detail=f"会话不存在：{uid}")
    # 【F2】wechat_id 只允许出现在这里（这是产品目的），列表接口不带。
    # sensitive=True 让前端知道这块要单独渲染（详情面板），不要往列表/日志里抄。
    return {
        "conversation": conversation,
        "messages": models.list_messages(db, uid),
        "sensitive": True,
    }

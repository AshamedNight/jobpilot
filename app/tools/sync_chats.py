"""增量同步 HR 回复 —— 补上"收"这一半。

## 为什么需要它

2026-11-15 排查发现的真问题：库里 186 条招呼、`conversations.reply_count` 全是 0、
`messages` 表只有 8 条（还都集中在 11-09~11-11）。
用户在 BOSS App 里数到 **20+ 条真实回复** —— 说明不是没人回，是**没人去收**。

`POST /api/run/followup` 早就有这个能力，但它是手动端点，没有定时任务调它，
所以那三天的数据是当时手动跑出来的，之后就没有了。

## 这个工具跟 followup 的区别

- followup 默认遍历列表里的全部会话（100 个 ≈ 100+ 次请求），文档里自己也写着"太贵"
- 这个工具**先花 1 次请求拿列表，再只抓有新动静的**（见 `app/core/chat_sync.py`）
- 默认 **dry-run**（只打印要抓谁，不发请求），加 `--apply` 才真的抓

## 用法

```powershell
# 1) 先看会抓谁（不发任何 chatmsg 请求）
.venv2\\Scripts\\python.exe -m app.tools.sync_chats

# 2) 真抓真写，一轮最多 5 个会话
.venv2\\Scripts\\python.exe -m app.tools.sync_chats --apply --limit 5

# 3) 连着跑几轮把积压的消化完（建议两轮之间隔 2-3 分钟）
.venv2\\Scripts\\python.exe -m app.tools.sync_chats --apply --limit 5
```

⚠️ 别在 pacer 正在 search 的时候跑：两边都走 `_CLI_LOCK`（浏览器独占），会互相抢。
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sqlite3
import sys
import time
import urllib.request
from datetime import datetime, timezone
from typing import Any

from app import models
from app.core import chat_sync, exchange
from app.core.state_machine import Event, can_transition, transition


def _pacer_running(port: int = 8010) -> bool | None:
    """问本地服务：pacer 现在在不在跑。

    返回 `None` 表示问不到（服务没起），调用方应当把它当"未知"处理，
    **不能**当"没在跑"——2026-11-16 我就是这么踩的：
    看 `pacer_state_b.json` 里没有 `running` 字段就断定"没起跑"，
    实际那个字段根本不落盘，pacer 当天 10:07 已经在跑，
    我 10:12 开始连发 7 次请求，10:18 直接把环境推进了 ENVIRONMENT_RISK。
    """
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}/api/pacing/status", timeout=4
        ) as resp:
            return bool(json.loads(resp.read().decode("utf-8")).get("running"))
    except Exception:  # noqa: BLE001 - 连不上就是"不知道"
        return None


def _advance(state: str, event: Event) -> str:
    """尽力推进状态：这条边不合法就原样返回，不抛。

    消息识别是"读"的产物，状态机是"写"的约束。一条历史消息不该因为当前状态
    不接受这条边，就把整轮同步打断。
    """
    return transition(state, event.value) if can_transition(state, event.value) else state


def _to_iso(epoch: float | None) -> str | None:
    """epoch 秒 → ISO 字符串。

    【踩过的坑】上游 `chatmsg` 的 `timestamp` 是 `"11-16 09:57"`（月-日 时:分，**没有年份**）。
    直接把它当 `last_msg_at` 写库，`to_epoch` 解析不出来 → 当成 0 →
    下一轮又判定"有新消息"→ 同一个会话被反复抓，永远抓不完。
    所以回写一律用 `chat()` 给的精确 `last_ts`（epoch）归一成 ISO。
    """
    if epoch is None:
        return None
    return datetime.fromtimestamp(epoch, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _store_messages(db: sqlite3.Connection, uid: str, messages: list[dict[str, Any]]) -> int:
    """消息入库，返回实际新增条数（重复的靠 UNIQUE 忽略）。"""
    added = 0
    for message in messages:
        mid = message.get("platform_mid")
        if not mid:
            continue  # 拿不到稳定 mid 的消息宁可不存，也不编假 id
        if models.insert_message(
            db,
            uid=uid,
            platform_mid=str(mid),
            direction=str(message.get("direction") or "in"),
            msg_type=str(message.get("msg_type") or "text"),
            content=exchange.readable(message.get("content")),
            raw_json=message,
            created_at=message.get("created_at"),
        ):
            added += 1
    return added


def _resolve_db_path(explicit: str | None) -> str:
    """定库路径。顺序很讲究，否则会在一个空库上"同步成功 0 条"，让人以为没回复。

    1. `--db` 显式指定
    2. 环境变量 `JOBPILOT_DB`（start-jobpilot.ps1 会设，指向 jobpilot-new.db）
    3. `data/jobpilot-new.db` 存在就用它 —— 本项目实际在用的是它，
       而 `models.DEFAULT_DB_PATH` 指向的是历史遗留的 `data/jobpilot.db`
    4. 兜底 DEFAULT_DB_PATH
    """
    if explicit:
        return explicit

    from_env = os.environ.get("JOBPILOT_DB")
    if from_env:
        return from_env

    candidate = models.PROJECT_ROOT / "data" / "jobpilot-new.db"
    if candidate.exists():
        return str(candidate)

    return str(models.DEFAULT_DB_PATH)


def _build_adapter(cfg: dict[str, Any]):
    from app.adapters.boss import BossAdapter

    return BossAdapter(cfg)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="增量同步 HR 回复（默认 dry-run）")
    ap.add_argument("--apply", action="store_true", help="真的调 chatmsg 并写库")
    ap.add_argument("--limit", type=int, default=5, help="一轮最多处理几个会话（默认 5）")
    ap.add_argument("--db", default=None, help="覆盖数据库路径（默认取 JOBPILOT_DB）")
    ap.add_argument("--gap", type=float, default=15.0,
                    help="每个会话之间等待的秒数（默认 15，0 表示不等待）")
    ap.add_argument("--force", action="store_true",
                    help="pacer 正在跑时也执行（不建议：两边请求叠加就是风控）")
    args = ap.parse_args(argv)

    # 前置闸门：先问 pacer 在不在跑。这一步比什么都重要 ——
    # 抓消息和打招呼打的是同一个平台，叠加起来就是我们一直在避免的"脚本形状"。
    pacer_state = _pacer_running()
    if pacer_state is True and not args.force:
        print("✗ pacer 正在跑 —— 拒绝执行。")
        print("  两边都在打平台请求，叠加会直接触发 ENVIRONMENT_RISK。")
        print("  先停：curl -X POST http://127.0.0.1:8010/api/pacing/stop")
        print("  （确认要硬来就加 --force，风险自负）")
        return 2
    if pacer_state is None:
        print("⚠ 没连上本地服务（8010），无法确认 pacer 是否在跑。请自己确认后再继续。\n")
    elif pacer_state is False:
        print("pacer 未运行 ✓\n")

    from app.config import load_config

    cfg = load_config()
    db_path = _resolve_db_path(args.db)
    print(f"库：{db_path}")
    print(f"模式：{'应用（会发请求）' if args.apply else '只体检（不发 chatmsg 请求）'}  上限 {args.limit}\n")

    db = models.connect(db_path)
    adapter = _build_adapter(cfg)

    # 第 1 次（也是唯一一次）列表请求
    print("读会话列表 …")
    remote = adapter.chat(page=1)
    print(f"列表返回 {len(remote)} 个会话")

    local = {c["uid"]: c.get("last_msg_at") for c in models.list_conversations(db)}
    targets = chat_sync.pick_targets(remote, local, args.limit)

    remote_by_uid: dict[str, dict[str, Any]] = {
        str(c["uid"]): c for c in remote if c.get("uid")
    }

    if not targets:
        print("没有需要跟进的新消息。")
        return 0

    print(f"\n需要跟进 {len(targets)} 个：")
    for cand in targets:
        name = next((c.get("hr_name") or c.get("company") for c in remote if c.get("uid") == cand.uid), "?")
        print(f"  {cand.uid}  {name}  —— {cand.describe()}")

    if not args.apply:
        print("\n（这是体检模式。加 --apply 才会真的抓消息。）")
        return 0

    total_added = 0
    replied = 0
    wechat_got = 0

    for index, cand in enumerate(targets):
        if index > 0 and args.gap > 0:
            wait = random.uniform(args.gap * 0.7, args.gap * 1.3)
            print(f"\n  等 {wait:.0f} 秒（人不会连珠炮地翻会话）…", end="")
            time.sleep(wait)

        print(f"\n→ 抓 {cand.uid} …", end=" ")
        messages = adapter.chatmsg(cand.uid)
        if not messages:
            print("没有消息（可能已被读掉）")
            continue

        added = _store_messages(db, cand.uid, messages)
        total_added += added

        # 回写状态：入向消息数 / 最后消息时间 / 交换微信
        conversation = models.get_conversation(db, cand.uid)
        state = (conversation or {}).get("state") or "GREETED"

        stored = models.list_messages(db, cand.uid)
        inbound = [m for m in stored if m.get("direction") == "in"]
        if inbound:
            replied += 1
            state = _advance(state, Event.HR_REPLIED)

        has_exchange_request = any(
            exchange.is_exchange_request(m.get("content")) for m in inbound
        )
        if has_exchange_request:
            state = _advance(state, Event.EXCHANGE_IN)

        wechat_id = None
        for message in inbound:
            found = exchange.extract_wechat(message.get("content"))
            if found:
                wechat_id = found
                break
        if wechat_id:
            wechat_got += 1
            state = _advance(state, Event.EXCHANGE_DONE)

        last_at = _to_iso(cand.last_epoch)

        fields: dict[str, Any] = {"reply_count": len(inbound)}
        if last_at:
            fields["last_msg_at"] = last_at
        if wechat_id:
            fields["wechat_id"] = wechat_id
            fields["wechat_source"] = "chatmsg"

        # 【踩过的坑】先 upsert 再写状态：`set_conversation_state` 是 UPDATE，
        # 会话不在表里时影响 0 行且**不报错**，数据会被静默丢掉
        # （张军锋那条就是这样没写进去的）。
        conv_remote = remote_by_uid.get(cand.uid, {})
        models.upsert_conversation(
            db,
            cand.uid,
            job_id=conv_remote.get("job_id"),
            hr_name=conv_remote.get("hr_name"),
            company=conv_remote.get("company"),
            state=state,
        )
        models.set_conversation_state(db, cand.uid, state, **fields)

        print(f"新增 {added} 条，状态 → {state}" + (f"，微信号 {wechat_id}" if wechat_id else ""))

    print(
        f"\n完成：处理 {len(targets)} 个会话，新增消息 {total_added} 条，"
        f"有回复 {replied} 个，拿到微信 {wechat_got} 个"
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())

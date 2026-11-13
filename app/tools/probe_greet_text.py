"""文案复验：我们传的 `--message` 到底有没有被平台用上？

## 为什么要这个工具

2026-11-11 真机读回的一条"我发出的"招呼是：

    您好，打扰了，我想和您聊聊这个职位。

这句话 **既不在我们 25 条文案池里，也不是上游 `api/client.py` 的兜底句**
（兜底是"您好，我对该岗位很感兴趣，希望能和您聊一聊。"）。它长得像 BOSS
账号级的**默认招呼语**。如果平台真的忽略 `greeting` 参数，那"25 条文案池 / 同轮不撞句"
这些验收全是形式达标，实际 HR 收到的永远是同一句 —— 也解释得通"发了 123 条 0 回复"。

## 它怎么判定

1. 搜一页，挑一个**没打过**的岗位
2. 用**带标记的文案**打招呼（标记里含时间戳，保证唯一）
3. 立刻用 `chat()` 找到这个岗位的会话 uid，再 `chatmsg` 读回最后一条 out 消息
4. 三选一结论：

| 读到什么 | 结论 | 下一步 |
|---|---|---|
| 我们发的那句（含标记） | 链路正常，之前那条是历史手工点击 | 文案池继续用 |
| 平台默认招呼语 | **平台改写了 greeting** | 改策略：文案池只能靠"账号级招呼语设置"生效 |
| 什么都没有 | 写进去了但读不回来 | 先排查 chatmsg，再谈文案 |

## 用法

```bash
# 1) 先看计划，不发任何请求（默认）
python -m app.tools.probe_greet_text --db data/jobpilot-new.db

# 2) 确认无误后真发一条（**这是写操作**，会真打招呼）
python -m app.tools.probe_greet_text --db data/jobpilot-new.db --confirm-send
```

注意：跑之前必须已经 `recover --confirmed` 清掉风控锁，否则上游会直接回
`ENVIRONMENT_RISK_LOCKED`，工具会如实报告而不是假装成功。
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from app import models
from app.config import load_config
from app.core import idempotency as idem
from app.core import search_targets

MARKER = "合同审核和法务支持"

#: 平台默认招呼语的特征片段（账号从没设过"打招呼语"时，BOSS 会发这句）。
PLATFORM_DEFAULT_HINTS = ("我想和您聊聊这个职位", "我想和你聊聊这个职位", "打扰了，我想和您")


def _marker_text() -> str:
    """复验用的文案。

    为什么不用「JP标记1234」这种显眼暗号：这条会**真发给一个真实 HR**，写成机器口令很难看，
    而且没必要 —— 平台的默认招呼语是"您好，打扰了，我想和您聊聊这个职位"，任何不一样的人话
    都能区分。这里用一句自然、但一定不在 25 条池子里的句子。
    """
    return f"您好，我这边主要做{MARKER}，看到这个岗位想了解下日常工作。"


def marker_prefix() -> str:
    return MARKER


def _claimed_job_ids(db) -> set[str]:
    rows = db.execute("SELECT DISTINCT target FROM actions WHERE action_type = 'greet'")
    return {str(r[0]) for r in rows if r[0]}


def _search_urls(cfg: dict[str, Any], override: str | None) -> list[str]:
    """要扫的搜索 URL 列表：优先 `--url`，否则 config 里的全部关键词。"""
    if override:
        return [override]
    target = cfg.get("target") or {}
    # 与 pacer 共用同一套展开逻辑（keywords × cities），避免两边口径不一致
    return search_targets.build_urls(target)


def _find_target(
    adapter, claimed: set[str], urls: list[str], pages: int, max_searches: int = 4
) -> dict[str, Any] | None:
    """按"关键词 × 页码"找第一个没打过、且有 security_id 的岗位。

    【2026-11-12 修】原来只看第一页：第一页早已被 123 条招呼占满，
    复验直接空手而归（"第一页没有可打的岗位"）。现在扫全部关键词 × 前 N 页，
    并且**先换页再换词** —— 和 pacer 的轮转顺序保持一致，方便对照。
    """
    total = 0
    searches = 0
    for page in range(1, max(pages, 1) + 1):
        for url in urls:
            # 刚解风控，不许一口气打十几次 search：给一个硬上限，到点就停
            if searches >= max_searches:
                print(f"  已达搜索次数上限 {max_searches}（刚解风控，不连打），停止")
                return None
            searches += 1
            jobs = (
                adapter.search("法务", cities=[], page=page, url=url)
                if url
                else adapter.search("法务", cities=[], page=page)
            )
            total += len(jobs)
            keyword = url.split("query=")[-1][:12] if url else "默认"
            for job in jobs:
                job_id = str(job.get("job_id") or "")
                if job_id and job_id not in claimed and job.get("security_id"):
                    print(f"  命中：第 {page} 页 · {keyword} · 累计扫到 {total} 条")
                    return job
            print(f"  第 {page} 页 · {keyword}：{len(jobs)} 条，都打过了")
    print(f"  扫完 {len(urls)} 个关键词 × {pages} 页，共 {total} 条岗位，没有可打的")
    return None


def _uid_of_job(adapter, job_id: str) -> str | None:
    """greet 的成功信封不返回 uid（补页 §7.2）→ 只能从 chat() 列表里按 job_id 反查。"""
    for conversation in adapter.chat():
        if str(conversation.get("job_id") or "") == job_id:
            uid = conversation.get("uid")
            return str(uid) if uid else None
    return None


def _last_out_text(adapter, uid: str) -> tuple[str | None, list[dict[str, Any]]]:
    messages = adapter.chatmsg(uid, raw=True)
    for message in reversed(messages):
        if str(message.get("direction") or "") == "out":
            return str(message.get("content") or ""), messages
    return None, messages


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="复验打招呼文案是否被平台采用")
    parser.add_argument("--db", default=None, help="数据库路径（默认按账号推导）")
    parser.add_argument("--account", default=None, help="用哪个账号跑（默认活动账号）")
    parser.add_argument("--url", default=None, help="只扫这个搜索 URL（默认扫 config 的全部关键词）")
    parser.add_argument("--pages", type=int, default=1, help="每个关键词扫前几页（默认 1）")
    parser.add_argument("--max-searches", type=int, default=4, help="本次最多搜几次（默认 4，刚解风控别连打）")
    parser.add_argument(
        "--confirm-send",
        action="store_true",
        help="真的发出这一条招呼（不加只打印计划，不发任何请求）",
    )
    args = parser.parse_args(argv)

    if args.account:
        import os

        os.environ["JOBPILOT_ACCOUNT"] = str(args.account)
    cfg = load_config()

    # 没指定 --db 就按账号推导：A 号探针必须用 A 号的库（claim 与额度都独立）
    if not args.db:
        from app.core import accounts as accounts_mod

        account = accounts_mod.active_account(cfg)
        args.db = str(account["db"]) if account else "data/jobpilot-new.db"

    text = _marker_text()
    print(f"标记文案：{text}")

    if not args.confirm_send:
        print("\n[演练] 未加 --confirm-send，不发送任何请求。")
        print("真跑会做这几步：扫关键词页面 → 挑 1 个没打过的岗位 → greet(复验文案) → chat() 反查 uid → chatmsg 读回比对")
        return 0

    # 每日探测配额（2026-11-13）：招呼探针比搜索探针更贵，必须一起计数。
    from app.core import probe_budget

    limit = int((cfg.get("safety") or {}).get("probe_quota_per_day") or probe_budget.DEFAULT_LIMIT)
    allowed, budget_state = probe_budget.consume("greet_probe", limit=limit)
    if not allowed:
        print(f"\n今日探测配额已用完：{probe_budget.describe(budget_state)}")
        print("  真要强跑，先想清楚：这条招呼会发给一个真实 HR。")
        return 6
    print(f"{probe_budget.describe(budget_state)}")

    # 延迟 import：演练模式完全离线，不该碰上游依赖
    from app.adapters.boss import BossAdapter

    adapter = BossAdapter(cfg)
    db = models.connect(args.db)
    try:
        claimed = _claimed_job_ids(db)
        urls = _search_urls(cfg, args.url)
        print(f"扫描范围：{len(urls)} 个关键词 × 前 {args.pages} 页")
        target = _find_target(adapter, claimed, urls, args.pages, args.max_searches)
        if target is None:
            print("第一页没有可打的岗位（都打过了，或搜索被拒）：", adapter.last_error)
            return 2

        job_id = str(target["job_id"])
        print(f"目标岗位：{target.get('title')} @ {target.get('company')}（{job_id}）")

        # 【重要】先占位再发：这条是真招呼，必须和 pacer 共用同一本账。
        # 不占位的话，pacer 之后还会把这个岗位当成"没打过"重新挑出来
        # （平台侧会回 ALREADY_GREETED，白占一个额度位）。
        key = idem.greet_key(job_id)
        if not idem.try_claim(db, key, "greet", job_id):
            print("这个岗位已经被占位过（pacer 打过或上次复验打过），换个岗位重跑")
            return 2

        result = adapter.greet(str(target["security_id"]), job_id, text)
        print("greet 返回：", json.dumps(result, ensure_ascii=False)[:200])
        if not result.get("sent"):
            idem.finish_claim(db, key, "failed", result)
            print("打招呼没有成功，先解决这个再谈文案：", adapter.last_error)
            return 3
        # 记成 ok：既让 pacer 跳过它，也让今天的额度如实 +1（它确实是一条真招呼）
        idem.finish_claim(db, key, "ok", {"code": 0, "tpl": "probe_marker"})

        uid = _uid_of_job(adapter, job_id)
        if not uid:
            print("招呼发出去了，但在 chat() 列表里找不到这个岗位的会话（可能还没同步）：")
            print("  等 1 分钟重跑本工具，或手工在浏览器里看一下那句文案")
            return 4

        got, messages = _last_out_text(adapter, uid)
        print(f"\n会话 uid={uid}，共 {len(messages)} 条消息")
        print("最后一条 out：", repr(got))

        got_text = (got or "").strip()
        if got_text and text.strip() in got_text:
            print("\n✅ 结论 A：平台采用了我们 API 传的文案（标记命中）→ 文案池有效")
            return 0

        # 平台默认语（账号没设过招呼语时用的那句）
        if got_text and any(hint in got_text for hint in PLATFORM_DEFAULT_HINTS):
            print("\n⚠️ 结论 B：会话里是**平台默认招呼语** → API 文案被丢弃，账号设置也没设")
            print("   下一步：去手机 App 设「打招呼语」，再跑一次本工具")
            return 1

        if got_text:
            # 【2026-11-12 新增】第三种情形，也是真实遇到的：用的是**账号级招呼语设置**。
            # 这是好消息 —— 说明第一句是可控的，只是控制点在账号设置，不在 API 参数。
            print("\n✅ 结论 C：会话里是**账号级「打招呼语」**（不是我们传的，也不是平台默认语）")
            print(f"   实际显示：{got_text[:80]}")
            print("   → 第一句可控，但控制点是账号设置（只能设一句，无法按岗位变化）")
            print("   → 个性化只能放在'招呼之后的聊天消息'上（需要 send 通道，上游未提供）")
            return 0

        print("\n⚠️ 读不到 out 消息：会话里没有我方发言，先排查 chatmsg / 会话同步")
        return 5
    finally:
        db.close()


if __name__ == "__main__":
    sys.exit(main())

"""幂等 + 两个 UNIQUE 约束（任务书 §5.3 / §8，≥4 条）。

核心断言：同一个 key 只能抢到一次。**宁可漏发，不可重发。**
"""

from __future__ import annotations

import sqlite3
import threading

import pytest

from app import models
from app.core import idempotency as idem


def test_first_claim_returns_true(conn):
    """首次 claim 返回 True，并在 actions 表留一条 pending。"""
    key = idem.greet_key("job_001")

    assert idem.try_claim(conn, key, action_type="greet", target="job_001") is True

    claim = idem.get_claim(conn, key)
    assert claim is not None
    assert claim["idempotency_key"] == "greet:job_001"      # 键用 job_id，不是 uid
    assert claim["action_type"] == "greet"
    assert claim["target"] == "job_001"
    assert claim["status"] == "pending"


def test_second_claim_returns_false_and_keeps_single_row(conn):
    """二次 claim 返回 False，且表里仍然只有一条记录（UNIQUE 兜住了）。"""
    key = idem.greet_key("job_001")

    assert idem.try_claim(conn, key, "greet", "job_001") is True
    assert idem.try_claim(conn, key, "greet", "job_001") is False
    assert idem.try_claim(conn, key, "greet", "job_001") is False

    assert idem.count_claims(conn) == 1
    # 首次认领的状态不能被后来的重复调用改写
    assert idem.get_claim(conn, key)["status"] == "pending"


def test_concurrent_claim_has_exactly_one_winner(db_path):
    """4 个线程（各自独立连接）同时抢同一个 key，只有一个 True。

    这条是"不要 SELECT 再 INSERT"的回归测试：先查后插会两个都拿到 True。
    """
    models.init_db(db_path).close()
    key = idem.greet_key("job_002")
    results: list[bool] = []
    results_lock = threading.Lock()
    barrier = threading.Barrier(4)

    def worker() -> None:
        connection = models.connect(db_path)
        try:
            barrier.wait(timeout=10)
            won = idem.try_claim(connection, key, "greet", "job_002")
        finally:
            connection.close()
        with results_lock:
            results.append(won)

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)

    assert len(results) == 4
    assert results.count(True) == 1

    connection = models.connect(db_path)
    try:
        assert idem.count_claims(connection) == 1
        assert idem.get_claim(connection, key)["status"] == "pending"
    finally:
        connection.close()


def test_different_keys_do_not_interfere(conn):
    """不同 key 互不影响；同一 job 的 greet 与 exchange 也是两条独立动作。"""
    assert idem.try_claim(conn, idem.greet_key("job_001"), "greet", "job_001") is True
    assert idem.try_claim(conn, idem.greet_key("job_002"), "greet", "job_002") is True
    assert idem.try_claim(conn, idem.exchange_key("fake_job_002"), "exchange", "fake_job_002") is True
    assert idem.try_claim(conn, idem.reply_key("fake_job_001", "m-1"), "reply", "fake_job_001") is True

    assert idem.count_claims(conn) == 4

    # 已经认领过的仍然拒绝
    assert idem.try_claim(conn, idem.greet_key("job_001"), "greet", "job_001") is False
    # 回填状态不影响其它 key
    idem.finish_claim(conn, idem.greet_key("job_001"), "ok", {"sent": True})
    assert idem.get_claim(conn, idem.greet_key("job_001"))["status"] == "ok"
    assert idem.get_claim(conn, idem.greet_key("job_002"))["status"] == "pending"


def test_db_opens_wal_and_messages_unique(conn):
    """骨架批的两个硬要求：WAL 打开、messages(uid, platform_mid) 唯一。"""
    mode = conn.execute("PRAGMA journal_mode").fetchone()[0]
    assert str(mode).lower() == "wal"

    row = {"uid": "fake_job_001", "platform_mid": "m-1", "direction": "in", "msg_type": "text",
           "content": "你好", "created_at": models.utcnow()}
    insert = (
        "INSERT INTO messages (uid, platform_mid, direction, msg_type, content, created_at)"
        " VALUES (:uid, :platform_mid, :direction, :msg_type, :content, :created_at)"
    )
    conn.execute(insert, row)
    conn.commit()

    # 同一批消息读两次不能重复入库。
    # 【F3】必须捕 IntegrityError 并确认是 UNIQUE 违规。写成 except Exception 时，
    # SQL 写错、表名拼错、连接断了都会让这个测试"绿"，等于没测。
    with pytest.raises(sqlite3.IntegrityError) as excinfo:
        conn.execute(insert, row)
    assert "UNIQUE" in str(excinfo.value).upper()
    conn.rollback()

    total = conn.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
    assert total == 1


def test_messages_null_platform_mid_is_rejected(conn):
    """【F1】`platform_mid` 必须 NOT NULL。

    SQLite 里 NULL != NULL，允许 NULL 时 `UNIQUE(uid, platform_mid)` 完全失效：
    同一条消息能无限重复入库，而 `reply:{uid}:{platform_mid}` 幂等键会退化成同一个
    `reply:u1:None`——该拦的拦不住，直接违背"宁可漏发不可重发"。

    **已知代价（交付说明 §7 待确认 5）**：拿不到 mid 的消息**不入库**，即"丢消息"
    而不是"存重复"。Phase 3 真机若 `chatmsg` 拿不到稳定 mid，回来重新裁定，
    **不许编假 mid**。
    """
    row = {
        "uid": "u1",
        "platform_mid": None,
        "direction": "in",
        "msg_type": "text",
        "content": "x",
        "created_at": models.utcnow(),
    }
    with pytest.raises(sqlite3.IntegrityError) as excinfo:
        conn.execute(
            "INSERT INTO messages (uid, platform_mid, direction, msg_type, content, created_at)"
            " VALUES (:uid, :platform_mid, :direction, :msg_type, :content, :created_at)",
            row,
        )
    assert "NOT NULL" in str(excinfo.value).upper()

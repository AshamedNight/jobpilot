"""`models.init_db` 的旧库列迁移 —— 1 条（补丁 §1.6）。

**必须用"已存在且不含 gap_flags 的旧表"来测**：临时新库会走
`CREATE TABLE IF NOT EXISTS` 建出带 `gap_flags` 的新表，这个 bug 根本抓不到。
用户 Windows 上现存的 `data/jobpilot.db` 就是旧表。
"""

from __future__ import annotations

import sqlite3

from app import models

#: P2-C 时期的 jobs 表（17 列，**没有** gap_flags）。
_OLD_JOBS_SCHEMA = """
CREATE TABLE jobs (
    job_id        TEXT PRIMARY KEY,
    security_id   TEXT NOT NULL,
    title         TEXT NOT NULL,
    company       TEXT NOT NULL,
    salary        TEXT,
    city          TEXT,
    industry      TEXT,
    hr_name       TEXT,
    hr_active     TEXT,
    company_scale TEXT,
    publish_time  TIMESTAMP,
    jd_text       TEXT,
    welfare       TEXT,
    match_score   REAL,
    match_reason  TEXT,
    first_seen    TIMESTAMP,
    raw_json      TEXT
);
"""


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def test_init_db_migrates_old_jobs_table(tmp_path):
    """旧库（无 `gap_flags`）跑 `init_db()` → 列被补上、旧行还在、且幂等。"""
    db_path = tmp_path / "old.db"

    # 1) 造一个 P2-C 时代的旧库，并塞一行老数据
    old = sqlite3.connect(str(db_path))
    old.executescript(_OLD_JOBS_SCHEMA)
    old.execute(
        "INSERT INTO jobs (job_id, security_id, title, company) VALUES (?, ?, ?, ?)",
        ("job_old", "sec_old", "法务专员", "老公司"),
    )
    old.commit()
    assert "gap_flags" not in _columns(old, "jobs")
    old.close()

    # 2) 升级后第一次启动：自动补列，不需要用户删库
    conn = models.init_db(db_path)
    try:
        assert "gap_flags" in _columns(conn, "jobs"), "旧库必须补上 gap_flags 列"

        # 老数据不能丢；新列为 NULL，读出来是 []
        row = conn.execute("SELECT job_id, title, gap_flags FROM jobs").fetchone()
        assert row["job_id"] == "job_old"
        assert row["title"] == "法务专员"
        assert row["gap_flags"] is None
        assert models.parse_gap_flags(row["gap_flags"]) == []

        # `/api/jobs` 走的就是这条路：带上 gap_flags 后必须能查出来（旧库原本会 500）
        items = models.list_jobs(conn)
        assert items[0]["job_id"] == "job_old"
        assert items[0]["gap_flags"] == []
    finally:
        conn.close()

    # 3) 幂等：第二次 init_db 不再 ALTER、不报错、旧行还在
    again = models.init_db(db_path)
    try:
        assert "gap_flags" in _columns(again, "jobs")
        assert again.execute("SELECT COUNT(*) AS n FROM jobs").fetchone()["n"] == 1
    finally:
        again.close()


# --------------------------------------------------------------------------- #
# 【P2-B §11 第 7–8 条】detail 账本 + JD 回填
# --------------------------------------------------------------------------- #
def _ledger_job(job_id: str) -> dict:
    """一条最小岗位（`jobs.security_id` NOT NULL，必须给）。"""
    return {
        "job_id": job_id,
        "security_id": f"sec_{job_id}",
        "title": "法务专员",
        "company": "某公司",
        "jd_text": "",
    }


def test_detail_ledger_claim_and_count_today(conn):
    """`detail:{job_id}:{YYYYMMDD}` 复用 actions 表：UNIQUE 天然防重、按类型分开计数。"""
    from datetime import datetime, timezone

    from app.core import idempotency as idem

    models.upsert_jobs(conn, [_ledger_job("job_a"), _ledger_job("job_b")])
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    key = f"detail:job_a:{today}"

    # 第一次占位成功
    assert idem.try_claim(conn, key, "detail", "job_a") is True
    idem.finish_claim(conn, key, "ok", {"code": 0})

    # 同一天同一岗再来一次 → UNIQUE 挡住（白拿"同岗同日最多一次"）
    assert idem.try_claim(conn, key, "detail", "job_a") is False

    # 另一个岗不受影响
    assert idem.try_claim(conn, f"detail:job_b:{today}", "detail", "job_b") is True

    # 计数按 action_type 分：detail 不该污染"今日已打招呼"
    assert models.count_actions_today(conn, "detail") == 2
    assert models.count_actions_today(conn, "greet") == 0

    # 跨天键不同 → 允许重试（不需要新列）
    assert idem.try_claim(conn, "detail:job_a:19990101", "detail", "job_a") is True

    # 账本内容可查
    row = idem.get_claim(conn, key)
    assert row["action_type"] == "detail"
    assert row["status"] == "ok"


def test_update_job_detail_writes_jd_and_gap_flags(conn):
    """`update_job_detail()`：UPDATE 生效、`gap_flags` 落 JSON 文本、空正文也照写。"""
    models.upsert_jobs(conn, [_ledger_job("job_c")])

    models.update_job_detail(conn, "job_c", "岗位职责：审核合同，要求律师执业证", ["要求持证"])

    row = models.get_job(conn, "job_c")
    assert row["jd_text"] == "岗位职责：审核合同，要求律师执业证"
    # 落库形态是 JSON 文本（不是 Python repr），读出来解析成 list
    assert isinstance(row["gap_flags"], str)
    assert models.parse_gap_flags(row["gap_flags"]) == ["要求持证"]

    # "抓到了但没有正文"是合法终态：空串照样写进去，与"从没抓过"区分开
    models.update_job_detail(conn, "job_c", "", [])
    row = models.get_job(conn, "job_c")
    assert row["jd_text"] == ""
    assert row["gap_flags"] == "[]"

    # gap_flags=None → NULL，读出为 []
    models.update_job_detail(conn, "job_c", "有正文", None)
    row = models.get_job(conn, "job_c")
    assert row["gap_flags"] is None
    assert models.parse_gap_flags(row["gap_flags"]) == []
    assert row["jd_text"] == "有正文"

    # 列数不变：本批不新增列、不迁移（§2 边界）
    assert len({r[1] for r in conn.execute("PRAGMA table_info(jobs)")}) == 18

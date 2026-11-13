"""SQLite 六张表 + WAL（架构 §5，任务书 §4）。

用标准库 `sqlite3`，不引 ORM（任务书 §11：六张表用 sqlite3 足够）。

两个唯一约束是本阶段的核心：
  - `actions.idempotency_key` UNIQUE                → 防重发（宁可漏发，不可重发）
  - `messages(uid, platform_mid)` UNIQUE            → 同一批消息读两次不重复入库
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

PROJECT_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_DB_PATH = PROJECT_ROOT / "data" / "jobpilot.db"


def utcnow() -> str:
    """统一用 ISO8601 UTC 字符串存时间（SQLite 没有原生 datetime）。"""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


# --------------------------------------------------------------------------- #
# 六张表：严格照抄架构 §5
# --------------------------------------------------------------------------- #
SCHEMA = """
-- 岗位
CREATE TABLE IF NOT EXISTS jobs (
    job_id        TEXT PRIMARY KEY,   -- 平台内部 ID，不入日志
    security_id   TEXT NOT NULL,      -- 【探针发现2】greet 必需参数，敏感，禁止导出
    title         TEXT NOT NULL,
    company       TEXT NOT NULL,
    salary        TEXT,
    city          TEXT,
    industry      TEXT,
    hr_name       TEXT,
    hr_active     TEXT,               -- 枚举：online/today/3d/week/2w/month/half_year/stale/unknown
    company_scale TEXT,
    publish_time  TIMESTAMP,
    jd_text       TEXT,
    welfare       TEXT,
    match_score   REAL,               -- 本地打分
    match_reason  TEXT,               -- 为什么匹配（给 LLM 当素材）
    first_seen    TIMESTAMP,
    raw_json      TEXT,               -- 原始信封，排查用，不导出
    gap_flags     TEXT                -- 【P2-A】profile_mode=demo 的提示项，JSON 数组文本；只提示不参与打分
);

-- 会话（本项目的心脏）
CREATE TABLE IF NOT EXISTS conversations (
    uid           TEXT PRIMARY KEY,
    job_id        TEXT REFERENCES jobs(job_id),
    hr_name       TEXT,
    company       TEXT,
    state         TEXT NOT NULL,
    greeted_at    TIMESTAMP,
    last_msg_at   TIMESTAMP,
    last_msg_from TEXT,               -- 'hr' | 'me'
    reply_count   INTEGER DEFAULT 0,
    exchange_requested_at TIMESTAMP,
    exchange_incoming_at  TIMESTAMP,
    exchange_agreed_at    TIMESTAMP,
    wechat_id     TEXT,               -- 敏感，导出默认排除
    wechat_source TEXT,               -- 'chatmsg' | 'manual'
    note          TEXT,
    updated_at    TIMESTAMP
);

-- 消息（去重用）
CREATE TABLE IF NOT EXISTS messages (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    uid           TEXT NOT NULL,
    platform_mid  TEXT NOT NULL,      -- 【F1】NULL 会让 UNIQUE 失效：NULL != NULL，同一条消息能无限重复入库
    direction     TEXT NOT NULL,      -- 'in' | 'out'
    msg_type      TEXT NOT NULL,      -- 'text'|'card_exchange'|'card_job'|'card_resume'|'system'
    content       TEXT,
    raw_json      TEXT,
    created_at    TIMESTAMP,
    UNIQUE(uid, platform_mid)         -- 天然幂等
);

-- 动作日志（防重发，最重要的一张表）
CREATE TABLE IF NOT EXISTS actions (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    action_type     TEXT NOT NULL,    -- 'greet'|'reply'|'exchange'|'detail'
    target          TEXT NOT NULL,
    idempotency_key TEXT NOT NULL UNIQUE,
    status          TEXT NOT NULL,    -- 'pending'|'ok'|'failed'|'unconfirmed'
    response_json   TEXT,
    created_at      TIMESTAMP
);

-- LLM 调用记录（控成本 + 复盘话术效果）
CREATE TABLE IF NOT EXISTS llm_calls (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    purpose      TEXT,                -- 'opener'|'reply'|'classify'
    uid          TEXT,
    model        TEXT,
    prompt_hash  TEXT,
    response     TEXT,
    tokens_in    INTEGER,
    tokens_out   INTEGER,
    created_at   TIMESTAMP
);

-- 每轮运行记录（排障）
CREATE TABLE IF NOT EXISTS runs (
    run_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    kind        TEXT,                 -- 'sweep'|'followup'|'exchange'
    started_at  TIMESTAMP,
    ended_at    TIMESTAMP,
    stats_json  TEXT,
    error       TEXT
);
"""

# jobs 里**禁止**对外输出的列（架构 §1.4 / §5：security_id 属敏感）
SENSITIVE_JOB_COLUMNS = ("security_id", "raw_json")

# API 暴露的岗位字段（不含 security_id / raw_json）
JOB_PUBLIC_COLUMNS = (
    "job_id",
    "title",
    "company",
    "salary",
    "city",
    "industry",
    "hr_name",
    "hr_active",
    "company_scale",
    "publish_time",
    "jd_text",
    "welfare",
    "match_score",
    "match_reason",
    "first_seen",
    "gap_flags",  # 【P2-A】JSON 数组文本，读出时解析成 list
)


# --------------------------------------------------------------------------- #
# 连接 / 建表
# --------------------------------------------------------------------------- #
def connect(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """开连接，并打开 WAL（任务书 §4.4）。"""
    path = Path(db_path)
    if path.parent and str(path.parent):
        path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA busy_timeout=5000")
    return conn


def _ensure_job_columns(conn: sqlite3.Connection) -> None:
    """旧库补列（补丁 §1.6）。

    `SCHEMA` 是 `CREATE TABLE IF NOT EXISTS`——**表已存在时整条 CREATE 被跳过**，
    新增的列不会补上。用户 Windows 上已有的 `data/jobpilot.db` 因此会在
    `/api/jobs` 直接 500（`JOB_PUBLIC_COLUMNS` 一带上 `gap_flags` 就炸），
    而 `pytest` 全绿（测试用临时新库）。

    幂等：第二次进来 `gap_flags` 已在，不再 ALTER，也不报错。
    """
    have = {row[1] for row in conn.execute("PRAGMA table_info(jobs)")}
    if "gap_flags" not in have:
        conn.execute("ALTER TABLE jobs ADD COLUMN gap_flags TEXT")
        conn.commit()


def init_db(db_path: str | Path = DEFAULT_DB_PATH) -> sqlite3.Connection:
    """建库建表（幂等，可重复调用），返回带 WAL 的连接。"""
    conn = connect(db_path)
    conn.executescript(SCHEMA)
    _ensure_job_columns(conn)  # 【补丁 §1.6】旧库补 gap_flags，不需要用户删库
    conn.commit()
    return conn


# --------------------------------------------------------------------------- #
# jobs
# --------------------------------------------------------------------------- #
def upsert_jobs(conn: sqlite3.Connection, jobs: list[dict[str, Any]]) -> int:
    """写入一批岗位。已存在的 job_id 不覆盖（first_seen 保留首见时间）。返回新增条数。"""
    inserted = 0
    now = utcnow()
    for job in jobs:
        cursor = conn.execute(
            """
            INSERT INTO jobs (
                job_id, security_id, title, company, salary, city, industry,
                hr_name, hr_active, company_scale, publish_time, jd_text, welfare,
                match_score, match_reason, first_seen, raw_json, gap_flags
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(job_id) DO NOTHING
            """,
            (
                job.get("job_id"),
                job.get("security_id"),
                job.get("title"),
                job.get("company"),
                job.get("salary"),
                job.get("city"),
                job.get("industry"),
                job.get("hr_name"),
                job.get("hr_active"),
                job.get("company_scale"),
                job.get("publish_time"),
                job.get("jd_text"),
                job.get("welfare"),
                job.get("match_score"),
                job.get("match_reason"),
                job.get("first_seen") or now,
                job.get("raw_json"),
                job.get("gap_flags"),
            ),
        )
        inserted += cursor.rowcount if cursor.rowcount and cursor.rowcount > 0 else 0
    conn.commit()
    return inserted


def list_jobs(conn: sqlite3.Connection, order_by_score: bool = True) -> list[dict[str, Any]]:
    """岗位列表。**不返回** security_id / raw_json（敏感，禁止导出）。

    `gap_flags` 落库是 JSON 数组文本，读出时解析成 list；旧行为 NULL → `[]`。
    """
    columns = ", ".join(JOB_PUBLIC_COLUMNS)
    order = "match_score IS NULL, match_score DESC, first_seen ASC" if order_by_score else "first_seen ASC"
    rows = conn.execute(f"SELECT {columns} FROM jobs ORDER BY {order}").fetchall()
    items = [dict(row) for row in rows]
    for item in items:
        item["gap_flags"] = parse_gap_flags(item.get("gap_flags"))
    return items


def parse_gap_flags(value: Any) -> list[str]:
    """`jobs.gap_flags`（TEXT）→ list[str]。NULL / 坏 JSON / 非数组一律给 `[]`。"""
    if not value:
        return []
    if isinstance(value, list):
        return [str(flag) for flag in value]
    try:
        data = json.loads(str(value))
    except (ValueError, TypeError):
        return []
    if not isinstance(data, list):
        return []
    return [str(flag) for flag in data]


def get_job(conn: sqlite3.Connection, job_id: str) -> dict[str, Any] | None:
    """内部取岗位（含 security_id），只给 core / adapter 用，不给 API 用。"""
    row = conn.execute("SELECT * FROM jobs WHERE job_id = ?", (job_id,)).fetchone()
    return dict(row) if row else None


def update_match_result(conn: sqlite3.Connection, job_id: str, score: float, reason: str) -> None:
    """回写本地打分结果（`/api/run/sweep` 打分阶段调用，`reason` 是 reasons 拼成的可读串）。"""
    conn.execute(
        "UPDATE jobs SET match_score = ?, match_reason = ? WHERE job_id = ?",
        (score, reason, job_id),
    )
    conn.commit()


def update_job_detail(
    conn: sqlite3.Connection,
    job_id: str,
    jd_text: str,
    gap_flags: Any = None,
) -> None:
    """【P2-B §9 授权清单第 2 条】回填 JD 正文与重算后的 `gap_flags`。

    **为什么必须有这个函数**：`upsert_jobs()` 是 `ON CONFLICT DO NOTHING`——
    已存在的 `job_id` 整行不动，所以"补正文"只能靠 UPDATE。`inserted` 的口径
    因此一个字都不用改（§14 第二条）。

    两个刻意的选择：

    - `jd_text` 为空串时**照样写**。"抓到了，只是这岗没写 JD"是合法终态（§6.6），
      不写就等于把"已经抓过、确实没有"和"从没抓过"混成一件事。
    - `gap_flags` 接受 `list` 或**已序列化的 JSON 文本**；`None` → 落 NULL
      （`parse_gap_flags()` 读出来是 `[]`）。落库前 `json.dumps` 是 P2-A 的既定口径
      （`welfare` / `raw_json` 同款），否则 sqlite 直接 `ProgrammingError`。
    """
    if gap_flags is None:
        flags_json = None
    elif isinstance(gap_flags, str):
        flags_json = gap_flags
    else:
        flags_json = json.dumps([str(flag) for flag in gap_flags], ensure_ascii=False)

    conn.execute(
        "UPDATE jobs SET jd_text = ?, gap_flags = ? WHERE job_id = ?",
        (str(jd_text or ""), flags_json, job_id),
    )
    conn.commit()


# --------------------------------------------------------------------------- #
# conversations / messages
# --------------------------------------------------------------------------- #
def list_conversations(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """会话列表。

    【F2】**不返回** `wechat_id` / `wechat_source`：微信号是敏感个人信息
    （架构 §1.4 / §5），只能出现在详情接口 `GET /api/conversations/{uid}`。
    列表一次性把所有号码吐给前端，是 Phase 4 一有号码就会炸的坑。
    """
    rows = conn.execute(
        """
        SELECT uid, job_id, hr_name, company, state, greeted_at, last_msg_at,
               last_msg_from, reply_count, updated_at
        FROM conversations
        ORDER BY COALESCE(last_msg_at, greeted_at, updated_at) DESC
        """
    ).fetchall()
    return [dict(row) for row in rows]


def get_conversation(conn: sqlite3.Connection, uid: str) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM conversations WHERE uid = ?", (uid,)).fetchone()
    return dict(row) if row else None


def count_conversations_with_reply(conn: sqlite3.Connection) -> int:
    """有过 HR 回复的会话数（漏斗中间指标）。"""
    row = conn.execute("SELECT COUNT(*) AS n FROM conversations WHERE reply_count > 0").fetchone()
    return int(row["n"]) if row else 0


def count_conversations_with_wechat(conn: sqlite3.Connection) -> int:
    """拿到微信号的会话数（漏斗最终指标）。

    只返回**数量**，不返回号码本身 —— 号码只在详情接口单独取（架构 §1.4）。
    """
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM conversations WHERE wechat_id IS NOT NULL AND wechat_id <> ''"
    ).fetchone()
    return int(row["n"]) if row else 0


def count_conversations_in_state(conn: sqlite3.Connection, state: str) -> int:
    """处于某状态的会话数（例如 REJECTED 计数）。"""
    row = conn.execute(
        "SELECT COUNT(*) AS n FROM conversations WHERE state = ?", (state,)
    ).fetchone()
    return int(row["n"]) if row else 0


def list_messages(conn: sqlite3.Connection, uid: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        SELECT id, uid, platform_mid, direction, msg_type, content, created_at
        FROM messages WHERE uid = ? ORDER BY id ASC
        """,
        (uid,),
    ).fetchall()
    return [dict(row) for row in rows]


def upsert_conversation(
    conn: sqlite3.Connection,
    uid: str,
    job_id: str | None,
    hr_name: str | None,
    company: str | None,
    state: str,
) -> None:
    """建/更新一条会话。

    `state` 必须由 `app.core.state_machine` 给出（`State.XXX.value` 或 `transition()` 的结果），
    **不要在调用处手写字面量拼状态**。已存在的 uid 不覆盖 job/hr/company 的空值。
    """
    now = utcnow()
    conn.execute(
        """
        INSERT INTO conversations (uid, job_id, hr_name, company, state, updated_at)
        VALUES (?, ?, ?, ?, ?, ?)
        ON CONFLICT(uid) DO UPDATE SET
            job_id   = COALESCE(excluded.job_id, conversations.job_id),
            hr_name  = COALESCE(excluded.hr_name, conversations.hr_name),
            company  = COALESCE(excluded.company, conversations.company),
            state    = excluded.state,
            updated_at = excluded.updated_at
        """,
        (uid, job_id, hr_name, company, state, now),
    )
    conn.commit()


# `set_conversation_state` 允许回填的字段（白名单：拼 SQL 的列名不能来自调用方自由输入）
CONVERSATION_MUTABLE_FIELDS: tuple[str, ...] = (
    "greeted_at",
    "last_msg_at",
    "last_msg_from",
    "reply_count",
    "exchange_requested_at",
    "exchange_incoming_at",
    "exchange_agreed_at",
    "wechat_id",
    "wechat_source",
    "note",
)


def set_conversation_state(
    conn: sqlite3.Connection,
    uid: str,
    state: str,
    **fields: Any,
) -> None:
    """改会话状态并回填字段。`state` 必须是 `state_machine.transition()` 的返回值。"""
    unknown = set(fields) - set(CONVERSATION_MUTABLE_FIELDS)
    if unknown:
        raise ValueError(f"set_conversation_state 不接受字段：{sorted(unknown)}")

    assignments = ["state = ?", "updated_at = ?"]
    params: list[Any] = [state, utcnow()]
    for column, value in fields.items():
        assignments.append(f"{column} = ?")
        params.append(value)
    params.append(uid)
    conn.execute(
        f"UPDATE conversations SET {', '.join(assignments)} WHERE uid = ?",
        params,
    )
    conn.commit()


def insert_message(
    conn: sqlite3.Connection,
    uid: str,
    platform_mid: str,
    direction: str,
    msg_type: str,
    content: str | None,
    raw_json: dict[str, Any] | None = None,
    created_at: str | None = None,
) -> bool:
    """插一条消息，靠 UNIQUE(uid, platform_mid) 去重。返回是否**真的新增**。

    【F1】`platform_mid` 为空直接不入库：拿不到平台消息 ID 的消息宁可不存，
    也不许编一个假 mid（否则 UNIQUE 失效、幂等键退化成 `reply:uid:None`）。
    """
    import json

    if not platform_mid:
        return False
    cursor = conn.execute(
        """
        INSERT OR IGNORE INTO messages
            (uid, platform_mid, direction, msg_type, content, raw_json, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        """,
        (
            uid,
            platform_mid,
            direction,
            msg_type,
            content,
            json.dumps(raw_json, ensure_ascii=False) if raw_json is not None else None,
            created_at or utcnow(),
        ),
    )
    conn.commit()
    return bool(cursor.rowcount and cursor.rowcount > 0)


def record_llm_call(
    conn: sqlite3.Connection,
    purpose: str,
    uid: str | None,
    model: str | None,
    prompt_hash: str | None,
    response: str | None,
    tokens_in: int = 0,
    tokens_out: int = 0,
) -> None:
    """记一条 LLM 调用（控成本 + 复盘话术）。只落 `prompt_hash`，不落 prompt 原文。"""
    conn.execute(
        """
        INSERT INTO llm_calls
            (purpose, uid, model, prompt_hash, response, tokens_in, tokens_out, created_at)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (purpose, uid, model, prompt_hash, response, tokens_in, tokens_out, utcnow()),
    )
    conn.commit()


def list_pending_manual(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    """待人工处理队列。Phase 1 没有写入路径，正常结果是空列表（结构要有）。"""
    rows = conn.execute(
        """
        SELECT uid, job_id, hr_name, company, state, last_msg_at, note
        FROM conversations WHERE state = 'PENDING_MANUAL' ORDER BY last_msg_at DESC
        """
    ).fetchall()
    return [dict(row) for row in rows]


# --------------------------------------------------------------------------- #
# actions 统计
# --------------------------------------------------------------------------- #
def count_actions_today(
    conn: sqlite3.Connection, action_type: str, *, ok_only: bool = False
) -> int:
    """今天（UTC 日期）某类动作的条数。页面额度进度从这里来，不许写死。

    `ok_only` 的取舍（2026-11-08）：
    - `greet` 的**日目标**必须只数成功的 —— 否则 2 条 `AUTH_EXPIRED`（没发出去）
      会吃掉 2 格额度，当天最多只能发 13 条真实的，验收线"每天 ≥15 条"永远达不到。
      所以 pacer / 手动入口 / 看板都用 `ok_only=True`。
    - `detail` 的**抓取预算**要数"占位次数"（包括还没回填的 pending），
      所以那边保持默认 `False`。
    """
    sql = (
        "SELECT COUNT(*) AS n FROM actions"
        " WHERE action_type = ? AND date(created_at) = date('now')"
    )
    if ok_only:
        sql += " AND status = 'ok'"
    row = conn.execute(sql, (action_type,)).fetchone()
    return int(row["n"]) if row else 0


# --------------------------------------------------------------------------- #
# runs
# --------------------------------------------------------------------------- #
def start_run(conn: sqlite3.Connection, kind: str) -> int:
    cursor = conn.execute(
        "INSERT INTO runs (kind, started_at) VALUES (?, ?)", (kind, utcnow())
    )
    conn.commit()
    return int(cursor.lastrowid or 0)


def finish_run(
    conn: sqlite3.Connection,
    run_id: int,
    stats: dict[str, Any] | None = None,
    error: str | None = None,
) -> None:
    import json

    conn.execute(
        "UPDATE runs SET ended_at = ?, stats_json = ?, error = ? WHERE run_id = ?",
        (utcnow(), json.dumps(stats or {}, ensure_ascii=False), error, run_id),
    )
    conn.commit()


def list_runs(conn: sqlite3.Connection, limit: int = 10) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT run_id, kind, started_at, ended_at, stats_json, error FROM runs"
        " ORDER BY run_id DESC LIMIT ?",
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]

"""`sweep` 接入 enrich 的集成测试 —— 3 条（任务书 §11 第 15–17 条）。

三条守的是三件不同的事，缺一条就等于本批没做：

15. **顺序**：`enrich` 在 `score` 之前。真机 `search` 不返回 JD（`jd_text=""`），
    分数只能靠 `detail` 回填后才有意义。顺序反了 = 分母还是 0 = `greeted=0` 重演。
16. **限额**：`detail_batch` 真的把单轮抓取截断，且**截掉的条数有账可查**
    （`skipped_by_detail_quota`，不是静默少抓）。
17. **不误伤**：fake 路径（列表项自带 JD）**一次 detail 都不发**，`greeted==4` 不变。

`_BlankJdAdapter` 模拟真机的形状：`search` 返回的每一项 `jd_text` 都是空串，
正文只有 `detail()` 拿得到——这正是 P2-A `greeted=0` 的现场。
"""

from __future__ import annotations

from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app import models
from app.adapters.fake import JOBS as FAKE_JOBS
from app.adapters.fake import FakeAdapter
from app.core import matcher
from app.main import create_app


class _BlankJdAdapter(FakeAdapter):
    """列表项没有 JD、只有 `detail()` 有正文的适配器（真机 search 的真实形状）。"""

    def __init__(self) -> None:
        super().__init__()
        self._jd = {str(job["job_id"]): str(job.get("jd_text") or "") for job in FAKE_JOBS}
        self.detail_calls: list[str] = []

    def probe(self) -> set[str]:
        self.calls.append("probe")
        return set(self.capabilities)

    def search(self, query: str = "", **filters) -> list[dict]:
        self.calls.append(f"search:{query}")
        return [dict(job, jd_text="") for job in self._jobs]

    def detail(self, security_id: str, job_id: str | None = None) -> dict:
        self.detail_calls.append(str(job_id))
        return {
            "ok": True,
            "job_id": str(job_id or ""),
            "jd_text": self._jd.get(str(job_id), ""),
            "address": "",
            "hr_active": "3d",
            "greeted": False,
        }


@pytest.fixture()
def client(tmp_path: Path):
    app = create_app(db_path=tmp_path / "sweep.db", env_path=tmp_path / "no.env")
    with TestClient(app) as test_client:
        yield test_client


def _swap_adapter(client: TestClient, adapter: FakeAdapter) -> FakeAdapter:
    """把 `app.state.adapter` 换成桩（`get_adapter()` 读的就是这个位置）。"""
    client.app.state.adapter = adapter
    client.app.state.adapter_name = "enrich_stub"
    return adapter


def _db(client: TestClient):
    return models.connect(client.app.state.db_path)


def test_sweep_enriches_before_score(client: TestClient):
    """顺序：无 JD 的岗经 `detail` 回填后分数过线 → `enriched>0`、`passed>0`、`greeted>0`。

    先证明"无 JD 时过不了线"（这正是 P2-A `greeted=0` 的原因），再证明同一岗位
    在回填后**过线并被真的打了招呼**——两句话一起说，才排除了"分数本来够高"的可能。
    """
    adapter = _swap_adapter(client, _BlankJdAdapter())
    cfg = client.app.state.config

    # ① 无 JD 时：分数低于阈值（否则这条测试证明不了顺序的重要性）
    blank_job = dict(next(job for job in FAKE_JOBS if job["job_id"] == "job_002"), jd_text="")
    base_score, _reasons = matcher.score(blank_job, cfg)
    assert base_score < cfg["min_match_score"]
    assert matcher.should_greet(base_score, cfg) is False

    # ② 跑一轮
    sweep = client.post("/api/run/sweep").json()
    assert sweep["ok"] is True
    stats = sweep["stats"]

    assert stats["enriched"] > 0, "detail 必须真的抓到正文"
    assert stats["passed"] > 0, "回填后必须有过线岗位（P2-A 缺的就是这个数）"
    assert stats["greeted"] > 0, "过线就要真的发出去"
    assert stats["skipped_by_detail_quota"] == 0  # 8 个候选没超 detail_batch=10
    assert stats["enriched_failed"] == 0

    # ③ 库里那条岗位：正文回填了、分数涨过线了（= enrich 在 score 之前）
    conn = _db(client)
    try:
        row = conn.execute(
            "SELECT jd_text, match_score, gap_flags FROM jobs WHERE job_id = 'job_002'"
        ).fetchone()
    finally:
        conn.close()
    assert row["jd_text"], "jd_text 必须已回填"
    assert row["match_score"] > base_score, "回填后分数必须变高"
    assert row["match_score"] >= cfg["min_match_score"]
    # gap_flags 是**重算**的（P2-A 根本没写这一列，且它是基于无 JD 的列表项算的）
    conn = _db(client)
    try:
        flags_rows = conn.execute("SELECT job_id, gap_flags FROM jobs").fetchall()
    finally:
        conn.close()
    computed = {
        flags_row["job_id"]: models.parse_gap_flags(flags_row["gap_flags"])
        for flags_row in flags_rows
        if models.parse_gap_flags(flags_row["gap_flags"])
    }
    assert computed, "重算后至少有一条岗位产出 gap_flags（如「要求2年经验」）"
    # 具体到 job_001：正文里有"两年以上企业法务经验"→ 必须产出"要求2年经验"
    assert "要求2年经验" in computed.get("job_001", [])


def test_sweep_enrich_respects_batch_and_reports_skipped(client: TestClient):
    """`detail_batch=2` → 只抓 2 条，剩下的条数进 `skipped_by_detail_quota`（不许静默）。"""
    client.app.state.config["quota"]["detail_batch"] = 2
    adapter = _swap_adapter(client, _BlankJdAdapter())

    sweep = client.post("/api/run/sweep").json()
    assert sweep["ok"] is True
    stats = sweep["stats"]

    assert stats["enriched"] == 2, "单轮上限必须真的截断"
    assert stats["skipped_by_detail_quota"] > 0, "被额度截掉的条数必须有账"
    # 串行 + 不超抓：适配器正好被调 2 次
    assert len(adapter.detail_calls) == 2
    # 抓了 2 条之后还有候选没抓 → 这三个数必须能解释 enriched 为什么这么小
    assert (
        stats["enriched"]
        + stats["skipped_by_detail_quota"]
        + stats["enrich_prefiltered"]
        + stats["enriched_failed"]
        >= stats["found"]
    )


def test_sweep_fake_path_makes_no_detail_calls(client: TestClient):
    """fake 路径：列表项自带 JD → **0 次 detail**，`greeted==4` 与 P2-A 完全一致。"""
    adapter = client.app.state.adapter
    assert isinstance(adapter, FakeAdapter)

    sweep = client.post("/api/run/sweep").json()
    assert sweep["ok"] is True
    stats = sweep["stats"]

    # 一次 detail 都不许发（`FakeAdapter` 根本没有 detail 方法，L0-1 双守卫兜住）
    assert not any(call.startswith("detail") for call in adapter.calls)
    assert stats["enriched"] == 0
    assert stats["enriched_failed"] == 0
    # 8 条全被"已有正文 / 无 detail 能力"剪掉，enriched=0 有解释（§6.6）
    assert stats["enrich_prefiltered"] == len(FAKE_JOBS)
    assert stats["skipped_by_detail_quota"] == 0

    # P2-A 的断言值一个都没变
    assert stats["found"] == 8
    assert stats["greeted"] == 4
    assert stats["skipped_by_quota"] == 0
    assert stats["passed"] == 4

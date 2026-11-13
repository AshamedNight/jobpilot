"""P2-B 风控期两个缺陷的回归 —— 3 条（F7 1 条 + F6 2 条）。

现场（B-4a）：`found=6` 真岗位 → `enriched=2` 真 JD → 第 3 次 `detail` 命中
`ENVIRONMENT_RISK`。风控处理是对的，但**结果被弄丢了 / 看不见**：

- **F7**：抓到的 2 条 JD 只合并进内存，落库在 `if not risk_hit:` 里面 → 一条没写；
  而 `detail:{job_id}:{今天}` 的 claim 已经是 `ok` → 当天永久不再抓。
  `enriched=2` 看着成功、库里 `jd_len=0`、预算白花，是"最糟失败形态"的变种。
- **F6**：`risk_locked` 只活在 `app.state`（进程内存），重启即清零；上游那把锁在
  `<data_dir>/cdp_risk_lock.json`（磁盘，要 `boss clean --risk-lock` 才解）。
  于是重启后页面显示"运行中"，而每一次请求都被拒——**重启后看起来正常、实际全被拒**。
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app import models
from app.adapters.boss import BossAdapter
from app.adapters.fake import JOBS as FAKE_JOBS
from app.adapters.fake import FakeAdapter
from app.main import create_app

REPO_ROOT = Path(__file__).resolve().parent.parent


class _StubCli:
    """顶掉 `boss._cli_entry`：只回答 `schema`，一个业务请求都不发。"""

    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[list[str]] = []

    def __call__(self, *args, **kwargs):
        self.calls.append(list(args))
        print(json.dumps(self.payload, ensure_ascii=False))


def _schema_ok() -> dict:
    return {
        "ok": True,
        "schema_version": "1.0",
        "command": "schema",
        "data": {"name": "boss-agent-cli"},
        "pagination": None,
        "error": None,
        "hints": None,
    }


class _RiskMidwayAdapter(FakeAdapter):
    """第 1 条 `detail` 成功、第 2 条返回 `ACCOUNT_RISK`（B-4a 的形状）。"""

    def __init__(self) -> None:
        super().__init__()
        first, second = FAKE_JOBS[0], FAKE_JOBS[1]
        self._job_list = [dict(first, jd_text=""), dict(second, jd_text="")]
        self._ok_jd = {str(first["job_id"]): str(first["jd_text"])}
        self.detail_calls: list[str] = []

    def probe(self) -> set[str]:
        self.calls.append("probe")
        return set(self.capabilities)

    def search(self, query: str = "", **filters) -> list[dict]:
        self.calls.append(f"search:{query}")
        return [dict(job) for job in self._job_list]

    def detail(self, security_id: str, job_id: str | None = None) -> dict:
        key = str(job_id or "")
        self.detail_calls.append(key)
        jd_text = self._ok_jd.get(key)
        if jd_text is not None:
            return {
                "ok": True,
                "job_id": key,
                "jd_text": jd_text,
                "address": "",
                "hr_active": "3d",
                "greeted": False,
            }
        # 第 2 次：平台风控。`code` 是上游原始码，`normalized` 是我们的归一码
        return {
            "ok": False,
            "error": {
                "sent": False,
                "error_code": "RISK_LOCKED",
                "detail": "访问环境存在异常",
                "raw": None,
                "command": "detail",
                "code": "ACCOUNT_RISK",
                "message": "访问环境存在异常",
                "normalized": "RISK_LOCKED",
            },
        }


@pytest.fixture()
def client(tmp_path: Path):
    app = create_app(db_path=tmp_path / "risk.db", env_path=tmp_path / "no.env")
    with TestClient(app) as test_client:
        yield test_client


# --------------------------------------------------------------------------- #
# F7：风控终止时**抓到的 JD 必须落库**
# --------------------------------------------------------------------------- #
def test_enriched_jd_survives_risk_lock_termination(client: TestClient):
    """`detail` 抓到 1 条后命中 `ACCOUNT_RISK` → 仍然落库，只是不算分、不打招呼。

    改之前这条必红：落库被 `if not risk_hit:` 包住，而 claim 已是 `ok`，
    于是那 1 条 JD 只在内存里、当天再也不会抓（`claimed_today`）——永久丢失 + 预算白花。
    """
    adapter = _RiskMidwayAdapter()
    client.app.state.adapter = adapter
    client.app.state.adapter_name = "risk_stub"

    sweep = client.post("/api/run/sweep").json()
    assert sweep["ok"] is True
    stats = sweep["stats"]

    # 风控确实命中了：锁上、整轮终止（不算分、不过滤、不打招呼）
    assert stats["risk_locked"] is True
    assert stats["enriched"] == 1
    assert stats["scored"] == 0
    assert stats["passed"] == 0
    assert stats["greeted"] == 0
    assert len(adapter.detail_calls) == 2

    # 命门：**抓到的那条 JD 必须在库里**（不是只在内存）
    first_job_id = str(FAKE_JOBS[0]["job_id"])
    second_job_id = str(FAKE_JOBS[1]["job_id"])
    conn = models.connect(client.app.state.db_path)
    try:
        first = models.get_job(conn, first_job_id)
        second = models.get_job(conn, second_job_id)
        claims = {
            row["idempotency_key"]: row["status"]
            for row in conn.execute("SELECT idempotency_key, status FROM actions")
        }
        detail_today = models.count_actions_today(conn, "detail")
    finally:
        conn.close()

    assert first["jd_text"], "抓到 JD 的岗位必须已落库（F7 的核心）"
    assert first["jd_text"] == str(FAKE_JOBS[0]["jd_text"])
    # gap_flags 也要跟着重算（否则页面提示项同样丢）
    assert models.parse_gap_flags(first["gap_flags"]) != []
    # 分数不写：风控后算分没有意义，`scored=0` 与库里一致
    assert first["match_score"] is None

    # 没抓到的那条仍是空（不许凭空写）
    assert second["jd_text"] == ""

    # claim 与落库必须一致：claim 是 ok 的，正文就必须在库里
    today = datetime.now(timezone.utc).strftime("%Y%m%d")
    assert claims[f"detail:{first_job_id}:{today}"] == "ok"
    assert claims[f"detail:{second_job_id}:{today}"] == "failed"
    assert detail_today == 2


# --------------------------------------------------------------------------- #
# F6：上游磁盘锁必须在启动时被看见
# --------------------------------------------------------------------------- #
def _boss_config_with_data_dir(tmp_path: Path, data_dir: Path) -> Path:
    """基于仓库 config.yaml 造一份 boss 配置，`data_dir` 指向临时目录。

    不把测试专用配置文件放进仓库（多一份就会漂移），只在 tmp 里生成一份。

    **必须走 YAML 序列化，不许手拼字符串**（2026-10-27 第二次事故）：
    原来写的是 `text.replace('data_dir: "…"', f'data_dir: "{data_dir}"')`——
    Linux 上 `tmp_path` 没有反斜杠，所以一直是绿的；Windows 上
    `C:\\Users\\19506\\AppData\\Local\\Temp\\…` 塞进**双引号标量**里，`\\U` 是非法转义，
    PyYAML 直接 `ScannerError`，两条 F6 测试变成
    `StartupError: 配置文件不是合法 YAML`。手拼路径 = 把平台差异藏进字符串。
    """
    cfg = yaml.safe_load((REPO_ROOT / "config.yaml").read_text(encoding="utf-8"))
    cfg["adapter"] = "boss"
    cfg.setdefault("adapter_options", {})["data_dir"] = str(data_dir)

    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump(cfg, allow_unicode=True, sort_keys=False), encoding="utf-8"
    )

    # 回读断言：含反斜杠的路径必须**原样往返**（这条直接钉死上面那个 Windows 坑）
    reloaded = yaml.safe_load(cfg_path.read_text(encoding="utf-8"))
    assert reloaded["adapter_options"]["data_dir"] == str(data_dir)
    assert reloaded["adapter"] == "boss"
    return cfg_path


def _make_boss_app(tmp_path: Path, monkeypatch, with_lock: bool):
    import app.adapters.boss as boss_module

    data_dir = tmp_path / "boss-data"
    data_dir.mkdir(parents=True, exist_ok=True)
    if with_lock:
        (data_dir / "cdp_risk_lock.json").write_text(
            '{"locked": true, "reason": "ENVIRONMENT_RISK"}', encoding="utf-8"
        )

    monkeypatch.setenv("ADAPTER", "boss")
    # 零网络：只顶掉 CLI 入口（`probe()` 只调 `schema`）与版本查询
    monkeypatch.setattr(boss_module, "_cli_entry", lambda: _StubCli(_schema_ok()))
    monkeypatch.setattr(BossAdapter, "upstream_version", staticmethod(lambda: "3.0.1"))

    cfg_path = _boss_config_with_data_dir(tmp_path, data_dir)
    return create_app(
        config_path=cfg_path,
        db_path=tmp_path / "boss.db",
        env_path=tmp_path / "no.env",
    )


def test_upstream_risk_lock_file_locks_on_startup(tmp_path: Path, monkeypatch):
    """`<data_dir>/cdp_risk_lock.json` 存在 → 启动即 `risk_locked=True` + 中文告警。"""
    app = _make_boss_app(tmp_path, monkeypatch, with_lock=True)

    assert app.state.risk_locked is True, "上游磁盘锁存在时必须一启动就锁上"
    assert getattr(app.state, "upstream_risk_lock", None) is True

    warnings = [str(w) for w in (app.state.config.get("warnings") or [])]
    assert any("cdp_risk_lock.json" in w for w in warnings), "必须告警说清是哪把锁"
    assert any("clean --risk-lock" in w for w in warnings), "必须给出解法"
    assert any("\u4e00" <= ch <= "\u9fff" for w in warnings for ch in w), "告警必须是中文"

    # 页面可见（`/api/status` 纯新增键），且 `llm` 仍是那 7 个键（冻结口径）
    with TestClient(app) as client:
        status = client.get("/api/status").json()
        assert status["risk_locked"] is True
        assert status["upstream_risk_lock"] is True
        assert set(status["llm"]) == {
            "mode",
            "calls_today",
            "daily_cap",
            "remaining",
            "cache_hit",
            "billable_calls_today",
            "last_fallback_reason",
        }


def test_no_risk_lock_file_keeps_app_running(tmp_path: Path, monkeypatch):
    """没有锁文件 → 不锁、不告警（不许把"没锁"也报成锁）。"""
    app = _make_boss_app(tmp_path, monkeypatch, with_lock=False)

    assert app.state.risk_locked is False
    assert getattr(app.state, "upstream_risk_lock", None) is False
    warnings = [str(w) for w in (app.state.config.get("warnings") or [])]
    assert not any("cdp_risk_lock.json" in w for w in warnings)

    with TestClient(app) as client:
        status = client.get("/api/status").json()
        assert status["risk_locked"] is False
        assert status["upstream_risk_lock"] is False

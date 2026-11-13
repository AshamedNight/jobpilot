"""LLM 生成策略网关（P2-C §4.7）—— 8 条，全部离线。

铁律：本批 **`core/` 一行未改**。所有测试都是"换注入对象"：`composer` / `matcher` /
`state_machine` / `extractor` / `FakeAdapter` 一个都没动。

8 条覆盖（§4.7 的 1~8，其中 6 与 8 各自合并了本批追加的口径断言）：

  1. `off`：不联网、≤60 字、文案含岗位字段
  2. `off` 下能走通 `compose_opener`（core 未改也接得上）
  3. `cache` 命中：同文本 + `tokens_in=0`
  4. `cache` 未命中：回落模板、不抛异常；**模板行不许被命中**
  5. `live` 预期内异常 → 降级模板；**未知异常必须上抛**
  6. `calls_today` / `billable_calls_today` 同值同源 + 超预算降级
  7. `llm.mode` 非法 → `StartupError`（中文）
  8. 模板选择确定性 + 平手按**声明顺序** tie-break
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest
import yaml
from fastapi.testclient import TestClient

from app import models
from app.adapters.llm_gateway import (
    MODEL_CACHE,
    MODEL_FALLBACK,
    MODEL_TEMPLATE,
    LlmGateway,
    LlmHttpClient,
    LlmNotConfigured,
    LlmTimeout,
    TemplateStore,
)
from app.config import StartupError, load_config
from app.core.composer import OPENER_MAX_CHARS, compose_opener, prompt_hash
from app.main import create_app

REPO_CONFIG = Path(__file__).resolve().parents[1] / "config.yaml"

JOB = {
    "job_id": "job_001",
    "title": "法务专员",
    "company": "某制造企业",
    "salary": "5-8K",
    "city": "淄博",
}


# --------------------------------------------------------------------------- #
# 辅助
# --------------------------------------------------------------------------- #
def _cfg(mode: str = "off", cap: int = 100, llm_configured: bool = False) -> dict:
    return {
        "llm": {
            "mode": mode,
            "daily_call_cap": cap,
            "model": "deepseek-chat",
            "temperature": 0.7,
            "max_tokens": 300,
            "timeout_s": 30,
        },
        "llm_configured": llm_configured,
    }


class _ExplodingClient:
    """任何调用都算"联网了"——用它证明 off / cache 根本没碰模型。"""

    model = "deepseek-chat"

    def complete(self, prompt: str) -> tuple[str, int, int]:
        raise AssertionError("这个模式下不许调用模型客户端")


class _FakeClient:
    """可控的假客户端：可返回正常文本，也可按需抛指定异常。"""

    model = "deepseek-chat"

    def __init__(self, text: str = "模型产出的开场白", error: BaseException | None = None) -> None:
        self.text = text
        self.error = error
        self.calls = 0

    def complete(self, prompt: str) -> tuple[str, int, int]:
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.text, 12, 8


def _rows(db_path, sql: str, params: tuple = ()):
    conn = models.connect(db_path)
    try:
        return conn.execute(sql, params).fetchall()
    finally:
        conn.close()


def _write_templates(tmp_path: Path, templates: list[dict]) -> Path:
    path = tmp_path / "opener_templates.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        yaml.safe_dump(
            {"version": 1, "max_chars": 60, "templates": templates}, allow_unicode=True
        ),
        encoding="utf-8",
    )
    return path


# --------------------------------------------------------------------------- #
# ① off：纯模板，不联网
# --------------------------------------------------------------------------- #
def test_off_mode_is_template_only_and_never_touches_the_model(conn, db_path):
    gateway = LlmGateway(_cfg("off"), db_path, client=_ExplodingClient())

    text = gateway.writer_for(JOB).complete("任意 prompt")

    assert text.strip()
    assert len(text) <= OPENER_MAX_CHARS
    assert JOB["title"] in text  # 文案里出现岗位字段

    rows = _rows(db_path, "SELECT model, tokens_in, tokens_out FROM llm_calls")
    assert len(rows) == 1  # 一次生成只记一行
    assert rows[0]["model"] == MODEL_TEMPLATE
    assert rows[0]["tokens_in"] == 0
    assert rows[0]["tokens_out"] == 0


# --------------------------------------------------------------------------- #
# ② off 下走通 compose_opener（core 未改也接得上）
# --------------------------------------------------------------------------- #
def test_off_mode_plugs_into_compose_opener_without_touching_core(conn, db_path):
    gateway = LlmGateway(_cfg("off"), db_path, client=_ExplodingClient())

    opener = compose_opener(JOB, "", gateway.writer_for(JOB))

    assert opener.strip()
    assert len(opener) <= OPENER_MAX_CHARS
    assert JOB["title"] in opener
    # 同一个 job 两次走完整 composer 链路，结果恒定
    assert compose_opener(JOB, "", gateway.writer_for(JOB)) == opener


# --------------------------------------------------------------------------- #
# ③ cache 命中
# --------------------------------------------------------------------------- #
def test_cache_hit_returns_stored_response_with_zero_tokens(conn, db_path):
    prompt = "缓存命中的 prompt"
    models.record_llm_call(
        conn,
        purpose="opener",
        uid=None,
        model="deepseek-chat",
        prompt_hash=prompt_hash(prompt),
        response="模型曾说过的开场白",
        tokens_in=11,
        tokens_out=7,
    )

    gateway = LlmGateway(_cfg("cache"), db_path, client=_ExplodingClient())
    text = gateway.writer_for(JOB).complete(prompt)

    assert text == "模型曾说过的开场白"
    latest = _rows(
        db_path, "SELECT model, tokens_in, tokens_out FROM llm_calls ORDER BY id DESC LIMIT 1"
    )[0]
    assert latest["model"] == MODEL_CACHE  # 命中仍记一行，保证可审计
    assert latest["tokens_in"] == 0
    assert latest["tokens_out"] == 0


# --------------------------------------------------------------------------- #
# ④ cache 未命中 + 模板行不可被命中
# --------------------------------------------------------------------------- #
def test_cache_miss_falls_back_to_template_and_ignores_template_rows(conn, db_path):
    prompt = "未命中的 prompt"
    # 模板行**不许**被当成缓存命中：否则模板文案会伪装成模型结果，审计口径作废
    models.record_llm_call(
        conn,
        purpose="opener",
        uid=None,
        model=MODEL_TEMPLATE,
        prompt_hash=prompt_hash(prompt),
        response="模板不该被命中",
        tokens_in=0,
        tokens_out=0,
    )

    gateway = LlmGateway(_cfg("cache"), db_path, client=_ExplodingClient())
    text = gateway.writer_for(JOB).complete(prompt)

    assert text != "模板不该被命中"
    assert JOB["title"] in text
    assert len(text) <= OPENER_MAX_CHARS

    latest = _rows(db_path, "SELECT model FROM llm_calls ORDER BY id DESC LIMIT 1")[0]
    assert latest["model"] == MODEL_TEMPLATE  # 未命中 → 走模板，不抛异常


# --------------------------------------------------------------------------- #
# ⑤ live：只吞预期内异常，未知异常必须上抛
# --------------------------------------------------------------------------- #
def test_live_degrades_only_on_expected_errors_and_leaks_unknown_ones(conn, db_path):
    gateway = LlmGateway(_cfg("live", llm_configured=True), db_path, client=_FakeClient())

    # (a) 预期内：超时 → 降级模板 + 留痕
    gateway.client = _FakeClient(error=LlmTimeout("上游超时"))
    text = gateway.writer_for(JOB).complete("p")
    assert JOB["title"] in text
    assert len(text) <= OPENER_MAX_CHARS
    assert gateway.last_fallback_reason == "timeout"
    assert gateway.stats_today()["last_fallback_reason"] == "timeout"
    latest = _rows(db_path, "SELECT model FROM llm_calls ORDER BY id DESC LIMIT 1")[0]
    assert latest["model"] == MODEL_FALLBACK

    # (b) 预期内：没有 key → 降级模板 + 留痕
    gateway.client = _FakeClient(error=LlmNotConfigured("没有 key"))
    gateway.writer_for(JOB).complete("p")
    assert gateway.last_fallback_reason == "no_api_key"

    # (c) 未知异常**必须上抛**（交给 dashboard 的 try/except 写 runs.error），不许吞
    gateway.client = _FakeClient(error=RuntimeError("意料之外"))
    with pytest.raises(RuntimeError, match="意料之外"):
        gateway.writer_for(JOB).complete("p")


# --------------------------------------------------------------------------- #
# ⑥ 计费口径：calls_today / billable_calls_today 同值同源 + 超预算降级
# --------------------------------------------------------------------------- #
def test_calls_today_counts_only_billable_and_shares_one_source_with_alias(conn, db_path):
    # 3 行模板 + 2 行 model='live' → 只有 2 行是花钱的
    for _ in range(3):
        models.record_llm_call(
            conn, purpose="opener", uid=None, model=MODEL_TEMPLATE,
            prompt_hash="h", response="模板", tokens_in=0, tokens_out=0,
        )
    for _ in range(2):
        models.record_llm_call(
            conn, purpose="opener", uid=None, model="live",
            prompt_hash="h", response="模型", tokens_in=9, tokens_out=5,
        )

    gateway = LlmGateway(_cfg("live", cap=2, llm_configured=True), db_path, client=_FakeClient())
    stats = gateway.stats_today()

    assert stats["calls_today"] == 2
    assert stats["billable_calls_today"] == stats["calls_today"]  # 同值同源
    assert stats["daily_cap"] == 2
    assert stats["remaining"] == 0
    assert stats["mode"] == "live"
    # §4.8 的五个键一个不少，且只多两个本批新增键（不加第三个同义键）
    assert set(stats) == {
        "mode", "calls_today", "billable_calls_today",
        "daily_cap", "remaining", "cache_hit", "last_fallback_reason",
    }

    # 预算到顶 → 降级模板，且可观测（一次都没真调）
    text = gateway.writer_for(JOB).complete("p")
    assert JOB["title"] in text
    assert gateway.client.calls == 0
    assert gateway.last_fallback_reason == "budget_exhausted"
    latest = _rows(db_path, "SELECT model FROM llm_calls ORDER BY id DESC LIMIT 1")[0]
    assert latest["model"] == MODEL_FALLBACK
    assert gateway.stats_today()["remaining"] == 0


# --------------------------------------------------------------------------- #
# ⑦ llm.mode 非法 → StartupError（中文）
# --------------------------------------------------------------------------- #
def test_illegal_llm_mode_raises_startup_error_in_chinese(tmp_path):
    real = Path(__file__).resolve().parents[1] / "config.yaml"
    broken = real.read_text(encoding="utf-8").replace('mode: "off"', 'mode: "turbo"')
    assert 'mode: "turbo"' in broken  # 确认真的改了，不是空操作

    bad_config = tmp_path / "config.yaml"
    bad_config.write_text(broken, encoding="utf-8")

    with pytest.raises(StartupError) as excinfo:
        load_config(bad_config)

    message = str(excinfo.value)
    assert "turbo" in message
    assert any("\u4e00" <= ch <= "\u9fff" for ch in message)  # 中文
    assert "off" in message  # 提示里给出合法枚举


# --------------------------------------------------------------------------- #
# ⑧ 模板选择确定性 + 平手按声明顺序
# --------------------------------------------------------------------------- #
def test_template_selection_is_deterministic_and_tie_break_uses_declaration_order(
    conn, db_path, tmp_path
):
    # (a) 无命中 → 只从"兜底组"（match_words 为空）里选，与 sha256 无关
    store = TemplateStore()
    for index in range(1, 40):
        assert store.pick({"job_id": f"job_{index:03d}", "title": "法务专员"})["id"] == "t_general"

    # (b) 同一个 job_id 两次生成结果相同（跨 gateway 实例也相同）
    first = LlmGateway(_cfg("off"), db_path, client=_ExplodingClient()).writer_for(JOB).complete("p")
    second = LlmGateway(_cfg("off"), db_path, client=_ExplodingClient()).writer_for(JOB).complete("p")
    assert first == second

    # (c) 平手：两条模板命中数相同 → 组内按**声明顺序**排列，索引 = sha256(job_id) % len(group)
    tie_a = _write_templates(
        tmp_path / "a",
        [
            {"id": "t_first", "match_words": ["合同"], "text": "第一条{title}"},
            {"id": "t_second", "match_words": ["合同"], "text": "第二条{title}"},
        ],
    )
    job = {"job_id": "job_001", "title": "合同岗"}
    spin = int(hashlib.sha256(b"job_001").hexdigest(), 16) % 2
    store_a = TemplateStore(tie_a)
    assert store_a.pick(job)["id"] == ("t_first" if spin == 0 else "t_second")
    assert store_a.pick(job)["id"] == store_a.pick(job)["id"]  # 同一 job 恒定

    # 把声明顺序倒过来：同一 job_id 若仍取 index 0，得到的必须是**现在声明在前**的那条，
    # 这证明 tie-break 用的是声明顺序，而不是字典序（字典序永远会先给 t_first）。
    tie_b = _write_templates(
        tmp_path / "b",
        [
            {"id": "t_second", "match_words": ["合同"], "text": "第二条{title}"},
            {"id": "t_first", "match_words": ["合同"], "text": "第一条{title}"},
        ],
    )
    store_b = TemplateStore(tie_b)
    assert store_b.pick(job)["id"] == ("t_second" if spin == 0 else "t_first")
    # 组内顺序 = 声明顺序：index 0 就是文件里第一条
    assert store_b.templates[0]["id"] == "t_second"
    assert store_b.render_text(store_b.templates[0]["text"], job) == "第二条合同岗"


# --------------------------------------------------------------------------- #
# ⑨【F1 修复回归】只写 .env 的 key 必须能到模型客户端，且不许泄露到 /api/status
# --------------------------------------------------------------------------- #
def test_live_reads_api_key_from_dotenv_and_never_leaks_it(tmp_path, monkeypatch):
    # 先清空系统环境变量，确保测的就是「只有 .env 这一条路」
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    env = tmp_path / ".env"
    env.write_text("LLM_API_KEY=sk-test-0123456789\n", encoding="utf-8")

    cfg = load_config(env_path=env)

    assert cfg["llm_configured"] is True
    assert cfg["llm"]["api_key"] == "sk-test-0123456789"  # key 值本身进了 cfg["llm"]
    assert LlmHttpClient(cfg).available() is True          # ← F1 的核心断言

    # 系统环境变量那条路不许被改坏（.env 优先，环境变量兜底）
    empty_env = tmp_path / "empty.env"
    empty_env.write_text("", encoding="utf-8")
    monkeypatch.setenv("LLM_API_KEY", "sk-sysenv")
    assert LlmHttpClient(load_config(env_path=empty_env)).available() is True

    # 占位符不许被当成可用 key 流转
    placeholder_env = tmp_path / "placeholder.env"
    placeholder_env.write_text("LLM_API_KEY=your-key-here\n", encoding="utf-8")
    placeholder_cfg = load_config(env_path=placeholder_env)
    assert placeholder_cfg["llm_configured"] is False
    assert placeholder_cfg["llm"]["api_key"] == ""

    # key 明文不许出现在 /api/status 的任何一处
    app = create_app(
        config_path=REPO_CONFIG, env_path=env, db_path=tmp_path / "leak.db"
    )
    with TestClient(app) as client:
        response = client.get("/api/status")
        assert response.status_code == 200
        body = response.text
        payload = response.json()

    assert "sk-test-0123456789" not in body
    assert "api_key" not in json.dumps(payload)
    assert "api_key" not in payload["llm"]


# --------------------------------------------------------------------------- #
# ⑩【F1 守卫】API Key 写进 config.yaml → StartupError（中文），不许随包泄露
# --------------------------------------------------------------------------- #
def test_api_key_in_config_yaml_is_rejected(tmp_path, monkeypatch):
    monkeypatch.delenv("LLM_API_KEY", raising=False)
    text = REPO_CONFIG.read_text(encoding="utf-8").replace(
        '  mode: "off"', '  mode: "off"\n  api_key: "sk-should-not-be-here"'
    )
    assert "sk-should-not-be-here" in text  # 确认真的改了，不是空操作

    bad_config = tmp_path / "config.yaml"
    bad_config.write_text(text, encoding="utf-8")

    with pytest.raises(StartupError) as excinfo:
        load_config(bad_config, env_path=tmp_path / "no.env")

    message = str(excinfo.value)
    assert "config.yaml" in message
    assert ".env" in message  # 指明正确位置
    assert any("\u4e00" <= ch <= "\u9fff" for ch in message)  # 中文

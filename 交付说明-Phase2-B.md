# 交付说明 · Phase 2-B 真打招呼

- 日期：2026-10-27
- 基线：`jobpilot-Phase2_A_fix.zip`（48 文件，74 passed，tag `p2a-done`）
- 本包：51 文件（基线 48 + 新增 3：`app/core/enrich.py` / `tests/test_enrich.py` / `tests/test_sweep_enrich.py`）
- 依据：`任务书-Phase2-B-真打招呼.md` v0.5（含 §11.1 两条授权例外、§8 已确认撤回）、`补页-P2B-上游事实实测.md`、`复审结论-Phase2-A.md` §6

---

## 0. 前置自检（§3）

沙盒**没有 `git`**（`git: command not found`），`git log --oneline -3` / `git status --short` 两项无法在本环境复现；tag 状态由用户侧截图确认（`a25f960 (HEAD -> master, tag: p2a-done)`，见补页 §F）。其余两项实测：

```text
$ python3 -m pytest -q          # 基线开工前
74 passed, 1 warning in 9.01s
```

那 1 条 warning 与 P2-A 定稿包同一个第三方告警（`starlette/testclient.py:40` 对 `anyio.abc.BlockingPortal` 的 `DeprecationWarning`，`fastapi 0.115.6` + `httpx 0.28.1` 引起），非失败、与本批代码无关。

---

## 1. `pytest -q` 与 `--collect-only` 原始输出

```text
$ python3 -m pytest -q
........................................................................ [ 97%]
..                                                                       [100%]
=============================== warnings summary ================================
../../usr/local/lib/python3.12/dist-packages/starlette/testclient.py:40
  .../testclient.py:40: DeprecationWarning: The anyio.abc.BlockingPortal alias is deprecated, use anyio.from_thread.BlockingPortal instead.
    _PortalFactoryType = typing.Callable[[], typing.ContextManager[anyio.abc.BlockingPortal]]

-- Docs: https://docs.pytest.org/en/stable/how-to/capture-warnings.html
91 passed, 1 warning in 10.78s
```

```text
$ python3 -m pytest --collect-only -q
91 tests collected in 1.74s
```

**91 passed / 91 collected**，与 §11 的目标合计一致（基线 74 + 新增 17，E1 是改名不增减）。

各文件条数（实测 `--collect-only`）：

| 文件 | 条数 | 本批变化 |
|---|---|---|
| `test_boss_adapter.py` | 22 | 18 → 22（+4，且 E1 1 条改名、E2 换 2 行断言） |
| `test_config.py` | 7 | 5 → 7（+2） |
| `test_models.py` | 3 | 1 → 3（+2） |
| `test_enrich.py` | 6 | 新增 |
| `test_sweep_enrich.py` | 3 | 新增 |
| 其余 6 个文件 | 50 | 未动 |

---

## 2. 新增 17 条逐条点名（对齐 §11 编号）

| # | 文件 | 测试名 | 锁什么 | 结果 |
|---|---|---|---|---|
| 1 | `test_enrich.py` | `test_needs_detail_only_when_jd_missing` | L0-1 双守卫 + L0-2；空白串不算正文 | ✅ |
| 2 | `test_enrich.py` | `test_pick_skips_hard_rejected` | L0-3；剪枝 ≠ 额度 | ✅ |
| 3 | `test_enrich.py` | `test_pick_skips_upper_bound_below_threshold` | L0-4；**monkeypatch `matcher.PRACTICE_AREA_CAP`** 预筛跟着变（防写死 32/60） | ✅ |
| 4 | `test_enrich.py` | `test_pick_respects_batch_limit_and_order` | L1 截断 + 同分稳定 + L2 日额度（两把刀同测） | ✅ |
| 5 | `test_enrich.py` | `test_pick_skips_claimed_today` | L0-5 / L0-6（注入 `is_claimed` / `is_greeted`） | ✅ |
| 6 | `test_enrich.py` | `test_merge_detail_does_not_overwrite_nonempty_jd` | 合并纯函数：不改入参、不覆盖、失败载荷不动 | ✅ |
| 7 | `test_models.py` | `test_detail_ledger_claim_and_count_today` | `detail:{id}:{date}` UNIQUE + `count_actions_today('detail')`，且不计入 greet | ✅ |
| 8 | `test_models.py` | `test_update_job_detail_writes_jd_and_gap_flags` | UPDATE 生效、`gap_flags` 是 JSON 文本、空正文照写、**列数仍 18** | ✅ |
| 9 | `test_boss_adapter.py` | `test_detail_failure_uses_record_upstream_failure` | P2-A 的 P3 收口：`command='detail'` 四字段有值 | ✅ |
| 10 | `test_boss_adapter.py` | `test_greet_success_maps_to_sent` | 成功信封 → `sent=True`；argv 形状；单发无 `--count` | ✅ |
| 11 | `test_boss_adapter.py` | `test_greet_already_greeted_normalized_ok` | `ALREADY_GREETED` → `OK`、`sent=True`、`already_greeted=True` | ✅ |
| 12 | `test_boss_adapter.py` | `test_greet_raw_has_no_security_id` | 冻结第 15 条：`raw == {"code": 0}`，`security_id` 零泄漏（成功/失败两路都查） | ✅ |
| 13 | `test_config.py` | `test_detail_batch_must_be_int_nonneg` | 中文报错；`2.5` 也必须拒（`int(2.5)` 会悄悄变 2） | ✅ |
| 14 | `test_config.py` | `test_detail_batch_cannot_exceed_20_or_daily` | `>20` / `>daily_detail` → `StartupError`；边界 `20/20` 合法 | ✅ |
| 15 | `test_sweep_enrich.py` | `test_sweep_enriches_before_score` | **顺序**：先证"无 JD 过不了线"，再证回填后分数涨、`passed>0`、`greeted>0` | ✅ |
| 16 | `test_sweep_enrich.py` | `test_sweep_enrich_respects_batch_and_reports_skipped` | `detail_batch=2` → `enriched==2`、`skipped_by_detail_quota>0`、适配器正好被调 2 次 | ✅ |
| 17 | `test_sweep_enrich.py` | `test_sweep_fake_path_makes_no_detail_calls` | fake 路径 0 次 detail、`greeted==4`、`passed==4` 不变 | ✅ |

---

## 3. 授权范围内的测试口径变更（2 条，§11.1 E1/E2）

**单列一节，不混在"新增测试"里。**

### E1 · `tests/test_boss_adapter.py`（改名，净条数不变）

before：

```python
def test_write_operations_are_blocked_by_not_implemented(adapter):
    """`greet` / `exchange` / `batch_greet` 一律 `NotImplementedError("P2-B 才实现")`。

    这是**防止误发真实招呼的闸门**，不是可以顺手补全的 TODO。
    """
    with pytest.raises(NotImplementedError, match="P2-B 才实现"):
        adapter.greet("sec_abc", "enc_job_1", "您好")
    with pytest.raises(NotImplementedError, match="P2-B 才实现"):
        adapter.exchange("uid_stable_1", type="wechat")
    with pytest.raises(NotImplementedError, match="P2-B 才实现"):
        adapter.batch_greet("法务", limit=1)
```

after：

```python
def test_exchange_and_batch_greet_are_blocked_by_not_implemented(adapter):
    """【§11.1 E1】闸门从"三写全关"前移到"`exchange` 关 + `batch_greet` 不做"。

    P2-B 把 `greet` 打开了（单发），再断言它抛错就是假的。留下来的两条各有性质：
    `exchange` 是**归 P2-D**（闸门继续关）；`batch_greet` 是**上游用不了**（§8）。
    """
    with pytest.raises(NotImplementedError, match="P2-B 才实现"):
        adapter.exchange("uid_stable_1", type="wechat")
    with pytest.raises(NotImplementedError, match="batch-greet"):
        adapter.batch_greet("法务", limit=1)
```

`batch_greet` 的 `match` 必须换掉：它的消息已按你的要求改成 `"上游 batch-greet 无 --url，本批不做"`，不再含 `"P2-B 才实现"`。**条数不变（1 → 1）**，无需补等价覆盖。

### E2 · `tests/test_boss_adapter.py::test_adapter_env_selects_boss`（2 行断言）

before：

```python
        # 只读能力，写能力不在（防误发的可见证据）
        assert "greet" not in status["capabilities"]
        assert status["capabilities"] == ["chat", "detail", "search"]
```

after：

```python
        # 【§11.1 E2】闸门前移到 "exchange 不许出现"：greet 本批真开了（§7），
        # exchange 归 P2-D，在 P2-D 之前它一直不该出现在 capabilities 里。
        assert "exchange" not in status["capabilities"]
        assert status["capabilities"] == ["chat", "detail", "greet", "search"]
```

**除这两处外，`tests/` 里既有 74 条零改动**（`test_e2e_fake.py` / `test_llm_gateway.py` 与基线 `SAME`）。

---

## 4. 改动文件清单（对齐 §9 授权清单）

| # | 文件 | 改了什么 | 处数 |
|---|---|---|---|
| 1 | `app/core/enrich.py` | **新增**：`needs_detail()` / `pick_for_detail()` / `merge_detail()` + 6 个 reason 常量；零 IO、零 adapter import | 新文件 |
| 2 | `app/models.py` | 新增 `update_job_detail()`；`SCHEMA` 里 `action_type` 注释枚举补 `'detail'` | 1 函数 + 1 行注释 |
| 3 | `app/adapters/boss.py` | ① `detail()` 失败改走 `_record_upstream_failure("detail", …)`；② `greet()` 真实现；③ 新增 `WRITE_CAPABILITIES` / `CAPABILITIES`，`probe()` 改赋 `CAPABILITIES`；④ `batch_greet()` 消息改为 `"上游 batch-greet 无 --url，本批不做"`；另：`from app.adapters.base import err_result, ok_result`（greet 需要 `ok_result`）、模块 docstring 改成现状 | 4 处 + 1 import + docstring |
| 4 | `app/config.py` | `validate_quota()` 内追加 `detail_batch` / `daily_detail` 校验（整数、≥0、`detail_batch ≤ 20`、`detail_batch ≤ daily_detail`）+ 私有助手 `_nonneg_int()` + 常量 `DETAIL_BATCH_HARD_LIMIT` | 1 函数内追加 + 1 助手 + 1 常量 |
| 5 | `config.yaml` | `quota` 段新增 `daily_detail: 50` + `detail_batch: 10`，各带注释 | 2 行（+4 行注释） |
| 6 | `app/api/dashboard.py` | ① `sweep` 按 §5.1 插入 enrich 五阶段（预打分 / 选候选 / 串行抓 / 合并 / 重算 `gap_flags`）；② `stats` 加 §6.7 的 5 键 + `already_greeted`；③ `/api/status` 的 `quota` 块加 3 键；另：常量 `RISK_ERROR_CODES` / `BUDGET_ERROR_CODE` / `GONE_ERROR_CODE` / `DETAIL_CIRCUIT_FAILURES` + `_detail_key()`、`runs.error` 拼装 | 3 处 + 支撑常量 |
| 7 | `tests/` | 新增 `test_enrich.py`(6) / `test_sweep_enrich.py`(3)；`test_models.py` +2、`test_config.py` +2、`test_boss_adapter.py` +4；E1/E2 两处按 §11.1 收敛 | 见 §2/§3 |
| 8 | `README.md` | 两处 `71` → `91`（`test_boss_adapter.py 15 条` → `22 条`、`共 71 条` → `共 91 条`、`累计 71 条全绿` → `累计 91 条全绿`） | 2 处 |
| 9 | `app/web/app.js` | **未改**（§9 第 9 条是"可选"；少一个 diff 点更稳，`quota` 3 键已在 `/api/status` 上可查） | 0 |

`app/adapters/base.py`、`app/main.py`、`app/api/__init__.py`、`app/core/*`（既有 5 个）、`config/matcher.yaml`、`config/prompts/*`、`requirements.txt`、`app/adapters/fake.py` **全部未动**。

---

## 5. `app/core/` 5 个既有文件的 diff 结论

```text
$ diff -rq /tmp/p2abase/jobpilot/app/core /workspace/jobpilot/app/core
Only in /workspace/jobpilot/app/core: enrich.py
```

**逐字节相同**：`matcher.py` / `state_machine.py` / `extractor.py` / `composer.py` / `idempotency.py` 无任何差异，只多一个 `enrich.py`（A4 通过）。

同批核对（`diff -q` 全部 `SAME`）：`app/adapters/fake.py`、`config/matcher.yaml`、`config/prompts/opener.md`、`config/prompts/reply.md`、`config/prompts/opener_templates.yaml`、`tests/test_llm_gateway.py`、`tests/test_e2e_fake.py`、`requirements.txt`。

`A7`：新增代码里 `publish_time` 零命中（命中全部落在 P2-A 既有代码：`boss.py:436` 填 `None`、`fake.py` 造数据、`matcher._is_fresh` 老逻辑、`models.py` 建表/列名）。

---

## 6. §8 三条证据的处理结果

**已按"不做 batch-greet"执行**（用户 2026-10-27 原话"撤回batchgreet"，任务书 §8 已写明已确认）：

1. `batch_greet()` 仍抛 `NotImplementedError`，消息改为 **`"上游 batch-greet 无 --url，本批不做"`**，并在 docstring 里写下三条证据（无 `--url` / 内部自己 search 挑人 / 两套去重账）；
2. `sweep` 走**单发 `greet` 循环**，无 10 条上限，节奏由 `daily_greet` + `schedule.sweep_at` 决定；
3. 测试侧由 E1 守着：`exchange` 与 `batch_greet` 仍抛错，`greet` 不再抛错。

---

## 7. 本批实现要点（供复审对照）

### 7.1 `detail` 三档失败必须分得开（补页 §C/§E）

`sweep` 里对每次 `detail` 失败按顺序判：

| 情形 | 处理 |
|---|---|
| `ACCOUNT_RISK`（含 `ENVIRONMENT_RISK*` / `LOGIN_RISK_CONTROL`） | `_lock_risk()` + `risk_hit=True` + **整轮终止**（后续 score/greet 一个字都不执行） |
| `REQUEST_BUDGET_EXHAUSTED`（或归一为 `LIMIT`） | 本轮**停止 enrich**，不自动重试（上游 `recovery_action` 明文禁止） |
| `JOB_NOT_FOUND`（归一 `GONE`） | **正常终态**：算 `enriched_failed`，`continue` 抓下一个。不锁风险、不当预算问题 |
| 其他 | 同一路 `_record_upstream_failure("detail", …)` 已落四字段，继续；**连续 3 次**熔断本轮 |

三种原因靠 `runs.error`（`"enrich: …"` 前缀）与 `/api/status.last_upstream_error.code` 分辨，**没有新增 stats 键**。

### 7.2 观测量（§6.7）

`stats` 新键：`enriched` / `skipped_by_detail_quota` / `passed` / `enriched_failed` / `enrich_prefiltered` / `already_greeted`。
`/api/status.quota` 新键：`daily_detail` / `detail_batch` / `used_detail_today`（最后一个从 `actions` **实时读**）。

`enriched == 0` 时必有一条解释：`runs.error` 里若三个数全为 0，会显式写"本轮没有任何候选取到 detail…"。

### 7.3 `gap_flags` 重算

P2-A **从未写过 `jobs.gap_flags`**（`compute_gap_flags()` 此前零调用点），本批在打分阶段按**合并后的 JD** 重算并落库；写库时若 `jobs` 的 `jd_text` 为空则回读库里已有正文，**不会用空串盖掉旧轮次抓到的 JD**。

### 7.4 `greet` 的 `raw` 白名单

`raw` 恒为 `{"code": 0}`（成功/已打过）或 `{"code": <上游原始码>}`（失败），**不含 `security_id` / `data` 整包**。补页 §A 已证实上游 `_redact_sensitive` 的敏感键白名单里**没有** `security_id`，它会被原样吐出来。

### 7.5 幂等的两层

1. 我们：`actions.idempotency_key` 的 `UNIQUE`（`greet:{job_id}` / `detail:{job_id}:{YYYYMMDD}`）；
2. 上游：`<data-dir>/cache/boss_agent.db` 的 `greet_records(security_id)` → 命中返 `ALREADY_GREETED`（**不发请求**），我们归一成 `OK` 且不计入重发，另记 `stats.already_greeted`。

---

## 8. 已知限制（照实写，不粉饰）

1. **`--city 淄博` 仍不可用**：上游 `CITY_CODES` 只有 40 城，淄博不在内。本批仍靠 `target.search_url` 走 `--url`（F1 口径不变）。`batch-greet` 正因此不可用（§8）。
2. **`publish_time` 恒 `None`**：上游 `search` / `detail` 都不返回，`boss.py` 硬编码 `None`，因此"7 天内发布 +5"这一项在真机上永远不命中（`_is_fresh(None)` 判否）。这是上游限制，不是 bug。
3. **`detail` 的浏览器兜底基本无效**：我们只传 `--job-id`、不传 `--lid`，httpx 失败后上游会降级走浏览器（几十秒 + 一次预算）却大概率拿不到正文；因此一次 `detail` 失败可能连吃 2 次预算，`detail_batch=10` 之上不做压榨、熔断阈值取 3。
4. **单轮 1~2 分钟属正常**：上游串行节流 1.5~3.0s/请求 + 突发惩罚（15s 内 ≥3 次 +1.2~2.8s、45s 内 ≥6 次 +4~7s）。10 条 detail ≈ 40~70s，3 条 greet + 3 次反查 ≈ 15~30s。`/api/run/sweep` 是同步接口，**前端不许加 15 秒超时**。
5. **同一分钟内连跑两轮会撞 `REQUEST_BUDGET_EXHAUSTED`**：预算账本（`request_attempts` / `request_state`）跨进程、跨轮持久化在 `--data-dir` 下。撞上时**不是 bug，是保险丝**；不许靠换 `--data-dir` 重置窗口。
6. **`daily_detail` 当前是 50（实验期值）**：按 §6.4 / B-7，真机验收最后一步要收回 **30** 再跑一轮，确认日额度生效。
7. **`/api/status.capabilities` 现在含 `greet`、不含 `exchange`**：前端口径随之变化（`app.js` 未改，按钮渲染逻辑本来就不读 capabilities 以外的东西）。
8. **沙盒无 `git`**：tag `p2a-done` 与 `git status` 两项只能在用户机验证。

---

## 9. 自检 A 组逐条（沙盒实跑）

| # | 项 | 结果 |
|---|---|---|
| A1 | `pytest -q` | ✅ 91 passed |
| A2 | `--collect-only -q` | ✅ 91 collected |
| A3 | 既有 74 条 | ✅ 72 条与基线逐字节相同；E1/E2 两处按 §11.1 收敛（§3 附 before/after）；总数 74 → 91 不减 |
| A4 | `app/core/` | ✅ 既有 5 文件逐字节相同，仅新增 `enrich.py` |
| A5 | fake 路径 | ✅ 0 次 detail；`test_e2e_fake.py` 与基线 `SAME`；`test_sweep_fake_path_makes_no_detail_calls` 断言 `greeted==4` |
| A6 | `test_llm_gateway.py:259` | ✅ 文件与基线 `SAME`（`git diff` 含义上零命中） |
| A7 | 无 `publish_time` 依赖 | ✅ 新增代码零命中 |
| A8 | `stats` | ✅ 旧 8 键名与语义不变，5 新键 + `already_greeted` 齐 |

"""招呼文案池（闭环 1 · 节奏引擎的第一步）。

**为什么把文案从代码里搬出来**

真机数据（`data/jobpilot-new.db`，2026-11-02 / 11-03）显示：11 条招呼在 77 秒内发完，
而且每个 HR 收到的都是同一句 `您好，我对这个岗位感兴趣，希望进一步了解岗位情况。`。
"同一账号、一分钟、11 个不同 HR、一字不差"是平台行为风控最容易识别的特征，
上游 `request_delay=[1.5, 3.0]` 只挡接口级限流，挡不住这个。

所以本模块做三件事：**给池子、给变体、给确定性**。

**确定性**是刻意的：同一个 `job_id` 无论生成多少次，永远得到同一句话。
理由与 `opener_templates.yaml` 一致——重试不能换文案，否则"同一岗位发了两句"
会变成看起来更像机器的行为，也会让排障无法复现。

三条硬约束（`from_file()` 校验，违反即 `StartupError`，不静默降级）：

1. 模板数 ≥ `MIN_TEMPLATES`：池子太小等于没做
2. 渲染后 ≤ `max_chars`：与 BOSS 打招呼输入框口径一致
3. 不许编造量化资历 / 资格：正则与词表双查
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

import yaml

from app.config import StartupError, read_yaml

#: 池子下限。少于这个数，同轮出现重复文案的概率会明显上升（生日悖论）。
MIN_TEMPLATES: int = 20

#: 渲染后长度上限（与 `composer.OPENER_MAX_CHARS` 同口径，但**不复用那个常量**：
#: 那是 LLM 开场白的预算，这里是对手输框的硬边界，两者改动理由不同，耦合会互相拖累）。
DEFAULT_MAX_CHARS: int = 60

#: 编造量化资历的形态：`3年经验` / `500起案件` / `90%胜诉` / `2万+`
FORBIDDEN_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"\d+\s*(年|起|件|单|万|%)"),
)

#: 硬编码的资格/背书词（不是不能提，是不能拿它当开场白的第一句承诺）
FORBIDDEN_WORDS: tuple[str, ...] = ("执业证号", "胜诉率", "通过率", "客户名称", "办案量")

#: 缺字段时的兜底词——保证任何岗位渲染出来都是通顺的中文，不留空格或悬空标点
FALLBACK_TITLE = "这个岗位"
FALLBACK_COMPANY = "贵司"


class GreetPoolError(StartupError):
    """文案池不可用。继承 `StartupError`：配置坏了就该起不来。"""


@dataclass(frozen=True)
class GreetTemplate:
    """一条模板。`match_words` 为空 = 通用组（任何岗位都能用）。"""

    id: str
    match_words: tuple[str, ...]
    text: str

    @property
    def is_general(self) -> bool:
        return not self.match_words


class _SafeValues(dict):
    """缺键渲染成空串（与 `composer._SafeValues` 同语义）。"""

    def __missing__(self, key: str) -> str:
        return ""


def _values(job: dict[str, Any], *, short: bool) -> dict[str, Any]:
    """模板变量。`short=True` 时用最短兜底词，供"超长降级"路径重渲染。"""
    title = str(job.get("title") or "").strip() or FALLBACK_TITLE
    company = str(job.get("company") or "").strip() or FALLBACK_COMPANY
    if short:
        return {"title": FALLBACK_TITLE, "company": FALLBACK_COMPANY}
    return {"title": title, "company": company}


def render(text: str, job: dict[str, Any], *, short: bool = False) -> str:
    return text.format_map(_SafeValues(_values(job, short=short)))


def _validate_template(item: dict[str, Any], index: int, max_chars: int) -> GreetTemplate:
    """单条模板校验。任何一条不合格都不许启动（不做"跳过坏数据"的宽容）。"""
    if not isinstance(item, dict):
        raise GreetPoolError(f"greet_templates 第 {index + 1} 条不是 mapping（键值对）")

    tpl_id = str(item.get("id") or "").strip()
    if not tpl_id:
        raise GreetPoolError(f"greet_templates 第 {index + 1} 条缺少 id")

    text = str(item.get("text") or "").strip()
    if not text:
        raise GreetPoolError(f"greet_templates 的 {tpl_id} 没有 text")

    # 用"最长可能渲染"来校验长度：变量取缺省兜底词，比真实岗位名短，
    # 所以再用一个 8 字岗位名 + 8 字公司名做压力样例，取较大者。
    probe_len = max(
        len(render(text, {"title": "法务合规专员", "company": "某某科技有限公司"})),
        len(render(text, {}, short=True)),
    )
    if probe_len > max_chars:
        raise GreetPoolError(
            f"greet_templates 的 {tpl_id} 渲染后最长 {probe_len} 字，超过上限 {max_chars}：{text}"
        )

    for pattern in FORBIDDEN_PATTERNS:
        hit = pattern.search(text)
        if hit:
            raise GreetPoolError(
                f"greet_templates 的 {tpl_id} 含编造量化资历「{hit.group(0)}」：{text}。"
                f"开场白不许出现年限/案件数/胜诉率这类无法自证的量化表述。"
            )
    for word in FORBIDDEN_WORDS:
        if word in text:
            raise GreetPoolError(f"greet_templates 的 {tpl_id} 含禁用词「{word}」：{text}")

    words_raw = item.get("match_words") or []
    if not isinstance(words_raw, (list, tuple)):
        raise GreetPoolError(f"greet_templates 的 {tpl_id} 的 match_words 必须是列表")
    words = tuple(str(w).strip() for w in words_raw if str(w).strip())

    return GreetTemplate(id=tpl_id, match_words=words, text=text)


class GreetPool:
    """只读文案池。构造即校验完成，运行期无 IO、无随机。"""

    def __init__(self, templates: Iterable[GreetTemplate], max_chars: int = DEFAULT_MAX_CHARS) -> None:
        items = list(templates)
        if len(items) < MIN_TEMPLATES:
            raise GreetPoolError(
                f"文案池只有 {len(items)} 条，少于下限 {MIN_TEMPLATES} 条。"
                f"文案太少会让同轮出现重复表达，等于没解决风控问题。"
            )
        ids = [t.id for t in items]
        duplicated = {i for i in ids if ids.count(i) > 1}
        if duplicated:
            raise GreetPoolError(f"文案池模板 id 重复：{sorted(duplicated)}")

        self._templates = tuple(items)
        self.max_chars = int(max_chars)

        # 按 match_words 分组，保持文件声明顺序（组内顺序决定"顺延"的走向）
        groups: dict[tuple[str, ...], list[GreetTemplate]] = {}
        for tpl in self._templates:
            groups.setdefault(tpl.match_words, []).append(tpl)
        self._groups = groups
        # 通用组 = match_words 为空的全部模板合并（允许多组声明，行为上等价于一组）
        self._general = [t for t in self._templates if t.is_general]
        if not self._general:
            raise GreetPoolError(
                "文案池没有任何通用模板（match_words 为空）。"
                "岗位标题不命中任何关键词时必须能兜底，否则这条链会断。"
            )
        self._specific = {k: v for k, v in groups.items() if k}

    # -- 只读信息（status / 排障用） ---------------------------------- #
    def __len__(self) -> int:
        return len(self._templates)

    @property
    def template_ids(self) -> list[str]:
        return [t.id for t in self._templates]

    # -- 选择 ---------------------------------------------------------- #
    def _group_for(self, job: dict[str, Any]) -> list[GreetTemplate]:
        """命中词最多的组胜出；无命中回落通用组。并列时按文件声明顺序先到先得。"""
        title = str(job.get("title") or "")
        best: list[GreetTemplate] | None = None
        best_hits = 0
        for words, group in self._specific.items():
            hits = sum(1 for w in words if w in title)
            if hits > best_hits:
                best, best_hits = group, hits
        return best if best is not None else self._general

    def _fit(self, text: str, job: dict[str, Any]) -> str:
        """渲染并压到 `max_chars` 以内。

        降级顺序：完整渲染 → 公司名换"贵司" → 岗位名换"这个岗位" → 两者都换 → 硬截。
        硬截是最后一道保险（宁可断句，也不发一条超长消息被平台截成半个句子）。
        """
        for short in (False, True):
            rendered = render(text, job, short=short)
            if len(rendered) <= self.max_chars:
                return rendered
        return render(text, job, short=True)[: self.max_chars]

    def pick(self, job: dict[str, Any], avoid: Iterable[str] = ()) -> str:
        return self.pick_detail(job, avoid)[0]

    def pick_detail(self, job: dict[str, Any], avoid: Iterable[str] = ()) -> tuple[str, str]:
        """返回 `(文案, 模板 id)`。

        `avoid`：本批已经用过的文案。组内还有别的选择时顺延，避免"同一轮两次撞句"。
        组内只有 1 条时才允许重复（此时重复是数据决定的，不是我们偷懒）。
        """
        group = self._group_for(job)
        job_id = str(job.get("job_id") or "")
        start = int(hashlib.sha256(job_id.encode("utf-8")).hexdigest()[:8], 16) % len(group)
        used = set(avoid)

        for offset in range(len(group)):
            tpl = group[(start + offset) % len(group)]
            text = self._fit(tpl.text, job)
            if text not in used:
                return text, tpl.id

        # 命中组用完了（细分方向的组只有 3 条，一轮发 4 条就会撞）：
        # 退到通用组找没用过的——宁可换一种语气，也不要同一轮出现两句一样的话。
        for tpl in self._general:
            text = self._fit(tpl.text, job)
            if text not in used:
                return text, tpl.id

        # 整个池子都用过了（一轮发 25 条以上，不该发生）：退回确定性那一条
        tpl = group[start]
        return self._fit(tpl.text, job), tpl.id

    # -- 装载 ---------------------------------------------------------- #
    @classmethod
    def from_file(
        cls, path: str | Path, max_chars: int | None = None, *, required: bool = True
    ) -> "GreetPool | None":
        """从 yaml 装载。`required=False` 时文件缺失返回 `None`（给测试/降级路径用）。"""
        p = Path(path)
        if not p.exists():
            if required:
                raise GreetPoolError(
                    f"缺少招呼文案池：{p}。这个文件是节奏引擎的资产，"
                    f"不允许静默回落到固定文案——请从仓库恢复 config/prompts/greet_templates.yaml。"
                )
            return None

        data = read_yaml(p)
        raw = data.get("templates")
        if not isinstance(raw, list) or not raw:
            raise GreetPoolError(f"文案池文件里没有 templates 列表：{p}")

        limit = max_chars if max_chars is not None else data.get("max_chars")
        try:
            limit_num = int(limit) if limit is not None else DEFAULT_MAX_CHARS
        except (TypeError, ValueError) as exc:
            raise GreetPoolError(f"greet_templates.yaml 的 max_chars 不是整数：{limit!r}") from exc
        if limit_num <= 0:
            raise GreetPoolError(f"greet_templates.yaml 的 max_chars 必须为正数，当前 {limit_num}")

        items = [_validate_template(item, i, limit_num) for i, item in enumerate(raw)]
        return cls(items, max_chars=limit_num)


def load_pool(cfg: dict[str, Any], *, project_root: str | Path | None = None) -> GreetPool:
    """按 `config.yaml` 的 `greet` 段装载（`main.py` 启动时调一次）。

    路径相对项目根，和 `target.matcher_config` 同一套解析习惯。
    """
    greet_cfg = cfg.get("greet") or {}
    if not isinstance(greet_cfg, dict):
        raise GreetPoolError("config.yaml 的 greet 段必须是 mapping（键值对）")

    declared = str(greet_cfg.get("templates") or "config/prompts/greet_templates.yaml")
    path = Path(declared)
    if not path.is_absolute():
        root = Path(project_root) if project_root else Path(__file__).resolve().parents[2]
        path = root / path

    pool = GreetPool.from_file(path, greet_cfg.get("max_chars"))
    assert pool is not None  # required=True 时不会返回 None，仅给类型检查器看
    return pool

"""FakeAdapter —— 本地剧本，不联网、不登录、不花钱（任务书 §5.8）。

它是**一等公民**（架构 §0.1），不是测试替身：整个项目在没有账号、没有网络、
零风控风险的条件下可被审查、演示、回归。

两条硬规则：
  1. **剧本行为不依赖 `quota.daily_greet` 的数值**（C6）。配置里是 5 还是 30，
     剧本永远产出 8 个岗位、固定 4 个过线岗位；额度截断由 core 层做。
  2. **返回错误码，不抛异常**（§5.8 末段）：`script="risk"` 等注入方式给 Phase 2 风控测试复用。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

from app.adapters.base import CAPABILITIES, err_result, ok_result

# --------------------------------------------------------------------------- #
# 剧本：8 个岗位（任务书 §5.8 两张表，每一行都必须能推出确定判定）
#
# 计分口径（逻辑批 test_matcher 按这个断言精确分数）：
#   方向命中 +40 / practice_areas 每词 +8 上限 32 / 薪资重叠 +10
#   → job_001 = 82，job_002 / job_003 / job_008 = 74
# 其余加分项一律不命中：hr_active='3d'（不是 online/today）、publish_time 超过 7 天、
# company_scale 不在 preferred_scales 里。这三个值是**故意**的，改分数就挂。
# --------------------------------------------------------------------------- #
_PUBLISH_TIME_30D = (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
_HR_ACTIVE = "3d"
_SCALE = "20-99人"


def _job(
    *,
    job_id: str,
    security_id: str,
    title: str,
    company: str,
    salary: str,
    industry: str,
    hr_name: str,
    jd_text: str,
    welfare: str,
) -> dict[str, Any]:
    return {
        "job_id": job_id,
        "security_id": security_id,
        "title": title,
        "company": company,
        "salary": salary,
        "city": "淄博",
        "industry": industry,
        "hr_name": hr_name,
        "hr_active": _HR_ACTIVE,
        "company_scale": _SCALE,
        "publish_time": _PUBLISH_TIME_30D,
        "jd_text": jd_text,
        "welfare": welfare,
    }


JOBS: tuple[dict[str, Any], ...] = (
    # 1 ✅ 过线 82：enterprise_legal（标题含"法务"）+ 4 个 practice_areas 词（+32）+ 薪资重叠（+10）
    _job(
        job_id="job_001",
        security_id="sec_001",
        title="法务专员",
        company="某制造企业",
        salary="5-8K",
        industry="制造业",
        hr_name="张女士",
        jd_text=(
            "岗位职责：1、负责公司各类合同的起草、审核、归档，建立合同台账；"
            "2、组织合规检查与合规培训，跟进整改；"
            "3、参与公司风控体系建设，识别经营中的法律风险；"
            "4、参与公司治理相关制度起草，配合三会运作。"
            "任职要求：法学本科及以上，两年以上企业法务经验优先。"
        ),
        welfare="五险一金、双休",
    ),
    # 2 ✅ 过线 74：law_firm_junior（标题含"律师助理"）+ 3 个词（+24）+ 薪资重叠（+10）
    #    4-6K 的上限 6000 与 expected_salary(5000-12000) 重叠 → +10；
    #    下限 4000 ≥ law_firm_junior.salary_floor(3000)，硬淘汰不命中。
    _job(
        job_id="job_002",
        security_id="sec_002",
        title="律师助理",
        company="XX律师事务所",
        salary="4-6K",
        industry="法律服务",
        hr_name="李律师",
        jd_text=(
            "岗位职责：1、协助律师整理卷宗、准备开庭材料；"
            "2、协助审核合同并出具初步法律意见；"
            "3、协助开展合规自查，整理检查记录；"
            "4、协助梳理项目法律风险，做风控台账。"
            "任职要求：法学本科，通过法律职业资格考试者优先，接受应届毕业生。"
        ),
        welfare="五险、带薪年假",
    ),
    # 3 ✅ 过线 74：enterprise_legal（标题含"合同"）+ 3 个词（+24）+ 薪资重叠（+10）
    _job(
        job_id="job_003",
        security_id="sec_003",
        title="合同管理",
        company="某集团公司",
        salary="6-9K",
        industry="综合集团",
        hr_name="王经理",
        jd_text=(
            "岗位职责：1、负责集团合同审核、合同模板与台账管理；"
            "2、参与股权投资项目文件审查与工商变更；"
            "3、参与公司治理与三会事务，起草决议文件；"
            "4、对接外部律师，跟进案件进展。"
            "任职要求：法学本科，三年以上合同管理经验。"
        ),
        welfare="五险一金、年终奖",
    ),
    # 4 ❌ 硬淘汰：is_employer_blacklist 命中"信息咨询"
    _job(
        job_id="job_004",
        security_id="sec_004",
        title="法务专员",
        company="某信息咨询有限公司",
        salary="无责底薪 8000",
        industry="咨询",
        hr_name="赵女士",
        jd_text=(
            "岗位职责：1、负责合同审核与归档；2、协助处理客户纠纷；3、完成上级交办的其他工作。"
            "任职要求：法学相关专业，沟通能力强，有无经验均可。"
        ),
        welfare="双休、无责底薪",
    ),
    # 5 ❌ 硬淘汰：is_jd_blacklist 命中"外访"（R4-8：先于 require_license_check 判定）
    _job(
        job_id="job_005",
        security_id="sec_005",
        title="债务处理专员",
        company="某资产管理公司",
        salary="提成",
        industry="资产管理",
        hr_name="孙经理",
        jd_text=(
            "岗位职责：1、负责不良资产的清收与处置，完成月度回款目标；"
            "2、按计划进行客户外访与沟通，跟进还款进度；"
            "3、整理欠款台账，上报处置情况。"
            "任职要求：能接受外访，有相关经验者优先。"
        ),
        welfare="提成上不封顶",
    ),
    # 6 ❌ 低分淘汰（**不是**硬淘汰）：partner_track 的 enabled=false，且不含任何 practice_areas 词
    #    标题必须是"授薪合伙人"——写"法务合伙人"会被 enterprise_legal 的 title_patterns("法务") 误归并 +40
    _job(
        job_id="job_006",
        security_id="sec_006",
        title="授薪合伙人",
        company="某企业",
        salary="面议",
        industry="法律服务",
        hr_name="周先生",
        jd_text=(
            "岗位职责：1、独立承办团队交办的业务，对案件质量负责；"
            "2、参与团队日常管理与青年律师培养；"
            "3、完成年度业务目标与客户维护。"
            "任职要求：持有律师执业证，五年以上执业经验，具备稳定案源。"
        ),
        welfare="面议",
    ),
    # 7 ❌ 硬淘汰：enterprise_legal.require_salary_floor=true 且 salary_floor=4000，2-3K 的下限 2000 < 4000
    #    （R4-2：不再用 exclude_title_words，那个词表只服务 Phase 2 的 search 过滤）
    _job(
        job_id="job_007",
        security_id="sec_007",
        title="实习法务",
        company="某公司",
        salary="2-3K",
        industry="制造业",
        hr_name="吴女士",
        jd_text=(
            "岗位职责：1、协助合同审核与归档；2、协助合规材料整理；3、协助部门日常事务。"
            "任职要求：法学专业在校生，每周到岗四天以上。"
        ),
        welfare="实习证明",
    ),
    # 8 ✅ 过线 74：enterprise_legal（标题含"合规"）+ 3 个词（+24）+ 薪资重叠（+10）
    _job(
        job_id="job_008",
        security_id="sec_008",
        title="合规专员",
        company="某科技公司",
        salary="8-12K",
        industry="科技",
        hr_name="郑女士",
        jd_text=(
            "岗位职责：1、负责公司合规体系建设与合规审查；"
            "2、负责业务风控流程设计与风险监测；"
            "3、审核对外合同与协议文本。"
            "任职要求：法学或相关专业本科，三年以上合规或风控经验。"
        ),
        welfare="五险一金、弹性工作",
    ),
)

# 剧本固定：4 个过线岗位（1/2/3/8），全部打招呼；额度截断由 core 层做
ONLINE_JOB_IDS: tuple[str, ...] = ("job_001", "job_002", "job_003", "job_008")

# HR 回话剧本（§5.8：chatmsg(#1) 要简历 / chatmsg(#2) 想加微信 / chatmsg(#3) 无回复）
HR_REPLIES: dict[str, str] = {
    "job_001": "你好，方便发简历吗",
    "job_002": "我们这边想先微信聊，方便加个微信吗",
}

# 交换成功后对方发来的号码（A5 未验证，剧本先固定一条）
_WECHAT_MESSAGE: dict[str, str] = {"job_002": "我的微信是 lvshi_zhang888"}

SCRIPTS: frozenset[str] = frozenset({"happy", "risk", "limit", "unconfirmed"})


class FakeAdapter:
    """本地剧本适配器。

    构造参数 `script`：
      - `"happy"`        正常
      - `"risk"`         greet 返回 `ACCOUNT_RISK`
      - `"limit"`        greet 返回 `GREET_LIMIT`
      - `"unconfirmed"`  exchange 返回 `sent=False, error_code=None`
    """

    def __init__(self, script: str = "happy", jobs: list[dict[str, Any]] | None = None) -> None:
        if script not in SCRIPTS:
            raise ValueError(f"未知剧本 {script!r}，可选：{sorted(SCRIPTS)}")
        self.script = script
        self._jobs: list[dict[str, Any]] = [dict(job) for job in (jobs or JOBS)]
        self.capabilities: set[str] = set(CAPABILITIES)
        # job_id -> uid（打招呼之后才有 uid，这也是幂等键只能用 job_id 的原因）
        self._greeted: dict[str, str] = {}
        self._greeting_text: dict[str, str] = {}
        self._exchange_done: set[str] = set()
        self.calls: list[str] = []          # 排障：记录调用顺序

    # -- 探针 ---------------------------------------------------------- #
    def probe(self) -> set[str]:
        self.calls.append("probe")
        return set(self.capabilities)

    # -- 读 ------------------------------------------------------------ #
    def search(self, query: str = "", **filters: Any) -> list[dict[str, Any]]:
        self.calls.append(f"search:{query}")
        return [dict(job) for job in self._jobs]

    def chat(self, **filters: Any) -> list[dict[str, Any]]:
        self.calls.append("chat")
        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        conversations: list[dict[str, Any]] = []
        for job_id, uid in self._greeted.items():
            job = self._job(job_id)
            reply = HR_REPLIES.get(job_id)
            wechat = _WECHAT_MESSAGE.get(job_id) if job_id in self._exchange_done else None
            last = wechat or reply or self._greeting_text.get(job_id, "")
            last_from = "hr" if (wechat or reply) else "me"
            conversations.append(
                {
                    "uid": uid,
                    "job_id": job_id,
                    "hr_name": job.get("hr_name"),
                    "company": job.get("company"),
                    "last_msg": last,
                    "last_msg_from": last_from,
                    "last_msg_at": now,
                    "unread": 1 if last_from == "hr" else 0,
                }
            )
        return conversations

    def chatmsg(self, uid: str, raw: bool = False) -> list[dict[str, Any]]:
        self.calls.append(f"chatmsg:{uid}")
        job_id = self._job_id_of(uid)
        if job_id is None:
            return []

        now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        messages: list[dict[str, Any]] = [
            {
                "platform_mid": f"{uid}-out-1",
                "direction": "out",
                "msg_type": "text",
                "content": self._greeting_text.get(job_id, ""),
                "created_at": now,
            }
        ]
        reply = HR_REPLIES.get(job_id)
        if reply:
            messages.append(
                {
                    "platform_mid": f"{uid}-in-1",
                    "direction": "in",
                    "msg_type": "text",
                    "content": reply,
                    "created_at": now,
                }
            )
        if job_id in self._exchange_done and job_id in _WECHAT_MESSAGE:
            messages.append(
                {
                    "platform_mid": f"{uid}-in-2",
                    "direction": "in",
                    "msg_type": "text",
                    "content": _WECHAT_MESSAGE[job_id],
                    "created_at": now,
                }
            )
        return messages

    # -- 写 ------------------------------------------------------------ #
    def greet(self, security_id: str, job_id: str, message: str) -> dict[str, Any]:
        self.calls.append(f"greet:{job_id}")
        job = self._job(job_id, by_security_id=security_id)
        if job is None:
            return err_result("JOB_NOT_FOUND", f"剧本里没有这个岗位：{job_id}")
        if job_id in self._greeted:
            return err_result("ALREADY_GREETED", "这个岗位已经打过招呼了")

        if self.script == "risk":
            return err_result("ACCOUNT_RISK", "剧本注入：命中风控（ACCOUNT_RISK）")
        if self.script == "limit":
            return err_result("GREET_LIMIT", "剧本注入：今日打招呼额度已用尽（GREET_LIMIT）")

        uid = f"fake_{job_id}"
        self._greeted[job_id] = uid
        self._greeting_text[job_id] = message
        return ok_result(f"已向 {job.get('company')} 的 {job.get('hr_name')} 打招呼", raw={"fake": True})

    def exchange(self, uid: str, type: str = "wechat") -> dict[str, Any]:
        self.calls.append(f"exchange:{uid}")
        job_id = self._job_id_of(uid)
        if job_id is None:
            return err_result("UID_NOT_FOUND", f"剧本里没有这个会话：{uid}")
        if job_id in self._exchange_done:
            return err_result("ALREADY_EXCHANGED", "这个会话已经交换过了")

        if self.script == "unconfirmed":
            # 发出了但无法确认：调用方必须按 UNCONFIRMED 处理，**不能重试**
            return err_result(None, "剧本注入：请求已发出但回读不到结果，按 UNCONFIRMED 处理，禁止重试")
        if self.script == "risk":
            return err_result("ACCOUNT_RISK", "剧本注入：命中风控（ACCOUNT_RISK）")

        self._exchange_done.add(job_id)
        return ok_result("交换请求已发出", raw={"fake": True, "type": type})

    # -- 内部 ---------------------------------------------------------- #
    def _job(self, job_id: str, by_security_id: str | None = None) -> dict[str, Any] | None:
        for job in self._jobs:
            if job.get("job_id") == job_id:
                if by_security_id is not None and job.get("security_id") != by_security_id:
                    return None
                return job
        return None

    def _job_id_of(self, uid: str) -> str | None:
        for job_id, known_uid in self._greeted.items():
            if known_uid == uid:
                return job_id
        return None

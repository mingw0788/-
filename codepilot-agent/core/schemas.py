"""Agent 之间传递的核心数据结构。

设计要点：
1. ``Issue`` 与 ``LocatedIssue`` 刻意分成两个模型，靠 ``issue_id`` 关联。定位失败因此是
   一个显式的 ``match_status=unresolved`` + ``line_start=None``，而不是让下游拿到一个假的
   行号 —— 这是需求里「严禁编造行号」在类型层面的落地。
2. 所有枚举都锁死取值，Agent 输出的枚举值一旦越界会在校验阶段立刻暴露，不会悄悄流到报告里。
3. ``fingerprint`` 用于回归阶段判断「新问题是否就是原问题」。它基于归一化后的锚点 +
   分类，而不是 LLM 生成的 ``issue_id`` —— 每次重新审查都会生成新 UUID，靠 UUID 无法比对。
"""
from __future__ import annotations

import hashlib
import re
from enum import Enum

from pydantic import BaseModel, Field, root_validator, validator

SCHEMA_VERSION = "1.0"


class Category(str, Enum):
    SECURITY = "Security"
    LOGIC = "Logic"
    PERFORMANCE = "Performance"
    STYLE = "Style"


class Priority(str, Enum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class Severity(str, Enum):
    BLOCKER = "Blocker"
    CRITICAL = "Critical"
    MAJOR = "Major"
    MINOR = "Minor"


class MatchStatus(str, Enum):
    EXACT = "exact"
    NORMALIZED = "normalized"
    FUZZY = "fuzzy"
    UNRESOLVED = "unresolved"


class FixStatus(str, Enum):
    PENDING = "pending"
    APPLIED = "applied"
    VERIFIED = "verified"
    REGRESSED = "regressed"
    ESCALATED = "escalated"
    SKIPPED_CROSS_FILE = "skipped_cross_file"
    PATCH_FAILED = "patch_failed"


class RunStatus(str, Enum):
    PASSED = "Passed"
    BLOCKED = "Blocked"


# 维度优先级（从高到低），用于报告排序与「安全 > 逻辑 > 性能 > 规范」的落地。
CATEGORY_RANK = {
    Category.SECURITY: 0,
    Category.LOGIC: 1,
    Category.PERFORMANCE: 2,
    Category.STYLE: 3,
}
PRIORITY_RANK = {Priority.P0: 0, Priority.P1: 1, Priority.P2: 2, Priority.P3: 3}

# 需求指定的重试上限：P0/P1 三次，P2 两次，P3 一次。
RETRY_LIMITS = {Priority.P0: 3, Priority.P1: 3, Priority.P2: 2, Priority.P3: 1}

# 会导致阻塞合并的优先级。
BLOCKING_PRIORITIES = frozenset({Priority.P0, Priority.P1})


_WS_RE = re.compile(r"\s+")


def normalize_snippet(text):
    """把代码片段归一化成可比较的指纹基底：统一空白、去首尾。

    只做空白归一化，不动标识符大小写 —— Python 大小写敏感，改小写会把 ``Foo`` 和 ``foo``
    错判成同一处代码。
    """
    if not text:
        return ""
    return _WS_RE.sub(" ", text).strip()


def compute_fingerprint(category, code_anchor):
    """由「分类 + 归一化锚点」派生稳定指纹，用于跨轮次比对同一个问题。"""
    basis = "{cat}::{anchor}".format(
        cat=category.value if isinstance(category, Category) else str(category),
        anchor=normalize_snippet(code_anchor),
    )
    return hashlib.sha1(basis.encode("utf-8")).hexdigest()[:16]


class StrictModel(BaseModel):
    """统一禁止未声明字段，让 Agent 的多余输出变成显式错误而不是被静默吞掉。"""

    class Config:
        extra = "forbid"
        use_enum_values = False
        validate_assignment = True


class ProjectContext(StrictModel):
    """项目上下文快照，随代码一起喂给审查与修复 Agent。"""

    project_name: str = "unnamed-project"
    dependency_versions: dict = Field(default_factory=dict)
    internal_coding_standards: list = Field(default_factory=list)
    related_files: list = Field(default_factory=list)
    root_path: str = ""
    primary_language: str = "python"

    @validator("related_files")
    def _dedupe_related_files(cls, value):
        seen = set()
        result = []
        for item in value or []:
            if item and item not in seen:
                seen.add(item)
                result.append(item)
        return result


class Issue(StrictModel):
    """审查 Agent 的结构化输出。行号由定位层派生，所以这里不含任何行号字段。"""

    issue_id: str
    category: Category
    priority: Priority
    severity: Severity
    code_anchor: str
    description: str
    suggestion: str = ""
    file_path: str = ""
    # 由 compute_fingerprint 在校验后填入，不信任外部传入。
    fingerprint: str = ""

    @validator("code_anchor")
    def _anchor_not_blank(cls, value):
        if not value or not value.strip():
            raise ValueError("code_anchor 不能为空：锚点为空则定位层无法工作")
        return value

    @root_validator(skip_on_failure=True)
    def _fill_fingerprint(cls, values):
        category = values.get("category")
        anchor = values.get("code_anchor")
        if category is not None and anchor:
            values["fingerprint"] = compute_fingerprint(category, anchor)
        return values

    def sort_key(self):
        return (
            PRIORITY_RANK.get(self.priority, 99),
            CATEGORY_RANK.get(self.category, 99),
        )

    @property
    def is_blocking(self):
        return self.priority in BLOCKING_PRIORITIES


class IssueList(StrictModel):
    """审查 Agent 的顶层输出容器。"""

    issues: list = Field(default_factory=list)
    notes: str = ""

    @validator("issues")
    def _each_is_issue(cls, value):
        result = []
        for item in value or []:
            result.append(item if isinstance(item, Issue) else Issue(**item))
        return result


class LocatedIssue(StrictModel):
    """定位层输出：把锚点换算成确定性的行号。"""

    issue_id: str
    file_path: str = ""
    line_start: int = None
    line_end: int = None
    match_status: MatchStatus = MatchStatus.UNRESOLVED
    match_method: str = ""
    confidence: float = 0.0
    matched_text: str = ""

    @root_validator(skip_on_failure=True)
    def _enforce_line_consistency(cls, values):
        status = values.get("match_status")
        start = values.get("line_start")
        end = values.get("line_end")

        if status == MatchStatus.UNRESOLVED:
            # unresolved 必须没有行号。这是「严禁凭空编造行号」的硬约束。
            if start is not None or end is not None:
                raise ValueError("match_status=unresolved 时 line_start/line_end 必须为 None")
            return values

        if start is None:
            raise ValueError("match_status={} 时 line_start 不能为 None".format(status))

        if end is None:
            values["line_end"] = start
        elif end < start:
            raise ValueError("line_end 不能小于 line_start")
        return values

    @property
    def is_resolved(self):
        return self.match_status != MatchStatus.UNRESOLVED

    def describe_location(self):
        if not self.is_resolved:
            return "未定位"
        if self.line_start == self.line_end:
            return "第 {} 行".format(self.line_start)
        return "第 {}-{} 行".format(self.line_start, self.line_end)


class LocatedIssueList(StrictModel):
    located_issues: list = Field(default_factory=list)
    notes: str = ""

    @validator("located_issues")
    def _each_is_located(cls, value):
        result = []
        for item in value or []:
            result.append(item if isinstance(item, LocatedIssue) else LocatedIssue(**item))
        return result


class Fix(StrictModel):
    """修复 Agent 输出：可验证的 Git Patch，绝不输出完整替换代码。"""

    issue_id: str
    requires_cross_file: bool = False
    patch: str = ""
    explanation: str = ""

    @root_validator(skip_on_failure=True)
    def _patch_presence(cls, values):
        cross_file = values.get("requires_cross_file")
        patch = values.get("patch") or ""
        if not cross_file and not patch.strip():
            raise ValueError("requires_cross_file=False 时 patch 不能为空")
        return values


class FixList(StrictModel):
    fixes: list = Field(default_factory=list)
    notes: str = ""

    @validator("fixes")
    def _each_is_fix(cls, value):
        result = []
        for item in value or []:
            result.append(item if isinstance(item, Fix) else Fix(**item))
        return result


class FixAttempt(StrictModel):
    """单个 issue 的修复尝试轨迹，回归闭环的可观测性靠它。"""

    issue_id: str
    fingerprint: str = ""
    priority: Priority = Priority.P3
    category: Category = Category.STYLE
    attempts: int = 0
    max_attempts: int = 1
    status: FixStatus = FixStatus.PENDING
    failure_reason: str = ""
    applied_range: list = Field(default_factory=list)
    # 回归检查的两面：原问题是否消失（verified），以及是否引入了新问题（regression_passed）。
    # 只看前者会让「修好了 A 但弄坏了 B」的补丁被判为成功。
    regression_passed: bool = True
    introduced_fingerprints: list = Field(default_factory=list)
    retry_trace: list = Field(default_factory=list)

    @property
    def introduced_regression(self):
        return bool(self.introduced_fingerprints)


class WorkflowState(str, Enum):
    PENDING = "PENDING"
    REVIEWING = "REVIEWING"
    LOCATING = "LOCATING"
    FIXING = "FIXING"
    REGRESSING = "REGRESSING"
    REPORTING = "REPORTING"
    DONE = "DONE"
    ESCALATED = "ESCALATED"
    FAILED = "FAILED"


class ReviewReport(StrictModel):
    """最终报告，也是持久化到 Artifact 的原始 JSON。"""

    schema_version: str = SCHEMA_VERSION
    project_name: str = ""
    target_file: str = ""
    status: RunStatus = RunStatus.BLOCKED
    workflow_state: WorkflowState = WorkflowState.DONE
    issues: list = Field(default_factory=list)
    located_issues: list = Field(default_factory=list)
    fixes: list = Field(default_factory=list)
    attempts: list = Field(default_factory=list)
    unresolved_issue_ids: list = Field(default_factory=list)
    escalated_issue_ids: list = Field(default_factory=list)
    cross_file_issue_ids: list = Field(default_factory=list)
    # 补丁引入的新问题指纹，与原有的 unresolved/escalated 分开记录 ——
    # 「没修好」和「修坏了」是两种不同的失败，报告和重试策略都不同。
    regression_issue_ids: list = Field(default_factory=list)
    applied_patch_count: int = 0
    llm_calls: int = 0
    started_at: str = ""
    finished_at: str = ""
    notes: str = ""

    # 从快照 JSON 恢复时，嵌套列表会是一堆 dict。``list`` 字段默认不做元素校验，
    # 于是一份恢复出来的报告里 ``issues`` / ``attempts`` 里躺着 dict，
    # 任何 ``issue.is_blocking`` / ``attempt.status`` 都会炸 —— 而
    # ``blocking_escalation`` 正是这么访问的。``--resume`` 会走到这条路，
    # 所以这里显式把元素还原成模型，与 IssueList/LocatedIssueList/FixList 的做法一致。

    @validator("issues")
    def _issues_are_models(cls, value):
        return [item if isinstance(item, Issue) else Issue(**item) for item in value or []]

    @validator("located_issues")
    def _located_are_models(cls, value):
        return [
            item if isinstance(item, LocatedIssue) else LocatedIssue(**item)
            for item in value or []
        ]

    @validator("fixes")
    def _fixes_are_models(cls, value):
        return [item if isinstance(item, Fix) else Fix(**item) for item in value or []]

    @validator("attempts")
    def _attempts_are_models(cls, value):
        return [
            item if isinstance(item, FixAttempt) else FixAttempt(**item)
            for item in value or []
        ]

    def issue_by_id(self):
        return {issue.issue_id: issue for issue in self.issues}

    def located_by_id(self):
        return {item.issue_id: item for item in self.located_issues}

    def sorted_issues(self):
        return sorted(self.issues, key=lambda issue: issue.sort_key())

    @property
    def blocking_issues(self):
        """高危：P0/P1，阻塞合并，报告中完整展开。"""
        return [i for i in self.sorted_issues() if i.is_blocking]

    @property
    def non_blocking_issues(self):
        """中低危：P2/P3，报告中以表格呈现。"""
        return [i for i in self.sorted_issues() if not i.is_blocking]

    @property
    def has_escalation(self):
        return bool(self.escalated_issue_ids)

    @property
    def blocking_escalation(self):
        """只有高危问题未通过才真正阻塞合并；P2/P3 耗尽重试不阻塞。

        「未通过」有三类，**三类都要看**：重试耗尽、未定位、补丁无法应用。

        漏掉第三类会开一个非常安静的假通过：高危问题被找到、也被准确定位，只是补丁
        打不上 → 流程只记一个 ``PATCH_FAILED``，它既不在 ``escalated_issue_ids`` 也不在
        ``unresolved_issue_ids`` 里 → ``status`` 算成 ``PASSED``、退出码 0、
        ``blocking.txt`` 写成 ``false``，一个未修复的 P0 就这么通过了 CI 门禁 ——
        而报告正文里「未通过项」还写着「该问题为高危，阻塞合并」，自相矛盾。
        """
        by_id = self.issue_by_id()
        for issue_id in self.escalated_issue_ids:
            issue = by_id.get(issue_id)
            if issue is not None and issue.is_blocking:
                return True
        for issue_id in self.unresolved_issue_ids:
            issue = by_id.get(issue_id)
            if issue is not None and issue.is_blocking:
                return True
        for attempt in self.attempts:
            if attempt.status == FixStatus.PATCH_FAILED:
                issue = by_id.get(attempt.issue_id)
                if issue is not None and issue.is_blocking:
                    return True
        return False

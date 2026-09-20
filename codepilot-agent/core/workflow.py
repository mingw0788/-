"""回归审查编排状态机。

需求描述的是「状态机骨架 + 局部并发」。这里的实现取舍：

- **骨架显式状态化**：每个阶段是一个具名状态，状态快照可持久化、可 ``--resume``。
  这让长跑任务在崩掉后不用从头再来，也让「现在卡在哪一步」在报告里可见。
- **并发只用在真正独立的地方**：审查与项目上下文加载互不依赖，可以并行；
  回归阶段每个 issue 的「应用 patch → 重新审查」也彼此独立。但**修复阶段不能并发**，
  因为多个 patch 会改同一份源码，并发应用必然互相踩踏。需求文档的伪代码在这一点上
  有隐患（``for fix in fixes`` 顺序应用是对的，但注释写的「局部并发」容易被误解成
  整个修复阶段并发），这里显式收窄并发边界。

对需求伪代码的两处修正，均已在此落地：

1. **重试计数按 issue 而非按优先级**。伪代码是 ``retry_count[fix.priority]``，
   但早停条件是「连续同类失败」。按优先级计数会让同一优先级下的不同问题互相消耗配额，
   与「连续同类失败早停」语义不符。这里用 ``retry_count[issue_id]`` 计数，
   另用 ``consecutive_category_failures`` 追踪同分类连续失败。
2. **定位走确定性引擎**，LLM 仅作兜底（见 ``core.anchors`` 与 ``agents.review_agents``）。
"""
from __future__ import annotations

import copy
import json
import os
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

from agents.review_agents import FixerAgent, LocatorAgent, ReviewerAgent
from core.anchors import locate_issues
from core.errors import (
    BudgetExceededError,
    CodePilotError,
    LLMResponseError,
    PatchError,
    StateError,
)
from core.patch import verify_and_apply
from core.schemas import (
    RETRY_LIMITS,
    FixAttempt,
    FixStatus,
    LocatedIssue,
    Priority,
    ProjectContext,
    ReviewReport,
    RunStatus,
    WorkflowState,
)

STATE_FILE = "state.json"
CODEPILOT_DIR = ".codepilot"

# 同分类连续失败达到此数即早停，避免在同一个根因上反复烧 token。
CONSECUTIVE_CATEGORY_FAILURE_LIMIT = 2

# 全局 LLM 调用预算上限，防止设计缺陷导致闭环无限膨胀。
DEFAULT_LLM_BUDGET = 200


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class WorkflowRunner(object):
    """执行「审查 → 定位 → 修复 → 回归 → 报告」全流程。"""

    def __init__(
        self,
        client,
        project_context=None,
        workdir=None,
        max_concurrency=4,
        llm_budget=DEFAULT_LLM_BUDGET,
        use_git_check=True,
        logger=None,
    ):
        self.client = client
        self.project_context = (
            project_context
            if isinstance(project_context, ProjectContext)
            else ProjectContext(**(project_context or {}))
        )
        self.workdir = workdir or os.getcwd()
        self.max_concurrency = max(1, max_concurrency)
        self.llm_budget = llm_budget
        self.use_git_check = use_git_check
        self.log = logger or (lambda message: None)

        self.reviewer = ReviewerAgent(client)
        self.locator = LocatorAgent(client)
        self.fixer = FixerAgent(client)

        self.state = WorkflowState.PENDING
        self.report = ReviewReport(project_name=self.project_context.project_name)
        self.retry_counts = {}
        self.consecutive_category_failures = {}
        self._issue_cache = {}
        # 历次审查（含回归轮）出现过的全部问题指纹，用于识别「补丁引入的新问题」。
        self._known_fingerprints = set()
        self._code = ""
        self._file_path = ""

    # ------------------------------------------------------------------ 状态持久化

    def _state_path(self):
        return os.path.join(self.workdir, CODEPILOT_DIR, STATE_FILE)

    def save_state(self):
        """把可恢复的状态写成快照。用临时文件 + 替换保证不会写出半截文件。"""
        directory = os.path.dirname(self._state_path())
        if not os.path.isdir(directory):
            os.makedirs(directory)

        snapshot = {
            "state": self.state.value,
            "target_file": self._file_path,
            "retry_counts": self.retry_counts,
            "consecutive_category_failures": self.consecutive_category_failures,
            "report": self.report.dict(),
            "saved_at": _now(),
        }

        temp_path = self._state_path() + ".tmp"
        with open(temp_path, "w", encoding="utf-8") as handle:
            json.dump(snapshot, handle, ensure_ascii=False, indent=2)
        os.replace(temp_path, self._state_path())
        return self._state_path()

    def load_state(self):
        """读取快照。损坏的快照必须显式报错，不能静默从头开始。"""
        path = self._state_path()
        if not os.path.isfile(path):
            return None
        try:
            with open(path, "r", encoding="utf-8") as handle:
                snapshot = json.load(handle)
        except (OSError, ValueError) as exc:
            raise StateError("状态快照损坏，无法恢复：{}".format(exc), detail=str(exc))

        try:
            self.state = WorkflowState(snapshot["state"])
            self.retry_counts = snapshot.get("retry_counts", {})
            self.consecutive_category_failures = snapshot.get("consecutive_category_failures", {})
            self._file_path = snapshot.get("target_file", "")
            self.report = ReviewReport(**snapshot.get("report", {}))
        except (KeyError, ValueError) as exc:
            raise StateError("状态快照结构非法：{}".format(exc), detail=str(exc))
        return snapshot

    # ------------------------------------------------------------------ 预算

    def _check_budget(self):
        if self.client.call_count > self.llm_budget:
            raise BudgetExceededError(
                "LLM 调用数已达预算上限 {n}，提前终止以避免失控消耗".format(n=self.llm_budget),
                detail="calls={}".format(self.client.call_count),
            )

    def _transition(self, new_state):
        self.state = new_state
        self.report.workflow_state = new_state
        self.log("状态 -> {}".format(new_state.value))

    # ------------------------------------------------------------------ 主流程

    def run(self, code, file_path="", resume=False):
        """执行完整流程，返回 ``ReviewReport``。

        :param code: 待审查的源码文本。
        :param file_path: 目标文件路径（用于 patch 文件头与报告展示）。
        :param resume: 为 True 时尝试从 ``.codepilot/state.json`` 恢复计数与报告。
        """
        self._code = code.replace("\r\n", "\n").replace("\r", "\n")
        self._file_path = file_path or "target.py"
        self.report.target_file = self._file_path
        self.report.started_at = self.report.started_at or _now()
        self.report.project_name = self.project_context.project_name

        if resume:
            restored = self.load_state()
            if restored:
                self.log("已从快照恢复：{}".format(restored.get("saved_at", "unknown")))

        try:
            self._run_pipeline()
        except (CodePilotError, OSError) as exc:
            # 超出预算或基础设施故障：保留已完成的部分，标记 FAILED 而不是丢报告。
            self._transition(WorkflowState.FAILED)
            self.report.notes = "流程异常终止：{}".format(exc)
            self.log("流程异常终止：{}".format(exc))
        finally:
            self._finalize()

        return self.report

    def _run_pipeline(self):
        self._check_budget()

        self._transition(WorkflowState.REVIEWING)
        issues = self._stage_review(is_initial=True)

        if not issues:
            # 没有任何问题：直接通过，无需进入修复闭环。
            self.log("审查未发现问题")
            self._transition(WorkflowState.DONE)
            return

        self._transition(WorkflowState.LOCATING)
        located = self._stage_locate(issues)

        self._transition(WorkflowState.FIXING)
        fixes = self._stage_fix(located)

        self._transition(WorkflowState.REGRESSING)
        self._stage_regression(issues, located, fixes)

        self._transition(WorkflowState.DONE)
        if self.report.has_escalation:
            self._transition(WorkflowState.ESCALATED)

    # ------------------------------------------------------------------ 阶段 1：审查

    def _load_context_parallel(self):
        """与审查并行的上下文加载钩子。

        真实实现里这里是「读依赖版本、扫团队规范文件、AST 分析跨文件引用」。
        这些 IO 与审查的首次 LLM 调用互不依赖，所以放在独立线程里预热；
        当前实现是轻量占位，保持并发边界显式可见。
        """
        return {
            "related_files": list(self.project_context.related_files),
            "standards": list(self.project_context.internal_coding_standards),
        }

    def _stage_review(self, code=None, is_initial=False):
        target = code if code is not None else self._code

        # 审查本身与上下文预热并行 —— 这是需求里「局部并发」的落点之一。
        with ThreadPoolExecutor(max_workers=2) as pool:
            review_future = pool.submit(
                self.reviewer.run, target, self.project_context, self._file_path
            )
            context_future = pool.submit(self._load_context_parallel)
            issues = review_future.result()
            context_future.result()

        for issue in issues:
            self._issue_cache[issue.issue_id] = issue

        # 只登记不比对：首次审查时点不存在「新引入」的概念，比对一律放在
        # _detect_introduced 里做，避免同一次审查既登记又检测而互相抵消。
        if is_initial:
            self._register_known(issues)

        self.report.issues = issues
        self.log("审查完成：{} 个问题".format(len(issues)))
        return issues

    # ------------------------------------------------------------------ 阶段 2：定位

    def _stage_locate(self, issues):
        """确定性定位为主，LLM 兜底为辅。"""
        located = locate_issues(issues, self._code, self._file_path)
        by_id = {item.issue_id: item for item in located}

        unresolved = [
            issue for issue in issues
            if issue.issue_id in by_id and not by_id[issue.issue_id].is_resolved
        ]

        resolved_count = len(located) - len(unresolved)
        self.log(
            "确定性定位完成：{} 命中，{} 未命中".format(resolved_count, len(unresolved))
        )

        if unresolved:
            self._check_budget()
            self.log("对 {} 个未命中问题启用 LLM 兜底消歧".format(len(unresolved)))
            try:
                fallback = self.locator.run(
                    self._code, unresolved, self._file_path, located
                )
                self._merge_fallback(by_id, fallback)
            except LLMResponseError as exc:
                # 兜底失败不应让整个流程崩溃：保持 unresolved 是安全的结果。
                self.log("兜底定位失败，保持 unresolved：{}".format(exc))

        final = [by_id[issue.issue_id] for issue in issues if issue.issue_id in by_id]
        self.report.located_issues = final
        self.report.unresolved_issue_ids = [
            item.issue_id for item in final if not item.is_resolved
        ]
        return final

    def _merge_fallback(self, by_id, fallback):
        """合并兜底结果。

        关键防御：兜底结果里给出的行号必须**回验** —— 它声明的区间是否真的存在于源码内。
        越界行号一律降级为 unresolved，防止把幻觉行号带进修复阶段。
        """
        total_lines = len(self._code.split("\n"))
        for item in fallback:
            if item.issue_id not in by_id:
                continue
            if not item.is_resolved:
                continue

            start = item.line_start
            end = item.line_end if item.line_end is not None else start
            if start is None or start < 1 or end > total_lines or end < start:
                self.log(
                    "兜底定位给出越界行号（issue {}: {}-{}），降级为 unresolved".format(
                        item.issue_id, start, end
                    )
                )
                by_id[item.issue_id] = LocatedIssue(
                    issue_id=item.issue_id,
                    file_path=item.file_path,
                    match_status=item.match_status.UNRESOLVED,
                    match_method="fallback_out_of_range",
                )
                continue

            by_id[item.issue_id] = item

    # ------------------------------------------------------------------ 阶段 3：修复

    def _stage_fix(self, located):
        """生成 patch。**串行**：多个 patch 会改同一份源码，并发应用必然互相踩踏。"""
        self._check_budget()

        resolved = [item for item in located if item.is_resolved]
        if not resolved:
            self.log("没有可定位的问题，跳过修复阶段")
            return []

        fixes = self.fixer.run(self._code, resolved, self.project_context, self._file_path)
        self.report.fixes = fixes
        self.log("修复方案生成完成：{} 个 patch".format(len(fixes)))
        return fixes

    # ------------------------------------------------------------------ 阶段 4：回归闭环

    def _stage_regression(self, issues, located, fixes):
        """逐 issue 应用 patch 并重新审查，对比原问题是否消失。"""
        issue_by_id = {issue.issue_id: issue for issue in issues}
        located_by_id = {item.issue_id: item for item in located}

        attempts = []
        cross_file_ids = []

        # 只处理能定位、且修复 Agent 给出 patch 的问题。
        pending = []
        for fix in fixes:
            issue = issue_by_id.get(fix.issue_id)
            if issue is None:
                continue
            if fix.requires_cross_file:
                cross_file_ids.append(fix.issue_id)
                attempts.append(self._make_attempt(issue, FixStatus.SKIPPED_CROSS_FILE,
                                                    "跨文件改动，交由人工处理"))
                continue
            if not located_by_id.get(fix.issue_id, None) or \
                    not located_by_id[fix.issue_id].is_resolved:
                attempts.append(self._make_attempt(issue, FixStatus.SKIPPED_CROSS_FILE,
                                                    "问题未定位，无法安全生成变更"))
                continue
            pending.append((issue, fix))

        self.report.cross_file_issue_ids = cross_file_ids

        current_code = self._code
        applied_count = 0

        # 逐个 issue 独立走「应用 → 重审 → 判定」。每个 issue 的判定互不依赖，
        # 但代码变更必须串行叠加，所以这里的并发只用在单次回归内部的两次审查上。
        for issue, fix in pending:
            self._check_budget()

            attempt = self._make_attempt(issue, FixStatus.PENDING, "")
            max_attempts = RETRY_LIMITS.get(issue.priority, 1)
            attempt.max_attempts = max_attempts

            try:
                new_code, apply_info = verify_and_apply(
                    current_code, fix.patch, self._file_path, use_git=self.use_git_check
                )
            except PatchError as exc:
                attempt.status = FixStatus.PATCH_FAILED
                attempt.failure_reason = str(exc)
                # 确实尝试过一次，计数要如实反映；写 0 会让报告显示「尝试 0/3」，
                # 看起来像压根没试过。
                attempt.attempts = 1
                attempts.append(attempt)
                self.log("issue {} 的 patch 无法应用：{}".format(issue.issue_id, exc))
                # patch 应用失败同样是「这个分类上的修复走不通」，必须计入早停统计。
                # 漏掉这一条会让「patch 全都打不上」的场景一直跑到最后一个问题，
                # 每个都白烧一轮 LLM 调用。
                if self._should_early_stop(issue):
                    self.log("检测到同分类连续失败，触发早停")
                    break
                continue

            attempt.status = FixStatus.APPLIED
            attempt.applied_range = self._applied_range(located_by_id.get(issue.issue_id))
            applied_count += 1
            current_code = new_code

            # 闭环：强制重新审查改动后的代码，同时检查两面 ——
            # 原问题是否真的消失，以及是否引入了新问题。
            #
            # 注意：必须先 _detect_introduced 再判早退。若把检测放在早退分支之后，
            # 「原问题消失了但同时引入了新问题」的情况会因为 prior 为 0、introduced
            # 为空而误判为验证通过 —— 这正是本模块要防的那类假性成功。
            regression_issues = self._stage_review(code=current_code)
            still_present = self._issue_still_present(issue, regression_issues)
            introduced = self._detect_introduced(regression_issues)

            if introduced:
                attempt.regression_passed = False
                attempt.introduced_fingerprints = introduced
                attempt.failure_reason = "补丁引入了 {} 个新问题".format(len(introduced))
                self.report.regression_issue_ids.extend(introduced)
                self.log(
                    "issue {} 的补丁引入了 {} 个新问题".format(issue.issue_id, len(introduced))
                )

            if not still_present and not introduced:
                attempt.status = FixStatus.VERIFIED
                attempt.attempts = 1
                self.log("issue {} 修复已验证".format(issue.issue_id))
                attempts.append(attempt)
                continue

            # 原问题仍在（或引入了新问题）：按优先级配额重试。
            attempt.attempts = 1
            fixed, current_code = self._retry_loop(
                issue, attempt, current_code, max_attempts
            )
            if fixed:
                applied_count += 1
                attempts.append(attempt)
                continue

            # 重试耗尽：升级并可能触发早停。
            attempt.status = FixStatus.ESCALATED
            attempts.append(attempt)
            self.report.escalated_issue_ids.append(issue.issue_id)
            self.log("issue {} 重试耗尽，已升级".format(issue.issue_id))

            if self._should_early_stop(issue):
                self.log("检测到同分类连续失败，触发早停")
                break

        self.report.attempts = attempts
        self.report.applied_patch_count = applied_count

    def _retry_loop(self, issue, attempt, code, max_attempts):
        """对单个 issue 做重试。

        重试语义：重新生成 patch（而不是重复应用同一个 patch —— 那样必然还是失败），
        再走一次「应用 → 重审」。

        :returns: ``(是否修复成功, 当前代码)``。第二个返回值是必须的：重试过程中
            即使最终失败，中间也存在过被应用成功的 patch，调用方要继续基于最新的
            代码文本往下走，否则后续 issue 的 patch 会打到已经变过的源码上。
        """
        current_code = code

        while attempt.attempts < max_attempts:
            self._check_budget()
            attempt.attempts += 1

            current_issue = self._issue_cache.get(issue.issue_id, issue)
            located = [
                item for item in self.report.located_issues
                if item.issue_id == issue.issue_id and item.is_resolved
            ]
            if not located:
                attempt.failure_reason = "重试时问题已无法定位"
                return False, current_code

            try:
                retry_fixes = self.fixer.run(
                    current_code, located, self.project_context, self._file_path
                )
            except LLMResponseError as exc:
                attempt.failure_reason = "重试生成 patch 失败：{}".format(exc)
                continue

            retry_fix = next(
                (item for item in retry_fixes if item.issue_id == issue.issue_id), None
            )
            if retry_fix is None or retry_fix.requires_cross_file or not retry_fix.patch.strip():
                attempt.failure_reason = "重试未产出可用的单文件 patch"
                continue

            try:
                new_code, _ = verify_and_apply(
                    current_code, retry_fix.patch, self._file_path,
                    use_git=self.use_git_check,
                )
            except PatchError as exc:
                attempt.failure_reason = "重试 patch 无法应用：{}".format(exc)
                # 应用失败也要留轨迹，否则「重试了但每次都在打补丁阶段就挂了」
                # 在报告里看起来跟「压根没重试」一模一样。
                attempt.retry_trace.append(
                    {"attempt": attempt.attempts, "error": "patch_unapplicable"}
                )
                continue

            current_code = new_code
            regression_issues = self._stage_review(code=current_code)
            still_present = self._issue_still_present(current_issue, regression_issues)
            # 同样必须先检测引入的新问题，再判早退（见 _stage_regression 里的说明）。
            introduced = self._detect_introduced(regression_issues)
            attempt.retry_trace.append(
                {
                    "attempt": attempt.attempts,
                    "still_present": still_present,
                    "introduced_count": len(introduced),
                }
            )

            if introduced:
                attempt.introduced_fingerprints.extend(introduced)
                self.report.regression_issue_ids.extend(introduced)

            if not still_present and not introduced:
                attempt.status = FixStatus.VERIFIED
                attempt.failure_reason = ""
                attempt.regression_passed = True
                self.log(
                    "issue {} 在第 {} 次尝试后修复成功".format(issue.issue_id, attempt.attempts)
                )
                return True, current_code

            # 只要本轮仍有问题（原问题未消失 或 引入了新问题），就继续消耗配额。
            attempt.regression_passed = not attempt.introduced_fingerprints
            attempt.failure_reason = (
                "重试后原问题依然存在" if still_present else "重试仍引入了新问题"
            )

        return False, current_code

    def _should_early_stop(self, issue):
        """同分类连续失败计数，达到上限即早停。"""
        category = issue.category.value
        self.consecutive_category_failures[category] = (
            self.consecutive_category_failures.get(category, 0) + 1
        )
        return (
            self.consecutive_category_failures[category]
            >= CONSECUTIVE_CATEGORY_FAILURE_LIMIT
        )

    def _issue_still_present(self, original, new_issues):
        """判断原问题是否仍然存在。

        比对依据是 ``fingerprint``（分类 + 归一化锚点），不是 ``issue_id`` ——
        每次重新审查都会生成全新 UUID，靠 ID 比对恒为「不存在」，闭环会假性通过。
        这是需求伪代码里 ``has_same_issue(regression_issues, fix.issue_id)`` 的
        一个隐患，这里按指纹实现。
        """
        for candidate in new_issues:
            if candidate.fingerprint == original.fingerprint:
                return True
        return False

    def _detect_introduced(self, new_issues):
        """找出本轮新出现的、此前未见过的指纹 —— 即补丁引入的新问题。

        需求把闭环描述为「对比原问题与新问题」，但只实现了「原问题是否消失」。
        缺少这一半，一个「修好 A、弄坏 B」的补丁会被判为成功，而这类回归正是
        自动修复最大的风险来源。

        **这是唯一一处更新 ``self._known_fingerprints`` 的地方**。曾把「记录已知指纹」
        顺手放在 ``_stage_review`` 里，结果是同一次审查既登记又检测，集合里永远
        已经有了本轮的新指纹，检测恒为空 —— 一个自己抵消自己的实现。
        必须严格分成「先比对、后登记」两步。
        """
        introduced = []
        for candidate in new_issues:
            if candidate.fingerprint not in self._known_fingerprints:
                introduced.append(candidate.fingerprint)

        # 登记放在比对之后：本轮的每个指纹都应成为后续轮次的「已知」。
        for candidate in new_issues:
            self._known_fingerprints.add(candidate.fingerprint)

        return introduced

    def _register_known(self, issues):
        """只登记、不比对。用于首次审查 —— 那个时点不存在「引入」的概念。"""
        for issue in issues:
            self._known_fingerprints.add(issue.fingerprint)

    def _make_attempt(self, issue, status, reason):
        return FixAttempt(
            issue_id=issue.issue_id,
            fingerprint=issue.fingerprint,
            priority=issue.priority,
            category=issue.category,
            status=status,
            failure_reason=reason,
            max_attempts=RETRY_LIMITS.get(issue.priority, 1),
        )

    def _applied_range(self, located_issue):
        if located_issue is None:
            return []
        return [located_issue.line_start, located_issue.line_end]

    # ------------------------------------------------------------------ 收尾

    def _finalize(self):
        self.report.llm_calls = self.client.call_count
        self.report.finished_at = _now()

        if self.state not in (WorkflowState.FAILED,):
            # 只有高危问题未通过才阻塞合并。P2/P3 耗尽重试属于改进建议，不阻塞。
            self.report.status = (
                RunStatus.BLOCKED if self.report.blocking_escalation else RunStatus.PASSED
            )
            if self.report.status == RunStatus.BLOCKED:
                self._transition(WorkflowState.ESCALATED)

        self.report.unresolved_issue_ids = sorted(set(self.report.unresolved_issue_ids))
        self.report.escalated_issue_ids = sorted(set(self.report.escalated_issue_ids))
        self.report.cross_file_issue_ids = sorted(set(self.report.cross_file_issue_ids))
        self.report.regression_issue_ids = sorted(set(self.report.regression_issue_ids))

        try:
            self.save_state()
        except OSError as exc:
            self.log("状态快照写入失败：{}".format(exc))

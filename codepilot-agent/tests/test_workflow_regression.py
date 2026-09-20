"""回归闭环测试：重试配额、早停、以及「补丁引入新问题」的检测。

这里不用 fixture 文件，而是用一个状态化的假客户端直接控制每次审查返回什么。
原因是回归闭环的正确性取决于「第 N 次审查看到什么」，用声明式 fixture 表达
这种时序非常别扭，用代码写清楚得多。
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.patch import verify_and_apply  # noqa: E402
from core.schemas import (  # noqa: E402
    Category,
    FixStatus,
    Priority,
    RunStatus,
    Severity,
)
from core.workflow import CONSECUTIVE_CATEGORY_FAILURE_LIMIT, WorkflowRunner  # noqa: E402
from llm.client import BaseLLMClient  # noqa: E402


# 一段带两个独立可修缺陷的源码，用于构造真实可应用的 patch。
SOURCE = """def handler(user, items):
    total = 0
    for i in range(len(items)):
        for j in range(len(items)):
            total += items[i] * items[j]
    name = user["name"]
    return total, name
"""


def make_issue(issue_id, anchor, category="Performance", priority="P2",
               severity="Major", description="测试问题"):
    return {
        "issue_id": issue_id,
        "category": category,
        "priority": priority,
        "severity": severity,
        "code_anchor": anchor,
        "description": description,
        "suggestion": "修掉它",
    }


NESTED_LOOP_ANCHOR = (
    "    for i in range(len(items)):\n"
    "        for j in range(len(items)):\n"
    "            total += items[i] * items[j]"
)
NONE_DEREF_ANCHOR = '    name = user["name"]'

# 一个能消除 NESTED_LOOP_ANCHOR 的补丁。
LOOP_FIX_PATCH = """--- a/target.py
+++ b/target.py
@@ -1,7 +1,5 @@
 def handler(user, items):
-    total = 0
-    for i in range(len(items)):
-        for j in range(len(items)):
-            total += items[i] * items[j]
+    total = sum(value * sum(items) for value in items)
     name = user["name"]
     return total, name
"""

# 重试用的补丁。注意它作用的对象是**已经被 LOOP_FIX_PATCH 改过的**代码
# （现实里也是一样：模型看到的 always 是当前版本的代码），所以这里的上下文
# 是 `total = sum(value * sum(items) for value in items)` 而非原始的两行嵌套循环。
LOOP_FIX_PATCH_ALT = """--- a/target.py
+++ b/target.py
@@ -1,5 +1,5 @@
 def handler(user, items):
-    total = sum(value * sum(items) for value in items)
+    total = sum(items) ** 2
     name = user["name"]
     return total, name
"""

# 一个能消除 NONE_DEREF_ANCHOR 的补丁。
NONE_FIX_PATCH = """--- a/target.py
+++ b/target.py
@@ -1,7 +1,9 @@
 def handler(user, items):
     total = 0
     for i in range(len(items)):
         for j in range(len(items)):
             total += items[i] * items[j]
+    if user is None:
+        return total, ""
     name = user["name"]
     return total, name
"""


class ScriptedClient(BaseLLMClient):
    """按脚本返回审查/修复结果的状态化假客户端。

    :param review_script: 每次审查调用的返回值，按次序取；耗尽后重复最后一项。
    :param fix_script: 每次修复调用的返回值，同上。
    """

    def __init__(self, review_script, fix_script=None, locator_script=None):
        super().__init__()
        self.review_script = list(review_script)
        self.fix_script = list(fix_script or [])
        self.locator_script = list(locator_script or [])
        self.counters = {"reviewer": 0, "fixer": 0, "locator": 0}
        self.review_codes = []

    def complete_json(self, *, system, user, tool_name, tool_description, schema_model, step=""):
        key = {"submit_review": "reviewer", "submit_fixes": "fixer",
               "submit_locations": "locator"}[tool_name]
        index = self.counters[key]
        self.counters[key] = index + 1

        if key == "reviewer":
            self.review_codes.append(user)
            script = self.review_script
        elif key == "fixer":
            script = self.fix_script
        else:
            script = self.locator_script

        if not script:
            payload = {"issues": [], "fixes": [], "located_issues": []}[tool_name]
        else:
            payload = script[index] if index < len(script) else script[-1]

        self._record(key, payload)
        return payload


def run(source, client, **kwargs):
    runner = WorkflowRunner(client=client, workdir=os.path.join(os.getcwd(), ".pytest-tmp"),
                            use_git_check=False, **kwargs)
    return runner.run(source, file_path="target.py")


class TestRetrySuccess:
    def test_fix_verified_when_issue_disappears(self):
        """补丁真的消除了问题 → 一次通过，标记 VERIFIED。"""
        client = ScriptedClient(
            review_script=[
                {"issues": [make_issue("i1", NESTED_LOOP_ANCHOR)]},
                {"issues": []},  # 回归审查：问题已消失
            ],
            fix_script=[{"fixes": [{"issue_id": "i1", "requires_cross_file": False,
                                    "patch": LOOP_FIX_PATCH, "explanation": "降复杂度"}]}],
        )
        report = run(SOURCE, client)

        assert report.status == RunStatus.PASSED
        assert report.applied_patch_count == 1
        verified = [a for a in report.attempts if a.status == FixStatus.VERIFIED]
        assert len(verified) == 1
        assert verified[0].attempts == 1

    def test_retry_then_success_stops_early(self):
        """P1 配额是 3 次：第 2 次修复成功后应当停止，不再消耗配额。"""
        client = ScriptedClient(
            review_script=[
                {"issues": [make_issue("i1", NESTED_LOOP_ANCHOR, priority="P1",
                                       severity="Critical")]},
                {"issues": [make_issue("i1", NESTED_LOOP_ANCHOR, priority="P1",
                                       severity="Critical")]},  # 第一次修复无效
                {"issues": []},  # 重试后成功
            ],
            fix_script=[
                # 第一次和重试用**不同**的补丁：重试复用同一个 patch 只会重复失败。
                {"fixes": [{"issue_id": "i1", "requires_cross_file": False,
                            "patch": LOOP_FIX_PATCH, "explanation": "第一次"}]},
                {"fixes": [{"issue_id": "i1", "requires_cross_file": False,
                            "patch": LOOP_FIX_PATCH_ALT, "explanation": "重试"}]},
            ],
        )
        report = run(SOURCE, client)

        verified = [a for a in report.attempts if a.status == FixStatus.VERIFIED]
        assert len(verified) == 1
        assert verified[0].attempts == 2
        # 审查调用：初次 + 首次回归 + 重试后回归 = 3 次，证明成功即停、没有多跑
        assert client.counters["reviewer"] == 3


class TestRetryExhaustion:
    def test_p2_exhausts_after_two_attempts(self):
        """P2 配额 2 次：两次都失败 → 升级，且尝试次数与配额吻合。"""
        issue = make_issue("i1", NESTED_LOOP_ANCHOR, priority="P2", severity="Major")
        client = ScriptedClient(
            review_script=[{"issues": [issue]}],  # 恒返回该问题，永远修不好
            fix_script=[{"fixes": [{"issue_id": "i1", "requires_cross_file": False,
                                    "patch": LOOP_FIX_PATCH, "explanation": "x"}]}],
        )
        report = run(SOURCE, client)

        escalated = [a for a in report.attempts if a.status == FixStatus.ESCALATED]
        assert len(escalated) == 1
        assert escalated[0].attempts == escalated[0].max_attempts == 2
        assert "i1" in report.escalated_issue_ids

    def test_p3_gets_only_one_attempt(self):
        """P3 配额 1 次：不存在重试。"""
        issue = make_issue("i1", NESTED_LOOP_ANCHOR, priority="P3",
                           category="Style", severity="Minor")
        client = ScriptedClient(
            review_script=[{"issues": [issue]}],
            fix_script=[{"fixes": [{"issue_id": "i1", "requires_cross_file": False,
                                    "patch": LOOP_FIX_PATCH, "explanation": "x"}]}],
        )
        report = run(SOURCE, client)

        escalated = [a for a in report.attempts if a.status == FixStatus.ESCALATED]
        assert len(escalated) == 1
        assert escalated[0].max_attempts == 1
        assert escalated[0].attempts == 1

    def test_p0_gets_three_attempts(self):
        """P0 配额 3 次。"""
        issue = make_issue("i1", NESTED_LOOP_ANCHOR, priority="P0",
                           category="Security", severity="Blocker")
        client = ScriptedClient(
            review_script=[{"issues": [issue]}],
            fix_script=[{"fixes": [{"issue_id": "i1", "requires_cross_file": False,
                                    "patch": LOOP_FIX_PATCH, "explanation": "x"}]}],
        )
        report = run(SOURCE, client)
        escalated = [a for a in report.attempts if a.status == FixStatus.ESCALATED]
        assert escalated[0].max_attempts == 3
        assert escalated[0].attempts == 3

    def test_retry_trace_records_each_attempt(self):
        """每次重试都要留下轨迹 —— 可观测性是需求里的非功能要求。"""
        issue = make_issue("i1", NESTED_LOOP_ANCHOR, priority="P1", severity="Critical")
        client = ScriptedClient(
            review_script=[
                {"issues": [issue]},
                {"issues": [issue]},   # 第一次修复无效，进入重试
                {"issues": []},        # 重试后成功
            ],
            fix_script=[
                {"fixes": [{"issue_id": "i1", "requires_cross_file": False,
                            "patch": LOOP_FIX_PATCH, "explanation": "a"}]},
                {"fixes": [{"issue_id": "i1", "requires_cross_file": False,
                            "patch": LOOP_FIX_PATCH_ALT, "explanation": "b"}]},
            ],
        )
        report = run(SOURCE, client)
        attempt = report.attempts[0]
        assert attempt.status == FixStatus.VERIFIED
        assert attempt.retry_trace
        assert all("attempt" in entry for entry in attempt.retry_trace)

    def test_retry_trace_records_patch_failures_too(self):
        """补丁打不上也要留轨迹，否则报告里与「没重试」无法区分。"""
        issue = make_issue("i1", NESTED_LOOP_ANCHOR, priority="P1", severity="Critical")
        client = ScriptedClient(
            review_script=[
                {"issues": [issue]},
                {"issues": [issue]},
            ],
            fix_script=[
                {"fixes": [{"issue_id": "i1", "requires_cross_file": False,
                            "patch": LOOP_FIX_PATCH, "explanation": "a"}]},
                {"fixes": [{"issue_id": "i1", "requires_cross_file": False,
                            "patch": "--- a/target.py\n+++ b/target.py\n@@ -1,3 +1,3 @@\n"
                                     " 对不上的上下文\n-旧\n+新\n",
                            "explanation": "b"}]},
            ],
        )
        report = run(SOURCE, client)
        attempt = report.attempts[0]
        assert any(entry.get("error") == "patch_unapplicable"
                   for entry in attempt.retry_trace)


class TestEarlyStop:
    # 三个补丁都注入一段**语法不合法**的 change，因此必然应用失败。
    # 这不只是为了构造失败 —— 它同时验证了「patch 应用失败也计入早停统计」这条修正，
    # 漏掉那条会让 patch 全打不上的场景一路跑完所有问题，每个白烧一轮 LLM 调用。
    def _broken_patch(self, marker):
        return (
            "--- a/target.py\n"
            "+++ b/target.py\n"
            "@@ -3,3 +3,3 @@\n"
            " 完全对不上的上下文\n"
            "-{old}\n"
            "+{new}\n"
        ).format(old=marker, new=marker + "_changed")

    def test_early_stop_needs_two_consecutive_same_category_failures(self):
        """单个问题失败不足以早停；同分类第二个失败才触发。"""
        issues = [
            make_issue("s1", NESTED_LOOP_ANCHOR, priority="P2", category="Style"),
            make_issue("s2", NONE_DEREF_ANCHOR, priority="P2", category="Style"),
            make_issue("s3", "    return total, name", priority="P2", category="Style"),
        ]
        client = ScriptedClient(
            review_script=[{"issues": issues}],
            fix_script=[{"fixes": [
                {"issue_id": "s1", "requires_cross_file": False,
                 "patch": self._broken_patch("a"), "explanation": "x"},
                {"issue_id": "s2", "requires_cross_file": False,
                 "patch": self._broken_patch("b"), "explanation": "x"},
                {"issue_id": "s3", "requires_cross_file": False,
                 "patch": self._broken_patch("c"), "explanation": "x"},
            ]}],
        )
        report = run(SOURCE, client)

        # 阈值 2：处理完 s1、s2 后早停，s3 不再被处理。
        assert len(report.attempts) == CONSECUTIVE_CATEGORY_FAILURE_LIMIT
        assert len(report.escalated_issue_ids) < len(issues)

    def test_different_categories_do_not_trigger_early_stop(self):
        """不同分类的失败各自计数，不会凑成连续同类失败。"""
        issues = [
            make_issue("a1", NESTED_LOOP_ANCHOR, priority="P2", category="Security"),
            make_issue("a2", NONE_DEREF_ANCHOR, priority="P2", category="Logic"),
            make_issue("a3", "    return total, name", priority="P2", category="Style"),
        ]
        client = ScriptedClient(
            review_script=[{"issues": issues}],
            fix_script=[{"fixes": [
                {"issue_id": "a1", "requires_cross_file": False,
                 "patch": self._broken_patch("a"), "explanation": "x"},
                {"issue_id": "a2", "requires_cross_file": False,
                 "patch": self._broken_patch("b"), "explanation": "x"},
                {"issue_id": "a3", "requires_cross_file": False,
                 "patch": self._broken_patch("c"), "explanation": "x"},
            ]}],
        )
        report = run(SOURCE, client)
        # 三个问题分属三个分类，都不构成「连续同类」，所以全部被处理。
        assert len(report.attempts) == len(issues)


class TestIntroducedRegression:
    def test_patch_that_introduces_a_new_issue_is_not_verified(self):
        """补丁消除了原问题，但引入了新问题 → 不能判为成功。"""
        original = make_issue("i1", NESTED_LOOP_ANCHOR, priority="P3",
                              category="Style", severity="Minor")
        introduced = make_issue("new", NONE_DEREF_ANCHOR, category="Logic",
                                priority="P1", severity="Critical",
                                description="新引入的空指针")

        client = ScriptedClient(
            # 第一次审查看到原问题；之后每次都只看到「新问题」，原问题确实消失了。
            review_script=[{"issues": [original]}, {"issues": [introduced]}],
            fix_script=[{"fixes": [{"issue_id": "i1", "requires_cross_file": False,
                                    "patch": LOOP_FIX_PATCH, "explanation": "x"}]}],
        )
        report = run(SOURCE, client)

        attempt = [a for a in report.attempts if a.issue_id == "i1"][0]
        assert attempt.status == FixStatus.ESCALATED
        assert attempt.regression_passed is False
        assert attempt.introduced_fingerprints
        assert report.regression_issue_ids
        # 引入的新问题指纹必须与「原问题未解决」区分开
        assert attempt.introduced_fingerprints[0] in report.regression_issue_ids


class TestPatchFailureHandling:
    def test_unapplicable_patch_is_recorded_not_fatal(self):
        """补丁应用不了应当记为 PATCH_FAILED，而不是让整个流程崩溃。"""
        client = ScriptedClient(
            review_script=[{"issues": [make_issue("i1", NESTED_LOOP_ANCHOR)]}],
            fix_script=[{"fixes": [{"issue_id": "i1", "requires_cross_file": False,
                                    "patch": "--- a/target.py\n+++ b/target.py\n"
                                             "@@ -99,3 +99,3 @@\n"
                                             " 完全不存在的上下文\n"
                                             "-也不存在\n"
                                             "+改了什么\n"}]}],
        )
        report = run(SOURCE, client)

        failed = [a for a in report.attempts if a.status == FixStatus.PATCH_FAILED]
        assert len(failed) == 1
        assert report.applied_patch_count == 0

    def test_cross_file_fix_is_skipped_not_attempted(self):
        """跨文件改动必须跳过并标记人工处理，不能触发回归。"""
        client = ScriptedClient(
            review_script=[{"issues": [make_issue("i1", NESTED_LOOP_ANCHOR)]}],
            fix_script=[{"fixes": [{"issue_id": "i1", "requires_cross_file": True,
                                    "patch": "", "explanation": "需要改调用方"}]}],
        )
        report = run(SOURCE, client)

        skipped = [a for a in report.attempts
                   if a.status == FixStatus.SKIPPED_CROSS_FILE]
        assert len(skipped) == 1
        assert "i1" in report.cross_file_issue_ids
        # 跨文件跳过不应消耗审查调用（只有初次审查一次）
        assert client.counters["reviewer"] == 1

    def test_p0_unapplicable_patch_blocks_merge(self):
        """高危问题的补丁打不上，同样是「高危未通过」，必须阻塞合并。

        曾经这里只把 PATCH_FAILED 记进 attempts：它既不在 escalated_issue_ids，
        也不在 unresolved_issue_ids 里，于是 status 被算成 PASSED、退出码 0、
        blocking.txt 写成 false —— 一个已定位但没修好的 P0 直接通过 CI 门禁，
        而报告正文的「未通过项」还写着「该问题为高危，阻塞合并」。
        """
        client = ScriptedClient(
            review_script=[{"issues": [make_issue("i1", NESTED_LOOP_ANCHOR,
                                                  priority="P0", category="Security",
                                                  severity="Blocker")]}],
            fix_script=[{"fixes": [{"issue_id": "i1", "requires_cross_file": False,
                                    "patch": "--- a/target.py\n+++ b/target.py\n"
                                             "@@ -99,3 +99,3 @@\n"
                                             " 完全不存在的上下文\n"
                                             "-也不存在\n"
                                             "+改了什么\n"}]}],
        )
        report = run(SOURCE, client)

        assert [a.status for a in report.attempts] == [FixStatus.PATCH_FAILED]
        assert report.blocking_escalation is True
        assert report.status == RunStatus.BLOCKED

    def test_p2_unapplicable_patch_still_does_not_block(self):
        """反向守卫：不能为了堵住假通过就把低危问题也变成阻塞。"""
        client = ScriptedClient(
            review_script=[{"issues": [make_issue("i1", NESTED_LOOP_ANCHOR,
                                                  priority="P2", category="Performance")]}],
            fix_script=[{"fixes": [{"issue_id": "i1", "requires_cross_file": False,
                                    "patch": "--- a/target.py\n+++ b/target.py\n"
                                             "@@ -99,3 +99,3 @@\n"
                                             " 完全不存在的上下文\n"
                                             "-也不存在\n"
                                             "+改了什么\n"}]}],
        )
        report = run(SOURCE, client)

        assert [a.status for a in report.attempts] == [FixStatus.PATCH_FAILED]
        assert report.blocking_escalation is False
        assert report.status == RunStatus.PASSED

    def test_failed_patch_attempt_reports_one_attempt(self):
        """补丁打不上也真的试过一次：计数写 0 会让报告显示「尝试 0/3」。"""
        client = ScriptedClient(
            review_script=[{"issues": [make_issue("i1", NESTED_LOOP_ANCHOR,
                                                  priority="P1", category="Security",
                                                  severity="Critical")]}],
            fix_script=[{"fixes": [{"issue_id": "i1", "requires_cross_file": False,
                                    "patch": "--- a/target.py\n+++ b/target.py\n"
                                             "@@ -99,3 +99,3 @@\n"
                                             " 完全不存在的上下文\n"
                                             "-也不存在\n"
                                             "+改了什么\n"}]}],
        )
        report = run(SOURCE, client)

        assert report.attempts[0].attempts == 1


class TestReportStatusSemantics:
    def test_only_blocking_issues_block_merge(self):
        """P2/P3 耗尽重试不阻塞合并 —— 它们只是改进建议。"""
        client = ScriptedClient(
            review_script=[{"issues": [make_issue("i1", NESTED_LOOP_ANCHOR,
                                                  priority="P3", category="Style")]}],
            fix_script=[{"fixes": [{"issue_id": "i1", "requires_cross_file": False,
                                    "patch": LOOP_FIX_PATCH, "explanation": "x"}]}],
        )
        report = run(SOURCE, client)

        assert report.escalated_issue_ids  # 确实升级了
        assert report.blocking_escalation is False
        assert report.status == RunStatus.PASSED

    def test_blocking_issue_escalation_blocks_merge(self):
        client = ScriptedClient(
            review_script=[{"issues": [make_issue("i1", NESTED_LOOP_ANCHOR,
                                                  priority="P0", category="Security",
                                                  severity="Blocker")]}],
            fix_script=[{"fixes": [{"issue_id": "i1", "requires_cross_file": False,
                                    "patch": LOOP_FIX_PATCH, "explanation": "x"}]}],
        )
        report = run(SOURCE, client)

        assert report.blocking_escalation is True
        assert report.status == RunStatus.BLOCKED

    def test_no_issues_passes_without_entering_the_loop(self):
        client = ScriptedClient(review_script=[{"issues": []}])
        report = run(SOURCE, client)

        assert report.status == RunStatus.PASSED
        assert report.attempts == []
        assert client.counters["fixer"] == 0


class TestLocatorFallback:
    def test_unresolved_anchor_stays_unresolved_and_gets_no_patch(self):
        """锚点在源码里找不到 → unresolved，且不应生成补丁。"""
        client = ScriptedClient(
            review_script=[{"issues": [make_issue("i1", "这段代码根本不在文件里")]}],
            locator_script=[{"located_issues": [
                {"issue_id": "i1", "file_path": "target.py", "line_start": None,
                 "line_end": None, "match_status": "unresolved", "confidence": 0.0}
            ]}],
        )
        report = run(SOURCE, client)

        assert "i1" in report.unresolved_issue_ids
        assert client.counters["fixer"] == 0

    def test_out_of_range_fallback_line_is_rejected(self):
        """兜底定位给出越界行号时必须降级为 unresolved，防幻觉行号进入修复层。"""
        client = ScriptedClient(
            review_script=[{"issues": [make_issue("i1", "这段代码根本不在文件里")]}],
            locator_script=[{"located_issues": [
                {"issue_id": "i1", "file_path": "target.py", "line_start": 9999,
                 "line_end": 10001, "match_status": "fuzzy", "confidence": 0.9}
            ]}],
        )
        report = run(SOURCE, client)

        assert "i1" in report.unresolved_issue_ids
        located = [item for item in report.located_issues if item.issue_id == "i1"][0]
        assert located.line_start is None
        assert located.match_status.value == "unresolved"


class TestVerifyApplyBuildsRealPatches:
    """补丁链路本身的回归网：确保上面用的夹具补丁确实可应用。"""

    def test_loop_fix_patch_actually_applies(self):
        new_code, info = verify_and_apply(SOURCE, LOOP_FIX_PATCH, "target.py",
                                          use_git=False)
        assert info["hunks_applied"] == 1
        assert "for i in range(len(items))" not in new_code

    def test_none_fix_patch_actually_applies(self):
        new_code, info = verify_and_apply(SOURCE, NONE_FIX_PATCH, "target.py",
                                          use_git=False)
        assert info["hunks_applied"] == 1
        assert "if user is None:" in new_code

"""多 chunk 合并的回归测试。

守的是一个曾经存在的假通过：``cli.review_one`` 在多 chunk 时只把各 chunk 的
``issues`` 拼起来，``status`` / 未定位 / 重试耗尽全取**最后一个 chunk** 的。
于是一个「第 1 块有未修复的高危问题、第 2 块干净」的大文件会被判为通过、
退出码 0、``blocking.txt`` 写成 false —— 高危问题失去阻塞能力。

这里用两种手段覆盖：直接对 ``merge_chunk_reports`` 做单元测试（快、边界清楚），
以及用脚本化客户端跑一次真实的两 chunk 文件（证明整条链路不再漏判）。
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import cli  # noqa: E402
from cli import merge_chunk_reports  # noqa: E402
from core.chunker import chunk_source  # noqa: E402
from core.errors import ConfigError  # noqa: E402
from core.schemas import (  # noqa: E402
    FixAttempt,
    FixStatus,
    Issue,
    LocatedIssue,
    ReviewReport,
    RunStatus,
    WorkflowState,
)
from llm.client import BaseLLMClient  # noqa: E402


def make_issue(issue_id="i1", priority="P0", category="Security",
               severity="Blocker", anchor='password = "MARKER_ONE"'):
    return Issue(
        issue_id=issue_id, category=category, priority=priority, severity=severity,
        code_anchor=anchor, description="d", suggestion="s",
    )


def blocked_report(issue_id="i1"):
    """一个 chunk 的报告：有条高危问题没解决 → 该 chunk 判 BLOCKED。"""
    report = ReviewReport(target_file="big.py")
    report.issues = [make_issue(issue_id)]
    report.located_issues = [LocatedIssue(issue_id=issue_id, file_path="big.py",
                                          match_status="unresolved")]
    report.unresolved_issue_ids = [issue_id]
    report.attempts = [FixAttempt(issue_id=issue_id, status=FixStatus.PATCH_FAILED)]
    report.status = RunStatus.BLOCKED
    report.workflow_state = WorkflowState.ESCALATED
    report.applied_patch_count = 1
    report.llm_calls = 3
    return report


def clean_report():
    report = ReviewReport(target_file="big.py")
    report.status = RunStatus.PASSED
    report.workflow_state = WorkflowState.DONE
    report.llm_calls = 5
    return report


class TestMergeChunkReports:
    def test_blocked_chunk_wins_over_clean_last_chunk(self):
        """这是核心断言：干净的最后一块不能抹掉前面那块的高危结论。"""
        merged = merge_chunk_reports([blocked_report(), clean_report()])
        assert merged.status == RunStatus.BLOCKED

    def test_order_does_not_matter(self):
        merged = merge_chunk_reports([clean_report(), blocked_report()])
        assert merged.status == RunStatus.BLOCKED

    def test_unresolved_and_attempts_are_merged_not_replaced(self):
        merged = merge_chunk_reports([blocked_report(), clean_report()])
        assert merged.unresolved_issue_ids == ["i1"]
        assert [a.status for a in merged.attempts] == [FixStatus.PATCH_FAILED]
        assert len(merged.located_issues) == 1

    def test_issues_are_not_duplicated(self):
        """合并只插一次：曾经的写法先取全集又在循环里再插一遍。"""
        merged = merge_chunk_reports([blocked_report(), blocked_report("i2")])
        assert [issue.issue_id for issue in merged.issues] == ["i1", "i2"]

    def test_counters_are_summed_and_maxed(self):
        first = blocked_report()
        first.applied_patch_count = 2
        first.llm_calls = 4
        second = clean_report()
        second.applied_patch_count = 3
        second.llm_calls = 9

        merged = merge_chunk_reports([first, second])
        assert merged.applied_patch_count == 5
        # 客户端是整个文件复用的，最后一次的计数才是总数
        assert merged.llm_calls == 9

    def test_clean_file_stays_passed(self):
        merged = merge_chunk_reports([clean_report(), clean_report()])
        assert merged.status == RunStatus.PASSED

    def test_empty_input_is_an_error(self):
        with pytest.raises(ConfigError):
            merge_chunk_reports([])


# --------------------------------------------------------------------------- 集成
# 一份能切出至少两个 chunk 的源码：第 1 块带标记与高危问题，第 2 块干净。
_FILLER = "\n".join("    # filler line %d to grow past the chunk budget" % i
                    for i in range(12))

TWO_CHUNK_SOURCE = (
    'import os\n\n\ndef first():\n    password = "MARKER_ONE"\n'
    + _FILLER + "\n    return password\n\n\ndef second():\n"
    + _FILLER + "\n    return 2\n"
)


class _ChunkAwareClient(BaseLLMClient):
    """只对含标记的那个 chunk 报高危问题，其余 chunk 一律干净。

    问题的锚点在代码里并不存在，所以它会停在 unresolved —— 这正是原来会被
    丢弃的那类结论。
    """

    def complete_json(self, *, system, user, tool_name, tool_description,
                      schema_model, step=""):
        self._record(step or tool_name, None)

        if tool_name == "submit_review":
            if "MARKER_ONE" not in user:
                return {"issues": [], "notes": "clean"}
            return {"issues": [{
                "issue_id": "p0-1",
                "category": "Security",
                "priority": "P0",
                "severity": "Blocker",
                "code_anchor": 'db_password = "anchor-that-is-nowhere"',
                "description": "硬编码凭据",
                "suggestion": "读环境变量",
            }], "notes": ""}

        if tool_name == "submit_locations":
            return {"located_issues": []}
        if tool_name == "submit_fixes":
            return {"fixes": []}
        raise AssertionError("unexpected tool " + tool_name)


class TestReviewOneAcrossChunks:
    def test_high_risk_issue_in_an_earlier_chunk_still_blocks(self, tmp_path):
        # 前提：这份源码确实被切成了多块，否则这个测试没有意义。
        assert len(chunk_source(TWO_CHUNK_SOURCE, char_budget=400)) == 2

        target = tmp_path / "bigmodule.py"
        target.write_text(TWO_CHUNK_SOURCE, encoding="utf-8")

        args = cli.build_parser().parse_args(
            ["review", str(target), "--char-budget", "400"]
        )

        report, markdown = cli.review_one(
            args, str(target), lambda _args: _ChunkAwareClient()
        )

        assert "p0-1" in [issue.issue_id for issue in report.issues]
        assert report.status == RunStatus.BLOCKED
        assert report.unresolved_issue_ids == ["p0-1"]
        assert "⛔ **阻塞合并**" in markdown

    def test_single_chunk_clean_file_passes(self, tmp_path):
        """未切块时走原路径，报告对象应当是 runner 直接给的那一份。"""
        target = tmp_path / "small.py"
        target.write_text("def f():\n    return 1\n", encoding="utf-8")
        assert len(chunk_source("def f():\n    return 1\n", char_budget=24000)) == 1

        args = cli.build_parser().parse_args(
            ["review", str(target), "--char-budget", "24000"]
        )
        report, _markdown = cli.review_one(args, str(target),
                                           lambda _args: _ChunkAwareClient())

        assert report.status == RunStatus.PASSED

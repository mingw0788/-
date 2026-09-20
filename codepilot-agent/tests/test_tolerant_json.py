"""容错 JSON 解析与 Pydantic 契约的测试。

重点在**边界**：能救回的噪声要有明确范围，救不回的必须显式报错而不是猜。
"""
from __future__ import annotations

import json
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.errors import SchemaValidationError  # noqa: E402
from core.schemas import (  # noqa: E402
    FixAttempt,
    FixStatus,
    Issue,
    IssueList,
    LocatedIssue,
    MatchStatus,
    Priority,
    ReviewReport,
    RunStatus,
)
from llm.tolerant_json import (  # noqa: E402
    coerce_issues_payload,
    extract_balanced_json,
    parse_json_response,
    repair_json_literals,
    strip_code_fence,
)


class TestStripCodeFence:
    def test_plain_fence_is_removed(self):
        assert strip_code_fence('```json\n{"a": 1}\n```') == '{"a": 1}'

    def test_bare_fence_is_removed(self):
        assert strip_code_fence('```\n{"a": 1}\n```') == '{"a": 1}'

    def test_text_without_fence_is_untouched(self):
        assert strip_code_fence('{"a": 1}') == '{"a": 1}'

    def test_only_leading_fence(self):
        assert strip_code_fence('```json\n{"a": 1}') == '{"a": 1}'

    def test_empty_input(self):
        assert strip_code_fence("") == ""


class TestExtractBalancedJson:
    def test_brace_inside_string_does_not_end_the_block(self):
        """`{"a": "}"}` 里的右花括号在字符串内，不能当结束符。"""
        text = '前言 {"a": "}"} 后语'
        assert extract_balanced_json(text) == '{"a": "}"}'

    def test_escaped_quote_inside_string(self):
        text = '{"a": "he said \\" hi"} tail'
        assert extract_balanced_json(text) == '{"a": "he said \\" hi"}'

    def test_nested_objects(self):
        text = 'noise {"a": {"b": {"c": 1}}} noise'
        assert extract_balanced_json(text) == '{"a": {"b": {"c": 1}}}'

    def test_returns_none_when_no_brace(self):
        assert extract_balanced_json("no json here") is None

    def test_returns_none_when_unbalanced(self):
        assert extract_balanced_json('{"a": 1') is None


class TestRepairLiterals:
    def test_trailing_comma_removed(self):
        assert repair_json_literals('{"a": 1,}') == '{"a": 1}'

    def test_trailing_comma_before_bracket(self):
        assert repair_json_literals('[1, 2,]') == '[1, 2]'

    def test_python_literals_normalized(self):
        repaired = repair_json_literals('{"a": True, "b": False, "c": None}')
        # 结果必须是不带引号的 JSON 字面量；带上引号就变成了字符串，语义全错。
        assert repaired == '{"a": true, "b": false, "c": null}'

    def test_literal_substring_not_touched(self):
        """`Nonexistent` 里的 None 不能被当成字面量替换。"""
        assert repair_json_literals('{"a": "Nonexistent"}') == '{"a": "Nonexistent"}'


class TestParseJsonResponse:
    def test_clean_json(self):
        assert parse_json_response('{"a": 1}') == {"a": 1}

    def test_fenced_json(self):
        assert parse_json_response('```json\n{"a": 1}\n```') == {"a": 1}

    def test_json_with_preamble_and_epilogue(self):
        """模型爱写「好的，以下是结果：」，必须能救回。"""
        text = '好的，以下是审查结果：\n{"issues": []}\n希望对你有帮助！'
        assert parse_json_response(text) == {"issues": []}

    def test_trailing_comma_repaired(self):
        assert parse_json_response('{"a": 1,}') == {"a": 1}

    def test_python_booleans_repaired(self):
        assert parse_json_response('{"a": True}') == {"a": True}

    def test_single_quotes_repaired(self):
        assert parse_json_response("{'a': 'b'}") == {"a": "b"}

    def test_empty_response_raises(self):
        with pytest.raises(SchemaValidationError):
            parse_json_response("")

    def test_whitespace_only_raises(self):
        with pytest.raises(SchemaValidationError):
            parse_json_response("   \n  ")

    def test_garbage_raises_with_raw_text_attached(self):
        """失败时必须带上原文，否则无法诊断也无法沉淀成 fixture。"""
        with pytest.raises(SchemaValidationError) as excinfo:
            parse_json_response("这完全不是 JSON，只是散文。")
        assert excinfo.value.raw_text

    def test_toplevel_array_is_rejected_by_parser(self):
        """顶层数组不是 dict，解析层拒绝 —— 规整交给 coerce 层，职责分开。"""
        with pytest.raises(SchemaValidationError):
            parse_json_response("[1, 2, 3]")


class TestCoerceIssuesPayload:
    def test_array_is_wrapped(self):
        payload = coerce_issues_payload([{"a": 1}])
        assert payload == {"issues": [{"a": 1}]}

    def test_single_issue_is_wrapped(self):
        payload = coerce_issues_payload({"category": "Logic", "code_anchor": "x"})
        assert payload["issues"] == [{"category": "Logic", "code_anchor": "x"}]

    def test_normal_payload_passes_through(self):
        payload = {"issues": [], "notes": "ok"}
        assert coerce_issues_payload(payload) == payload


class TestSchemaContracts:
    def _issue(self, **overrides):
        base = {
            "issue_id": "i1",
            "category": "Security",
            "priority": "P0",
            "severity": "Blocker",
            "code_anchor": "dangerous()",
            "description": "d",
        }
        base.update(overrides)
        return base

    def test_valid_issue_parses(self):
        issue = Issue(**self._issue())
        assert issue.fingerprint

    def test_unknown_category_is_rejected(self):
        with pytest.raises(Exception):
            Issue(**self._issue(category="Chaos"))

    def test_unknown_priority_is_rejected(self):
        with pytest.raises(Exception):
            Issue(**self._issue(priority="P9"))

    def test_blank_anchor_is_rejected(self):
        """锚点为空则定位层无法工作，必须在契约层就拦住。"""
        with pytest.raises(Exception):
            Issue(**self._issue(code_anchor="   "))

    def test_extra_fields_are_rejected(self):
        """多余字段必须变成显式错误，而不是被静默忽略。"""
        with pytest.raises(Exception):
            Issue(**self._issue(unexpected_field="x"))

    def test_fingerprint_is_stable_across_whitespace_changes(self):
        a = Issue(**self._issue(code_anchor="foo(  bar  )"))
        b = Issue(**self._issue(code_anchor="foo( bar )"))
        assert a.fingerprint == b.fingerprint

    def test_fingerprint_differs_by_category(self):
        a = Issue(**self._issue(category="Security"))
        b = Issue(**self._issue(category="Logic"))
        assert a.fingerprint != b.fingerprint

    def test_fingerprint_is_case_sensitive(self):
        """不能把 Foo 和 foo 当成同一处代码 —— Python 大小写敏感。"""
        a = Issue(**self._issue(code_anchor="Foo = 1"))
        b = Issue(**self._issue(code_anchor="foo = 1"))
        assert a.fingerprint != b.fingerprint

    def test_issue_list_wraps_dicts(self):
        result = IssueList(issues=[self._issue()])
        assert isinstance(result.issues[0], Issue)


class TestLocatedIssueInvariants:
    def test_unresolved_must_have_null_lines(self):
        """unresolved 携带行号是需求明令禁止的，必须是硬错误。"""
        with pytest.raises(Exception):
            LocatedIssue(issue_id="i1", match_status="unresolved", line_start=5)

    def test_unresolved_with_null_lines_is_fine(self):
        item = LocatedIssue(issue_id="i1", match_status="unresolved")
        assert item.line_start is None
        assert not item.is_resolved

    def test_resolved_requires_line_start(self):
        with pytest.raises(Exception):
            LocatedIssue(issue_id="i1", match_status="exact")

    def test_line_end_defaults_to_start(self):
        item = LocatedIssue(issue_id="i1", match_status="exact", line_start=7)
        assert item.line_end == 7

    def test_end_before_start_is_rejected(self):
        with pytest.raises(Exception):
            LocatedIssue(issue_id="i1", match_status="exact", line_start=10, line_end=3)

    def test_describe_location_uses_derived_lines(self):
        item = LocatedIssue(issue_id="i1", match_status="exact", line_start=3, line_end=5)
        assert item.describe_location() == "第 3-5 行"


class TestReportBlockingSemantics:
    """``blocking_escalation`` 是「什么算阻塞合并」的唯一定义处，这里守住它。

    它必须覆盖**三类**未通过：重试耗尽、未定位、补丁无法应用。漏掉第三类会造成
    一个非常安静的假通过（详见 core/schemas.py 里的说明）。
    """

    def _report_with_attempt(self, priority, status):
        report = ReviewReport(target_file="target.py")
        report.issues = [Issue(
            issue_id="i1",
            category="Security",
            priority=priority,
            severity="Blocker" if priority in ("P0", "P1") else "Minor",
            code_anchor="dangerous()",
            description="d",
        )]
        report.attempts = [FixAttempt(
            issue_id="i1", priority=priority, category="Security", status=status,
        )]
        return report

    def test_patch_failure_on_high_priority_blocks(self):
        report = self._report_with_attempt(Priority.P0, FixStatus.PATCH_FAILED)
        assert report.blocking_escalation is True

    def test_patch_failure_on_low_priority_does_not_block(self):
        """P2/P3 的补丁打不上只是改进建议，不能卡住所有人的 PR。"""
        report = self._report_with_attempt(Priority.P3, FixStatus.PATCH_FAILED)
        assert report.blocking_escalation is False

    def test_verified_attempt_does_not_block(self):
        report = self._report_with_attempt(Priority.P0, FixStatus.VERIFIED)
        assert report.blocking_escalation is False

    def test_report_survives_a_json_snapshot(self):
        """``--resume`` 会把报告 JSON 读回来，元素必须还原成模型而不是裸 dict。

        否则 ``blocking_escalation`` 里的 ``issue.is_blocking`` / ``attempt.status``
        会在恢复出来的报告上直接抛 AttributeError。
        """
        report = self._report_with_attempt(Priority.P0, FixStatus.PATCH_FAILED)
        report.status = RunStatus.BLOCKED

        restored = ReviewReport(**json.loads(report.json()))

        assert isinstance(restored.issues[0], Issue)
        assert isinstance(restored.attempts[0], FixAttempt)
        assert restored.blocking_escalation is True
        assert restored.status == RunStatus.BLOCKED

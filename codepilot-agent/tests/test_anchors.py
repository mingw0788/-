"""锚点定位引擎的表驱动测试。

这个模块的正确性直接决定整个工具可不可信，所以用表驱动把「应该命中」和
「必须拒绝」两类用例都钉死。特别是拒绝类用例 —— 宁可 unresolved 也不能给错行号。
"""
from __future__ import annotations

import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.anchors import FUZZY_THRESHOLD, match_anchor  # noqa: E402
from core.schemas import MatchStatus  # noqa: E402


SAMPLE = '''import os
import sqlite3


def get_user(conn, username):
    """按用户名查用户。"""
    cursor = conn.cursor()
    query = "SELECT * FROM users WHERE name = '" + username + "'"
    cursor.execute(query)
    return cursor.fetchone()


def safe_get_user(conn, username):
    cursor = conn.cursor()
    cursor.execute("SELECT * FROM users WHERE name = ?", (username,))
    return cursor.fetchone()


def compute(items):
    total = 0
    for i in range(len(items)):
        for j in range(len(items)):
            total += items[i] * items[j]
    return total
'''


class TestExactMatch:
    def test_single_line_anchor_hits_correct_line(self):
        anchor = '''query = "SELECT * FROM users WHERE name = '" + username + "'"'''
        result = match_anchor(anchor, SAMPLE)
        assert result.status == MatchStatus.EXACT
        assert (result.line_start, result.line_end) == (8, 8)
        assert result.confidence == 1.0

    def test_first_line_of_file(self):
        result = match_anchor("import os", SAMPLE)
        assert result.status == MatchStatus.EXACT
        assert result.line_start == 1
        assert result.line_end == 1

    def test_last_line_of_file(self):
        result = match_anchor("    return total", SAMPLE)
        assert result.status in (MatchStatus.EXACT, MatchStatus.NORMALIZED)
        assert result.line_start == 24
        assert result.line_end == 24

    def test_multi_line_anchor_spans_range(self):
        anchor = "def compute(items):\n    total = 0"
        result = match_anchor(anchor, SAMPLE)
        assert result.status == MatchStatus.EXACT
        assert (result.line_start, result.line_end) == (19, 20)

    def test_reports_occurrence_count(self):
        result = match_anchor("    return cursor.fetchone()", SAMPLE)
        assert result.occurs == 2


class TestNormalizedMatch:
    def test_anchor_with_different_indentation_still_matches(self):
        # LLM 常把缩进压平后输出锚点，归一化层必须兜住。
        anchor = 'query = "SELECT * FROM users WHERE name = \'" + username + "\'"'
        result = match_anchor(anchor, SAMPLE)
        assert result.is_resolved
        assert result.line_start == 8

    def test_trailing_comment_in_source_does_not_break_match(self):
        source = 'x = 1  # 初始化\n'
        result = match_anchor("x = 1", source)
        assert result.is_resolved
        assert result.line_start == 1

    def test_blank_lines_inside_anchor_are_ignored(self):
        anchor = "def compute(items):\n\n    total = 0"
        result = match_anchor(anchor, SAMPLE)
        # 精确层命中失败（原文该处无空行），归一化层应当兜住。
        assert result.is_resolved
        assert (result.line_start, result.line_end) == (19, 20)

    def test_crlf_source_is_normalized(self):
        source = "a = 1\r\nb = 2\r\nc = 3\r\n"
        result = match_anchor("b = 2", source)
        assert result.is_resolved
        assert result.line_start == 2


class TestFuzzyMatch:
    """模糊层是最后一道防线：越过阈值才认，否则宁可 unresolved。"""

    def test_near_miss_above_threshold_resolves_as_fuzzy(self):
        # 内层循环变量被改名（j -> k），精确层与归一化层都应失败，只能靠模糊层兜住。
        # 目标是第 21-23 行的嵌套循环块。
        anchor = (
            "for i in range(len(items)):\n"
            "        for k in range(len(items)):\n"
            "            total += items[i] * items[k]"
        )
        result = match_anchor(anchor, SAMPLE)
        assert result.status == MatchStatus.FUZZY
        assert (result.line_start, result.line_end) == (21, 23)
        assert result.confidence >= FUZZY_THRESHOLD

    def test_fuzzy_confidence_recorded_below_one(self):
        anchor = (
            "for i in range(len(items)):\n"
            "        for k in range(len(items)):\n"
            "            total += items[i] * items[k]"
        )
        result = match_anchor(anchor, SAMPLE)
        assert result.is_resolved
        assert 0.0 < result.confidence < 1.0

    def test_fuzzy_does_not_claim_dissimilar_code(self):
        anchor = "print('totally unrelated statement about network sockets')"
        result = match_anchor(anchor, SAMPLE)
        assert result.status == MatchStatus.UNRESOLVED


class TestMustNotResolve:
    """拒绝类用例：这些输入绝不能换来一个行号。"""

    def test_completely_absent_anchor_is_unresolved(self):
        anchor = "def function_that_is_not_in_the_file():\n    return 42"
        result = match_anchor(anchor, SAMPLE)
        assert result.status == MatchStatus.UNRESOLVED
        assert result.line_start is None
        assert result.line_end is None

    def test_unresolved_carries_no_fabricated_line_number(self):
        result = match_anchor("import nonexistent_module_xyz", SAMPLE)
        assert result.status == MatchStatus.UNRESOLVED
        assert result.line_start is None

    def test_empty_anchor_is_unresolved(self):
        for anchor in ("", "   ", "\n\n"):
            result = match_anchor(anchor, SAMPLE)
            assert result.status == MatchStatus.UNRESOLVED
            assert result.line_start is None

    def test_empty_source_is_unresolved(self):
        result = match_anchor("import os", "")
        assert result.status == MatchStatus.UNRESOLVED

    def test_whitespace_only_divergence_below_threshold_rejects(self):
        # 短锚点必须严格：不做归一化后 "pass" 命中，避免超短锚点到处误命中。
        source = "def f():\n    pass\n"
        result = match_anchor("def totally_other_thing(): return 999", source)
        assert result.status == MatchStatus.UNRESOLVED


class TestLineNumberEdges:
    def test_line_numbers_are_one_based(self):
        result = match_anchor("import os", SAMPLE)
        assert result.line_start == 1

    def test_anchor_at_end_of_file_without_trailing_newline(self):
        source = "a = 1\nb = 2"
        result = match_anchor("b = 2", source)
        assert result.line_start == 2
        assert result.line_end == 2

    def test_single_line_source(self):
        result = match_anchor("x = 1", "x = 1")
        assert result.line_start == 1
        assert result.line_end == 1

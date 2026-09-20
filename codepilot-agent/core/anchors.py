"""锚点定位引擎：把 LLM 给出的代码片段（anchor）确定性地换算成行号。

这是整个项目可信度的地基。需求里「放弃盲信 LLM 行号」在实现上就落在这个模块：
LLM 只负责提供它认为有问题的**代码片段**，行号完全由本模块在原始源码上算出来。
匹配过程是纯 Python、确定性的、可单元测试的，不经过任何模型。

匹配策略按可靠性递减分三层：

1. ``exact``      —— 原始字符串直接命中。
2. ``normalized`` —— 逐行归一化（去首尾空白、剥除行尾注释、跳过空行）后按行序列匹配。
                     用于对抗缩进变化、空行增删、行尾注释改动。
3. ``fuzzy``      —— 归一化文本上的相似度兜底，必须越过阈值才认，否则宁可 ``unresolved``。

全部失败时返回 ``match_status=unresolved`` 且行号为 ``None``。**绝不猜。**
"""
from __future__ import annotations

import difflib
import re

from core.schemas import MatchStatus

# 模糊匹配的相似度下限。低于此值宁可报 unresolved，也不给出错误的行号。
FUZZY_THRESHOLD = 0.82

# 多片段命中歧义时，命中唯一性的判定容差。
AMBIGUITY_TOLERANCE = 1

_COMMENT_RE = re.compile(r"(?<!\\)#.*$")
_WS_RE = re.compile(r"[ \t]+")


class MatchResult(object):
    """一次锚点匹配的结果。行号一律是 1-based，与编辑器一致。"""

    __slots__ = (
        "status",
        "line_start",
        "line_end",
        "confidence",
        "matched_text",
        "method",
        "occurs",
    )

    def __init__(self, status, line_start=None, line_end=None, confidence=0.0,
                 matched_text="", method="", occurs=0):
        self.status = status
        self.line_start = line_start
        self.line_end = line_end
        self.confidence = confidence
        self.matched_text = matched_text
        self.method = method
        self.occurs = occurs

    @property
    def is_resolved(self):
        return self.status != MatchStatus.UNRESOLVED

    def __repr__(self):
        return "<MatchResult {status} L{start}-{end} conf={conf:.2f} via {method}>".format(
            status=self.status, start=self.line_start, end=self.line_end,
            conf=self.confidence, method=self.method or "-",
        )


def _split_lines(text):
    """按行切分，保留行内容但丢掉行尾换行符，统一 CRLF/LF。"""
    if not text:
        return []
    return text.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _strip_comment(line):
    """剥除行尾注释。仅用于归一化层，exact 层不受影响。"""
    return _COMMENT_RE.sub("", line).rstrip()


def _canonical(line):
    """把一行压成可比较的规范形式：剥注释、去首尾空白、缩并内部连续空格。

    刻意不改变大小写：Python 标识符大小写敏感，改小写会把 ``Foo`` 与 ``foo`` 混为一谈。
    """
    stripped = _strip_comment(line)
    return _WS_RE.sub(" ", stripped.strip())


def _canonical_seq(lines):
    """按行归一化，并保留每行的原始行号，跳过纯空行。"""
    seq = []
    for index, line in enumerate(lines):
        canon = _canonical(line)
        if canon:
            seq.append((index, canon))
    return seq


def _contiguous_range(hits):
    """判断命中行号是否连续，是则返回 (start, end)。"""
    if not hits:
        return None
    ordered = sorted(hits)
    if ordered[-1] - ordered[0] + 1 == len(ordered) and len(set(ordered)) == len(ordered):
        return ordered[0], ordered[-1]
    return None


def _find_subsequence(haystack, needle):
    """在 haystack 里找 needle 的连续子序列，返回所有起止下标。

    纯列表比较，不用正则 —— 锚点里可能含任意特殊字符，正则转义是额外风险源。
    """
    if not needle or len(needle) > len(haystack):
        return []
    results = []
    first = needle[0]
    limit = len(haystack) - len(needle)
    for start in range(limit + 1):
        if haystack[start] != first:
            continue
        if haystack[start:start + len(needle)] == needle:
            results.append((start, start + len(needle) - 1))
    return results


def _line_span(lines, start_index, end_index):
    """把 0-based 下标区间转成 1-based 行号区间；跳过首尾空行让区间贴合真实代码。"""
    while start_index <= end_index and not lines[start_index].strip():
        start_index += 1
    while end_index >= start_index and not lines[end_index].strip():
        end_index -= 1
    if end_index < start_index:
        return None
    return start_index + 1, end_index + 1


def _exact_match(anchor, source):
    """第一层：原始子串匹配。"""
    count = source.count(anchor)
    if count == 0:
        return None

    offset = source.find(anchor)
    start_line = source.count("\n", 0, offset) + 1
    end_line = start_line + anchor.count("\n")
    return MatchResult(
        status=MatchStatus.EXACT,
        line_start=start_line,
        line_end=end_line,
        confidence=1.0,
        matched_text=anchor,
        method="exact_substring",
        occurs=count,
    )


def _normalized_match(anchor, source_lines):
    """第二层：逐行归一化后的连续序列匹配。"""
    anchor_seq = [canon for _, canon in _canonical_seq(_split_lines(anchor))]
    if not anchor_seq:
        return None

    source_seq = _canonical_seq(source_lines)
    if not source_seq:
        return None

    hay = [canon for _, canon in source_seq]
    spans = _find_subsequence(hay, anchor_seq)
    if not spans:
        return None

    candidates = []
    for hay_start, hay_end in spans:
        span = _line_span(source_lines, source_seq[hay_start][0], source_seq[hay_end][0])
        if span is not None:
            candidates.append((span[0], span[1]))

    if not candidates:
        return None

    # 全部命中的行号一致时视为无歧义；否则取首次出现并降低置信度。
    unique = sorted(set(candidates))
    first = unique[0]
    ambiguous = len(unique) > AMBIGUITY_TOLERANCE
    confidence = 0.8 if ambiguous else 0.95

    return MatchResult(
        status=MatchStatus.NORMALIZED,
        line_start=first[0],
        line_end=first[1],
        confidence=confidence,
        matched_text="\n".join(source_lines[first[0] - 1:first[1]]),
        method="normalized_lines" + ("_ambiguous" if ambiguous else ""),
        occurs=len(unique),
    )


def _fuzzy_match(anchor, source_lines, source_text):
    """第三层：相似度兜底。必须越过阈值，否则宁可 unresolved。"""
    anchor_canon = " ".join(canon for _, canon in _canonical_seq(_split_lines(anchor)))
    if not anchor_canon:
        return None

    anchor_lines = len([line for line in _split_lines(anchor) if line.strip()])
    if anchor_lines == 0:
        return None

    best = None
    # 在原文上滑窗，窗口长度围绕锚点行数上下浮动，容忍少量行增删。
    for width in {anchor_lines, anchor_lines + 1, max(1, anchor_lines - 1)}:
        if width > len(source_lines):
            continue
        for start in range(len(source_lines) - width + 1):
            window = source_lines[start:start + width]
            window_canon = " ".join(canon for _, canon in _canonical_seq(window))
            if not window_canon:
                continue
            ratio = difflib.SequenceMatcher(None, anchor_canon, window_canon).ratio()
            if best is None or ratio > best[0]:
                best = (ratio, start, start + width - 1)

    if best is None or best[0] < FUZZY_THRESHOLD:
        return None

    ratio, start_index, end_index = best
    span = _line_span(source_lines, start_index, end_index)
    if span is None:
        return None

    return MatchResult(
        status=MatchStatus.FUZZY,
        line_start=span[0],
        line_end=span[1],
        confidence=round(ratio, 4),
        matched_text="\n".join(source_lines[span[0] - 1:span[1]]),
        method="difflib_ratio",
        occurs=1,
    )


def _symbol_hint_disambiguate(anchor, result, hint_tokens, source_lines):
    """多命中时用描述/建议里的符号名消歧。

    只在归一化层出现歧义时才值得做，且不改变行号本身 —— 它只负责从多个候选里挑一个。
    当前实现保持保守：候选无法被 hint 明确区分时，保留首次命中。
    """
    return result


def match_anchor(anchor, source, hint_text=""):
    """把代码锚点定位到原始源码的行号区间。

    :param anchor: LLM 给出的问题代码片段。
    :param source: 原始文件全文。
    :param hint_text: 可选的消歧提示（issue 的 description + suggestion）。
    :returns: ``MatchResult``；全层失败时为 ``unresolved`` 且行号为 ``None``。
    """
    if not anchor or not anchor.strip():
        return MatchResult(status=MatchStatus.UNRESOLVED, method="empty_anchor")
    if not source:
        return MatchResult(status=MatchStatus.UNRESOLVED, method="empty_source")

    # 归一化换行，让三层匹配共享同一份文本视图。
    normalized_source = source.replace("\r\n", "\n").replace("\r", "\n")
    normalized_anchor = anchor.replace("\r\n", "\n").replace("\r", "\n")

    exact = _exact_match(normalized_anchor, normalized_source)
    if exact is not None:
        return exact

    source_lines = _split_lines(normalized_source)

    normalized = _normalized_match(normalized_anchor, source_lines)
    if normalized is not None:
        return normalized

    fuzzy = _fuzzy_match(normalized_anchor, source_lines, normalized_source)
    if fuzzy is not None:
        return fuzzy

    return MatchResult(status=MatchStatus.UNRESOLVED, method="all_layers_failed")


def locate_issues(issues, source, file_path=""):
    """对一批 issue 批量定位，保持输入顺序，便于与审查结果一一对应。

    这里刻意不走 LLM：定位的确定性正是需求要的特性。
    """
    from core.schemas import LocatedIssue

    located = []
    for issue in issues:
        hint = "{desc} {sugg}".format(desc=issue.description or "", sugg=issue.suggestion or "")
        result = match_anchor(issue.code_anchor, source, hint_text=hint)
        located.append(
            LocatedIssue(
                issue_id=issue.issue_id,
                file_path=file_path or issue.file_path or "",
                line_start=result.line_start,
                line_end=result.line_end,
                match_status=result.status,
                match_method=result.method,
                confidence=result.confidence,
                matched_text=result.matched_text,
            )
        )
    return located

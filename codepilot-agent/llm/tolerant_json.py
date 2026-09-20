"""LLM 响应的容错解析。

需求要求「JSON 解析容错」，但容错必须有边界：能从噪声里救回合法 JSON，但**绝不**猜测或
补全缺失字段 —— 那是 Pydantic 契约层该拒绝的事。本模块只负责「把文本变成 dict」，
字段合法性交给 ``core.schemas``。

解析管线，逐级降级：

1. 剥离 Markdown 代码围栏（```json / ```）。
2. 直接 ``json.loads``。
3. 提取第一个**平衡**的 ``{...}`` 块再解析（应对模型在 JSON 前后写了寒暄）。
4. 轻量修复：去尾逗号、把单引号键值转双引号、``True/False/None`` 归一化成 JSON 字面量。
5. 全部失败则抛 ``SchemaValidationError``，并带上原文片段供诊断 / 沉淀成 fixture。

刻意不做的事：不用正则「猜」字段、不做模糊键名匹配（``category`` 不会匹配 ``Category``）。
键名歧义会悄悄改变语义，宁可让上游重试一次。
"""
from __future__ import annotations

import json
import re

from core.errors import SchemaValidationError

# 用于诊断信息截断，避免把整份代码糊进异常里。
RAW_EXCERPT_LIMIT = 500

_FENCE_RE = re.compile(r"^\s*```[a-zA-Z0-9_+-]*\s*$", re.MULTILINE)
_TRAILING_COMMA_RE = re.compile(r",(\s*[}\]])")
_SINGLE_QUOTED_RE = re.compile(r"'([^'\\]*(?:\\.[^'\\]*)*)'")
_PY_LITERAL_RE = re.compile(r"\b(True|False|None)\b")


def strip_code_fence(text):
    """剥掉 Markdown 代码围栏，保留围栏内部内容。

    模型经常无视「不要包含 ```json 标记」的约束，这里是第一道也是最常见的一道防线。
    """
    if not text:
        return ""
    cleaned = text.strip()
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        # 去掉开头的 ```xxx 行
        if _FENCE_RE.match(lines[0]):
            lines = lines[1:]
        # 去掉结尾的 ```
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines)
    return cleaned.strip()


def extract_balanced_json(text, open_char="{", close_char="}"):
    """扫出第一个括号平衡的片段。

    必须自己维护字符串状态：``{"a": "}"}`` 里的右花括号在字符串内，不能当结束符。
    正则做不到这点，所以这里是手写扫描。
    """
    if not text:
        return None

    start = text.find(open_char)
    if start == -1:
        return None

    depth = 0
    in_string = False
    escaped = False

    for index in range(start, len(text)):
        char = text[index]

        if in_string:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                in_string = False
            continue

        if char == '"':
            in_string = True
        elif char == open_char:
            depth += 1
        elif char == close_char:
            depth -= 1
            if depth == 0:
                return text[start:index + 1]

    return None


def repair_json_literals(text):
    """轻量修复常见的手写 JSON 偏差。

    只做三种全局安全的替换：尾逗号、Python 布尔/空值字面量、单引号字符串。
    单引号替换放在最后，且只处理不含双引号的简单串，避免破坏 SQL 里合法的引号。
    """
    if not text:
        return text

    repaired = _TRAILING_COMMA_RE.sub(r"\1", text)
    repaired = _PY_LITERAL_RE.sub(
        lambda match: {"True": "true", "False": "false", "None": "null"}[match.group(1)],
        repaired,
    )
    return repaired


def _single_quote_to_double(text):
    """把单引号包裹的字符串转成双引号。仅在直接解析失败后作为最后手段调用。"""

    def replace(match):
        inner = match.group(1).replace('"', '\\"')
        return '"{inner}"'.format(inner=inner)

    return _SINGLE_QUOTED_RE.sub(replace, text)


def parse_json_response(text, context="LLM 响应"):
    """把 LLM 原始文本解析成 dict。

    :raises SchemaValidationError: 所有降级手段都失败时抛出，携带原文片段。
    """
    raw = text or ""
    if not raw.strip():
        raise SchemaValidationError(
            "{ctx} 为空".format(ctx=context), raw_text=raw, detail="empty_response"
        )

    candidates = []

    stripped = strip_code_fence(raw)
    candidates.append(("fence_stripped", stripped))
    candidates.append(("raw", raw.strip()))

    # 逐级尝试：直接解析 → 平衡块 → 修复后解析。
    for label, candidate in candidates:
        if not candidate:
            continue
        parsed = _try_loads(candidate)
        if parsed is not None:
            return parsed

        balanced = extract_balanced_json(candidate)
        if balanced:
            parsed = _try_loads(balanced)
            if parsed is not None:
                return parsed

            repaired = repair_json_literals(balanced)
            parsed = _try_loads(repaired)
            if parsed is not None:
                return parsed

            parsed = _try_loads(_single_quote_to_double(repaired))
            if parsed is not None:
                return parsed

    excerpt = raw[:RAW_EXCERPT_LIMIT]
    raise SchemaValidationError(
        "{ctx} 无法解析为 JSON（已尝试剥围栏 / 平衡块提取 / 字面量修复 / 单引号转换）".format(
            ctx=context
        ),
        raw_text=raw,
        detail="unparseable; excerpt={!r}".format(excerpt),
    )


def _try_loads(text):
    """尝试解析，失败返回 None（不抛）。解析成功但顶层不是 dict 也返回 None。"""
    try:
        parsed = json.loads(text)
    except (ValueError, TypeError):
        return None
    if isinstance(parsed, dict):
        return parsed
    return None


def coerce_issues_payload(payload):
    """把解析结果规整成 ``IssueList`` 可接受的形状。

    容忍两种常见的模型偏差：
    - 顶层直接是数组（``[{...}, {...}]``）。
    - 单个 issue 被直接返回，没有 ``issues`` 包裹。

    只做结构层面的规整，不做字段层面的臆测。
    """
    if isinstance(payload, list):
        return {"issues": payload}
    if not isinstance(payload, dict):
        return {"issues": []}

    if "issues" not in payload and "category" in payload and "code_anchor" in payload:
        return {"issues": [payload]}

    return payload


def coerce_list_payload(payload, key, item_marker):
    """通用规整：处理顶层数组、以及 key 缺失但对象本身就是一条记录的情况。"""
    if isinstance(payload, list):
        return {key: payload}
    if not isinstance(payload, dict):
        return {key: []}

    if key not in payload and item_marker in payload:
        return {key: [payload]}

    return payload

"""大文件切块与依赖注入。

需求第三部分要求解决「大文件超限」与「跨文件上下文」两个问题。这里用 ``ast`` 实现，
不引入 ``tree-sitter`` —— 后者在 Windows + Python 3.9 上需要编译工具链，是个纯粹的
部署风险；而被审查语言当前只支持 Python，标准库 ``ast`` 完全够用。

切块策略：按顶层类 / 函数切，保留模块级语句（import、常量、模块 docstring）作为
``module_header`` 附加到每个 chunk 上 —— 否则模型看不到 import，会把「未定义的名字」
误报成问题，或无法判断依赖版本相关问题。
"""
from __future__ import annotations

import ast
import re

# 触发切块的估算阈值（近似 token 数）。粗估 1 token ≈ 4 字符，只用于决定是否切块。
DEFAULT_CHAR_BUDGET = 24000

_IMPORT_RE = re.compile(r"^\s*(?:from\s+([\w.]+)\s+import|import\s+([\w.]+))", re.MULTILINE)


class Chunk(object):
    """一个待审查的代码块。"""

    __slots__ = ("chunk_id", "name", "kind", "text", "start_line", "end_line", "symbols")

    def __init__(self, chunk_id, name, kind, text, start_line, end_line, symbols=None):
        self.chunk_id = chunk_id
        self.name = name
        self.kind = kind
        self.text = text
        self.start_line = start_line
        self.end_line = end_line
        self.symbols = symbols or []

    @property
    def size(self):
        return len(self.text)

    def __repr__(self):
        return "<Chunk {kind}:{name} L{start}-{end} {size}B>".format(
            kind=self.kind, name=self.name, start=self.start_line,
            end=self.end_line, size=self.size,
        )


def estimate_tokens(text):
    """粗略估算 token 数。刻意用简单启发式 —— 这里只需要一个量级判断。"""
    if not text:
        return 0
    # 非 ASCII 字符（中文注释）token 密度更高，按经验加权。
    non_ascii = sum(1 for char in text if ord(char) > 127)
    ascii_count = len(text) - non_ascii
    return int(ascii_count / 4 + non_ascii / 1.5)


def _segment_lines(source):
    """按行切分并保留原始行号，行尾换行符丢掉。"""
    return source.replace("\r\n", "\n").replace("\r", "\n").split("\n")


def _node_span(node, lines):
    """取节点的行区间（1-based，闭区间）。

    用 ``end_lineno``（Python 3.8+）而不是自己数，避免装饰器与多行定义算错。
    """
    start = getattr(node, "lineno", None)
    end = getattr(node, "end_lineno", None)
    if start is None:
        return None
    if end is None:
        end = start
    return start, end


def _extract_module_header(tree, lines):
    """提取模块级语句（import、常量、docstring）作为公共头部。"""
    header_end = 0
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            span = _node_span(node, lines)
            if span:
                header_end = max(header_end, span[1])
        elif isinstance(node, ast.Assign):
            span = _node_span(node, lines)
            if span:
                header_end = max(header_end, span[1])

    if header_end == 0:
        return "", 0
    return "\n".join(lines[:header_end]), header_end


def extract_imports(source):
    """抽取源码里的 import 目标，供 ``ProjectContext.related_files`` 使用。"""
    found = []
    seen = set()
    for match in _IMPORT_RE.finditer(source):
        target = match.group(1) or match.group(2) or ""
        root = target.split(".")[0]
        if root and root not in seen:
            seen.add(root)
            found.append(root)
    return found


def extract_symbols(source):
    """抽取顶层定义的符号名，用于跨文件引用推断。"""
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return []
    names = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.append(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    names.append(target.id)
    return names


def chunk_source(source, char_budget=DEFAULT_CHAR_BUDGET):
    """把源码切成待审查的 chunk 列表。

    行为约定：
    - 文件不大（未超阈值）时返回**单个** chunk，内容是全文 —— 不切块能保留最完整的
      上下文，模型对跨函数问题的判断更准。切块是超限时的降级手段，不该默认开启。
    - 语法错误时整体作为一个 chunk 返回（语法错误本身就是审查目标之一，不能因为
      解析失败就拒绝审查）。
    - 每个 chunk 都带上模块头部（import / 常量），避免模型误报未定义名。
    """
    if not source or not source.strip():
        return [Chunk("all", "<empty>", "empty", source or "", 1, 1)]

    lines = _segment_lines(source)

    if estimate_tokens(source) <= char_budget / 4:
        return [Chunk("all", "<whole-file>", "whole_file", source, 1, len(lines))]

    try:
        tree = ast.parse(source)
    except SyntaxError:
        return [Chunk("all", "<syntax-error>", "whole_file", source, 1, len(lines))]

    header_text, header_end = _extract_module_header(tree, lines)
    chunks = []
    index = 0

    for node in tree.body:
        if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            continue

        span = _node_span(node, lines)
        if span is None:
            continue

        start, end = span

        if isinstance(node, ast.ClassDef):
            kind = "class"
        elif isinstance(node, ast.AsyncFunctionDef):
            kind = "async_function"
        else:
            kind = "function"

        body = "\n".join(lines[start - 1:end])
        text = _compose_chunk(header_text, body)
        symbols = extract_symbols(body)

        chunks.append(
            Chunk(
                chunk_id="chunk-{}".format(index),
                name=node.name,
                kind=kind,
                text=text,
                start_line=start,
                end_line=end,
                symbols=symbols,
            )
        )
        index += 1

    if not chunks:
        # 没有顶层定义（纯脚本），只能整体作为一块。
        return [Chunk("all", "<script>", "whole_file", source, 1, len(lines))]

    return chunks


def _compose_chunk(header_text, body):
    """拼装 chunk 正文：模块头 + 分隔注释 + 定义体。"""
    if not header_text:
        return body
    return (
        "# --- 模块级上下文（import / 常量，供参考，勿报告其中的问题）---\n"
        "{header}\n"
        "# --- 以上为模块级上下文，以下为待审查代码 ---\n"
        "{body}"
    ).format(header=header_text, body=body)


def build_related_files(source, known_files=None):
    """推断当前文件引用到的其他文件，作为 ``related_files`` 注入 ProjectContext。"""
    related = []
    for module in extract_imports(source):
        if known_files and module not in known_files:
            # 外部依赖，记成依赖而非关联文件，避免把第三方库当成待审代码。
            continue
        related.append(module)
    return related

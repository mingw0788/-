"""切块与依赖注入的测试。

这一层的关键约定是**默认不切**：只有超出预算才切，因为切块会割裂跨函数的上下文，
让模型对「这个变量是哪里来的」判断失准。切块是超限时的降级手段，不是默认行为 ——
所以测试里最重要的一条就是「小文件必须是单个 chunk」。
"""
from __future__ import annotations

import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.chunker import (  # noqa: E402
    Chunk,
    build_related_files,
    chunk_source,
    estimate_tokens,
    extract_imports,
    extract_symbols,
)

SMALL_SOURCE = """import os

CONSTANT = 1


def alpha():
    return CONSTANT


def beta():
    return alpha()
"""

BIG_SOURCE = """import os
import sys

VERSION = "1.0"

{body}

def tail_function():
    return 1
"""


def _big_source(function_count=6, filler_lines=400):
    body = "\n\n".join(
        "def generated_{index}():\n{padding}    return {index}".format(
            index=index,
            padding="".join("    x{0} = {1}\n".format(i, i) for i in range(filler_lines)),
        )
        for index in range(function_count)
    )
    return BIG_SOURCE.format(body=body)


class TestEstimateTokens:
    def test_empty_is_zero(self):
        assert estimate_tokens("") == 0

    def test_longer_text_estimates_more(self):
        assert estimate_tokens("a" * 400) > estimate_tokens("a" * 40)

    def test_chinese_counts_denser_than_ascii(self):
        """中文注释的 token 密度更高，等字符数下估算值必须更大。"""
        assert estimate_tokens("中" * 100) > estimate_tokens("a" * 100)


class TestNoSplitByDefault:
    def test_small_file_is_a_single_chunk(self):
        """小文件绝不能切 —— 切块会丢掉跨函数上下文。"""
        chunks = chunk_source(SMALL_SOURCE)
        assert len(chunks) == 1
        assert chunks[0].kind == "whole_file"
        assert chunks[0].text == SMALL_SOURCE

    def test_whole_file_chunk_covers_all_lines(self):
        chunk = chunk_source(SMALL_SOURCE)[0]
        assert chunk.start_line == 1
        assert chunk.end_line == len(SMALL_SOURCE.split("\n"))


class TestSplitWhenOverBudget:
    def test_large_file_is_split_by_top_level_definition(self):
        source = _big_source()
        chunks = chunk_source(source, char_budget=100)
        names = [chunk.name for chunk in chunks]
        assert "generated_0" in names
        assert "tail_function" in names
        assert "whole_file" not in [chunk.kind for chunk in chunks]

    def test_each_chunk_carries_module_header(self):
        """chunk 必须带上 import / 常量，否则模型会把 os.VERSION 之类的误报成未定义名。"""
        source = _big_source()
        chunks = chunk_source(source, char_budget=100)
        for chunk in chunks:
            assert "import os" in chunk.text
            assert 'VERSION = "1.0"' in chunk.text

    def test_chunk_ids_are_unique(self):
        chunks = chunk_source(_big_source(), char_budget=100)
        ids = [chunk.chunk_id for chunk in chunks]
        assert len(ids) == len(set(ids))

    def test_chunk_line_ranges_are_within_source(self):
        source = _big_source()
        total_lines = len(source.split("\n"))
        for chunk in chunk_source(source, char_budget=100):
            assert 1 <= chunk.start_line <= chunk.end_line <= total_lines

    def test_chunk_ids_are_deterministic(self):
        """同样输入必须给同样切分，否则报告里的 chunk 引用无法复现。"""
        first = chunk_source(_big_source(), char_budget=100)
        second = chunk_source(_big_source(), char_budget=100)
        assert [c.chunk_id for c in first] == [c.chunk_id for c in second]
        assert [c.text for c in first] == [c.text for c in second]


class TestDegradedInputs:
    def test_empty_source_yields_placeholder_chunk(self):
        chunks = chunk_source("")
        assert len(chunks) == 1
        assert chunks[0].kind == "empty"

    def test_whitespace_only_source(self):
        assert len(chunk_source("   \n  ")) == 1

    def test_syntax_error_still_reviewable(self):
        """语法错误本身就是审查目标之一，不能因为解析失败就拒绝审查。"""
        broken = "def broken(\n    pass\n"
        chunks = chunk_source(broken, char_budget=1)
        assert len(chunks) == 1
        assert chunks[0].kind == "whole_file"
        assert chunks[0].text == broken

    def test_script_without_definitions_falls_back_to_whole_file(self):
        script = "\n".join("print({0})".format(i) for i in range(2000))
        chunks = chunk_source(script, char_budget=100)
        assert len(chunks) == 1
        assert chunks[0].kind == "whole_file"

    def test_class_is_split_as_a_unit(self):
        source = "class Widget(object):\n" + "".join(
            "    def method_{0}(self):\n        return {0}\n\n".format(i) for i in range(300)
        )
        chunks = chunk_source(source, char_budget=100)
        assert len(chunks) == 1
        assert chunks[0].kind == "class"
        assert chunks[0].name == "Widget"

    def test_async_function_kind(self):
        source = "async def worker():\n" + "".join(
            "    value_{0} = {0}\n".format(i) for i in range(1200)
        )
        chunks = chunk_source(source, char_budget=100)
        assert chunks[0].kind == "async_function"


class TestExtractImports:
    def test_plain_imports(self):
        assert extract_imports("import os\nimport sys\n") == ["os", "sys"]

    def test_from_import_uses_root_module(self):
        assert extract_imports("from os.path import join\n") == ["os"]

    def test_dotted_import_uses_root(self):
        assert extract_imports("import xml.etree.ElementTree\n") == ["xml"]

    def test_duplicates_are_collapsed(self):
        assert extract_imports("import os\nimport os\n") == ["os"]

    def test_no_imports(self):
        assert extract_imports("x = 1\n") == []


class TestExtractSymbols:
    def test_functions_and_classes(self):
        assert extract_symbols("def a():\n    pass\n\n\nclass B:\n    pass\n") == ["a", "B"]

    def test_module_level_assignment(self):
        assert "CONFIG" in extract_symbols("CONFIG = {}\n")

    def test_syntax_error_returns_empty(self):
        """抽符号是辅助信息，解析失败时返回空而不是抛异常打断整个审查。"""
        assert extract_symbols("def broken(\n") == []

    def test_lambda_assignment_does_not_crash(self):
        extract_symbols("f = lambda x: x\n")


class TestBuildRelatedFiles:
    def test_known_module_is_included(self):
        source = "import helper\nimport os\n"
        assert build_related_files(source, known_files={"helper"}) == ["helper"]

    def test_unknown_module_is_excluded(self):
        """不在项目内的 import 是外部依赖，不能当待审代码。"""
        source = "import helper\nimport os\n"
        assert build_related_files(source, known_files={"helper", "other"}) == ["helper"]

    def test_without_known_files_everything_is_related(self):
        assert build_related_files("import os\n") == ["os"]


class TestChunkObject:
    def test_size_reflects_text_length(self):
        chunk = Chunk("c1", "n", "function", "abcdef", 1, 1)
        assert chunk.size == 6

    def test_symbols_default_to_empty_list(self):
        assert Chunk("c1", "n", "function", "x", 1, 1).symbols == []

    def test_repr_shows_name_and_span(self):
        """repr 用于测试失败时快速定位，至少要能看出是哪个定义、落在哪几行。"""
        text = repr(Chunk("c1", "n", "function", "x", 1, 2))
        assert "function:n" in text
        assert "L1-2" in text

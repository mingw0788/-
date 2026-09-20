"""Patch 校验与应用的测试。

这一层是「能不能安全改代码」的关口，所以两类用例都要有：
能正确应用的（正向），以及必须被拒绝的（负向 —— 打错地方比不打更危险）。

**补丁一律用 difflib 生成，不手写。** 手写 `@@` 计数极易出错，而 ``git apply``
对计数的要求是精确相等（上下文 3 行就必须写 3，写 4 直接 ``corrupt patch``），
手写等于给自己埋一个「测试失败但产品代码没问题」的坑。
"""
from __future__ import annotations

import difflib
import os
import sys

import pytest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core.errors import PatchError  # noqa: E402
from core.patch import (  # noqa: E402
    apply_patch_in_memory,
    git_available,
    looks_like_patch,
    normalize_patch_headers,
    prepare_patch,
    strip_patch_fence,
    validate_with_git,
    verify_and_apply,
)

# 末尾带换行，与磁盘上的真实文件一致。
SOURCE = """def greet(name):
    message = "hello " + name
    return message


def farewell(name):
    message = "bye " + name
    return message
"""


def make_patch(source, old_snippet, new_snippet, file_name="target.py", n=3):
    """从源码派生补丁。计数由 difflib 保证，绝不手写。"""
    if source.count(old_snippet) < 1:
        raise AssertionError("锚点不存在：{!r}".format(old_snippet[:60]))
    patched = source.replace(old_snippet, new_snippet, 1)
    return "".join(
        difflib.unified_diff(
            source.splitlines(True),
            patched.splitlines(True),
            fromfile="a/{name}".format(name=file_name),
            tofile="b/{name}".format(name=file_name),
            n=n,
        )
    )


SIMPLE_PATCH = make_patch(
    SOURCE, '    message = "hello " + name', '    message = f"hello {name}"'
)

AMBIGUOUS_PATCH = make_patch(
    SOURCE, '    message = "bye " + name', '    message = f"bye {name}"'
)


def unusable_patch(source, old_snippet, new_snippet="    replaced_for_test = True\n"):
    """生成一个格式合法、但作用在**别的**源码上的补丁。

    用它来测试「上下文对不上必须被拒绝」：补丁本身能被正常解析，
    唯一的问题是它跟目标源码不是一个版本 —— 这正是真实场景里最常见的失败原因
    （模型基于旧版本代码生成补丁）。格式合法很重要，否则测试会因为格式错误而通过，
    掩盖掉上下文校验这条真正想验的路径。
    """
    return make_patch(source, old_snippet, new_snippet, n=1)


# 一份与 SOURCE 完全无关的源码，用来产出「上下文必然对不上」的补丁。
FOREIGN_SOURCE = """def entirely_different():
    nothing = 1
    return nothing
"""

FOREIGN_PATCH = unusable_patch(FOREIGN_SOURCE, "    nothing = 1")


class TestPatchDetection:
    def test_valid_patch_is_recognized(self):
        assert looks_like_patch(SIMPLE_PATCH)

    def test_plain_text_is_not_a_patch(self):
        assert not looks_like_patch("这是一段解释，不是补丁。")

    def test_fenced_patch_is_recognized(self):
        fenced = "```diff\n" + SIMPLE_PATCH + "```"
        assert looks_like_patch(strip_patch_fence(fenced))


class TestStripFence:
    def test_diff_fence_removed(self):
        assert strip_patch_fence("```diff\n" + SIMPLE_PATCH + "```") == SIMPLE_PATCH

    def test_crlf_normalized(self):
        """CRLF 在 Windows 上会让 git 报 corrupt patch，必须在这一层就统一。"""
        assert "\r" not in strip_patch_fence(SIMPLE_PATCH.replace("\n", "\r\n"))

    def test_trailing_newline_added(self):
        assert strip_patch_fence(SIMPLE_PATCH.rstrip("\n")).endswith("\n")

    def test_leading_blank_lines_removed(self):
        assert strip_patch_fence("\n\n" + SIMPLE_PATCH) == SIMPLE_PATCH


class TestNormalizeHeaders:
    def test_headers_unified_to_target_name(self):
        patch = SIMPLE_PATCH.replace("target.py", "some/other/name.py")
        result = normalize_patch_headers(patch, "real.py")
        assert "--- a/real.py" in result
        assert "+++ b/real.py" in result
        assert "name.py" not in result


class TestPreparePatch:
    def test_empty_patch_rejected(self):
        with pytest.raises(PatchError):
            prepare_patch("", "target.py")

    def test_non_patch_text_rejected(self):
        with pytest.raises(PatchError):
            prepare_patch("模型忘记输出补丁了，只说了一句抱歉。", "target.py")

    def test_valid_patch_passes(self):
        prepared = prepare_patch(SIMPLE_PATCH, "target.py")
        assert "@@" in prepared


class TestApplyInMemory:
    def test_simple_patch_applies(self):
        new_code, hunks = apply_patch_in_memory(SOURCE, SIMPLE_PATCH, "target.py")
        assert hunks == 1
        assert 'f"hello {name}"' in new_code
        assert '"hello " + name' not in new_code

    def test_only_first_occurrence_is_replaced(self):
        """`farewell` 里的 `"bye " + name` 不应被 `hello` 的补丁波及。"""
        new_code, _ = apply_patch_in_memory(SOURCE, SIMPLE_PATCH, "target.py")
        assert '"bye " + name' in new_code

    def test_declared_position_is_used_when_context_matches(self):
        """两处代码相似时，必须落到 hunk 头声明的那一处。"""
        new_code, _ = apply_patch_in_memory(SOURCE, AMBIGUOUS_PATCH, "target.py")
        assert 'f"bye {name}"' in new_code
        assert '"hello " + name' in new_code

    def test_context_mismatch_is_rejected(self):
        """上下文对不上就必须报错 —— 打错地方比不打更危险。

        补丁内容合法（能被解析），但对一份**不同的**源码生成，所以上下文必然对不上。
        """
        bad = FOREIGN_PATCH
        with pytest.raises(PatchError) as excinfo:
            apply_patch_in_memory(SOURCE, bad, "target.py")
        assert excinfo.value.detail == "hunk_context_mismatch"

    def test_multiple_hunks_applied_back_to_front(self):
        """同一文件的多个 hunk 必须从后往前应用，否则前面的改动会顶掉后面的行号。"""
        once = SOURCE.replace('    message = "hello " + name', '    message = f"hello {name}"')
        twice = once.replace('    message = "bye " + name', '    message = f"bye {name}"')
        patch = "".join(difflib.unified_diff(
            SOURCE.splitlines(True), twice.splitlines(True),
            fromfile="a/target.py", tofile="b/target.py", n=1,
        ))
        new_code, hunks = apply_patch_in_memory(SOURCE, patch, "target.py")
        assert hunks == 2
        assert 'f"hello {name}"' in new_code
        assert 'f"bye {name}"' in new_code

    def test_patch_with_context_blank_lines(self):
        """diff 里的 context 空行容易被 rstrip 成空串，解析器必须能兜住。"""
        source = "a = 1\n\n\nb = 2\n"
        patch = """--- a/target.py
+++ b/target.py
@@ -1,4 +1,4 @@
 a = 1


-b = 2
+b = 3
"""
        new_code, _ = apply_patch_in_memory(source, patch, "target.py")
        assert "b = 3" in new_code

    def test_patch_with_no_hunks_rejected(self):
        with pytest.raises(PatchError):
            apply_patch_in_memory(SOURCE, "--- a/x\n+++ b/x\n", "target.py")


class TestOriginalFileNeverTouched:
    def test_apply_returns_new_text_and_leaves_input_unchanged(self):
        """核心安全约束：应用 patch 绝不修改传入的原始文本。"""
        original = SOURCE
        new_code, _ = apply_patch_in_memory(original, SIMPLE_PATCH, "target.py")
        assert original == SOURCE
        assert new_code != SOURCE


@pytest.mark.skipif(not git_available(), reason="环境无 git")
class TestGitValidation:
    def test_valid_patch_passes_git_check(self):
        ok, output = validate_with_git(SOURCE, prepare_patch(SIMPLE_PATCH, "target.py"),
                                       "target.py")
        assert ok is True, output

    def test_wrong_context_fails_git_check(self):
        bad = FOREIGN_PATCH
        ok, output = validate_with_git(SOURCE, prepare_patch(bad, "target.py"), "target.py")
        assert ok is False
        assert output

    def test_verify_and_apply_rejects_what_git_rejects(self):
        bad = FOREIGN_PATCH
        with pytest.raises(PatchError):
            verify_and_apply(SOURCE, bad, "target.py", use_git=True)

    def test_verify_and_apply_reports_git_validation(self):
        _, info = verify_and_apply(SOURCE, SIMPLE_PATCH, "target.py", use_git=True)
        assert info["git_validated"] is True
        assert info["hunks_applied"] == 1

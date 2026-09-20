"""Patch 生成、校验与应用。

两条互不信任的路径，按可靠性排序：

1. **``git apply`` 子进程** —— 权威校验器。它能识别 LLM 生成的 patch 里绝大多数
   结构性错误（hunk 头计数不对、上下文不匹配、换行符问题）。
2. **进程内应用器** —— 当环境没有 git 时的降级路径，也用于把已校验通过的 patch
   真正落到内存中的代码文本上（不需要真的写文件）。

核心安全约束：**永远不修改用户的原始文件**。所有应用都在内存文本或临时副本上进行，
原始文件只在显式 ``--write`` 时才被写入。
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile

from core.errors import PatchError

# hunk 头形如 @@ -12,3 +12,5 @@ 或 @@ -12 +12 @@
HUNK_HEADER_RE = re.compile(
    r"^@@\s+-(\d+)(?:,(\d+))?\s+\+(\d+)(?:,(\d+))?\s+@@"
)

# patch 里必须出现的文件头标记，用于判断「这是一段 diff 而不是随便一段文本」。
DIFF_HEADER_MARKERS = ("--- ", "+++ ", "@@ ")

_GIT_AVAILABLE = None


def git_available():
    """探测 git 是否可用，结果缓存。没有 git 时自动走降级路径。"""
    global _GIT_AVAILABLE
    if _GIT_AVAILABLE is None:
        _GIT_AVAILABLE = shutil.which("git") is not None
    return _GIT_AVAILABLE


def looks_like_patch(text):
    """粗略判断文本是否包含 diff 结构。"""
    if not text or not text.strip():
        return False
    lines = text.split("\n")
    has_hunk = any(HUNK_HEADER_RE.match(line) for line in lines)
    has_marker = any(line.startswith(marker) for line in lines for marker in DIFF_HEADER_MARKERS)
    return has_hunk and has_marker


def strip_patch_fence(text):
    """剥掉 patch 外面可能被模型套上的 Markdown 围栏，并统一换行符。

    换行符统一是必须的：在 Windows 上如果 patch 里混入 CRLF，``git apply`` 会直接
    报 ``corrupt patch``，而不是给出「行尾风格不一致」这种可诊断的错误。
    同时要保证文本以换行结尾 —— 末行没有 ``\\n`` 会让 git 认为 hunk 行数对不上。
    """
    if not text:
        return ""
    cleaned = text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    if cleaned.startswith("```"):
        lines = cleaned.split("\n")
        lines = lines[1:]
        if lines and lines[-1].strip().startswith("```"):
            lines = lines[:-1]
        cleaned = "\n".join(lines)
    return cleaned.rstrip("\n") + "\n"


def normalize_patch_headers(patch, target_file):
    """把 patch 的文件头统一成目标文件名。

    模型常写出 ``a/fix.py`` / ``b/fix.py`` 或与实际路径不符的名字。因为我们的场景是
    「单文件 patch」，文件头本身不携带信息，统一即可，避免 git apply 因路径不匹配而拒绝。
    """
    lines = patch.split("\n")
    result = []
    for line in lines:
        if line.startswith("--- "):
            result.append("--- a/{name}".format(name=target_file))
        elif line.startswith("+++ "):
            result.append("+++ b/{name}".format(name=target_file))
        elif line.startswith("diff --git "):
            result.append(
                "diff --git a/{name} b/{name}".format(name=target_file)
            )
        else:
            result.append(line)
    joined = "\n".join(result)
    if not joined.endswith("\n"):
        joined += "\n"
    return joined


def _parse_hunks(patch):
    """把 patch 切成 hunk 列表，每个 hunk 记录起始行与逐行类型。

    **按 hunk 头声明的行数消费，而不是靠「遇到不认识的字符就停」**。后者会让解析器
    越过 hunk 边界吃掉下一个 diff 段的上下文行，导致 expected 行数比声明多，
    匹配必然失败。声明的计数在这里反而是可信的：``git apply --check`` 已经能识破
    计数造假的 patch，走到这一步说明计数是自洽的，用它来定界最稳。
    """
    hunks = []
    current = None
    lines = patch.split("\n")

    for line in lines:
        match = HUNK_HEADER_RE.match(line)
        if match:
            if current is not None:
                hunks.append(current)
            current = {
                "old_start": int(match.group(1)),
                "old_count": int(match.group(2)) if match.group(2) else 1,
                "new_start": int(match.group(3)),
                "new_count": int(match.group(4)) if match.group(4) else 1,
                "body": [],
                "old_seen": 0,
                "new_seen": 0,
            }
            continue

        if current is None:
            # 还没进入 hunk 体，忽略文件头等元信息。
            continue

        # 计数已满足，本 hunk 结束。
        if current["old_seen"] >= current["old_count"] and \
                current["new_seen"] >= current["new_count"]:
            hunks.append(current)
            current = None
            continue

        if line.startswith(("\\ No newline",)):
            continue
        if line.startswith(("diff --git", "--- ", "+++ ")):
            hunks.append(current)
            current = None
            continue

        # 空行要单独判：diff 里 context 空行写作单个空格，但若上游做过 rstrip
        # （很多工具链都会），它就退化成真正的空串，`''[:1]` 是 `''` 而非 ' '，
        # 会被误判成 hunk 结束而丢弃 —— 这会吃掉尾部 context 行导致匹配失败。
        prefix = line[:1] if line else " "
        if prefix not in (" ", "+", "-"):
            # 无法识别的行，视为 hunk 结束。
            hunks.append(current)
            current = None
            continue

        current["body"].append(line if line else " ")
        if prefix in (" ", "-"):
            current["old_seen"] += 1
        if prefix in (" ", "+"):
            current["new_seen"] += 1

    if current is not None:
        hunks.append(current)

    return hunks


def _hunk_expected_old_lines(hunk):
    """hunk 体里对应「原文」的行：上下文行 + 删除行。"""
    return [
        line[1:] if len(line) > 1 else ""
        for line in hunk["body"]
        if line[:1] in (" ", "-")
    ]


def _hunk_new_lines(hunk):
    """hunk 体里对应「结果」的行：上下文行 + 新增行。"""
    return [
        line[1:] if len(line) > 1 else ""
        for line in hunk["body"]
        if line[:1] in (" ", "+")
    ]


def apply_patch_in_memory(source, patch, target_file="target.py"):
    """在内存里应用 patch，返回 (新文本, 应用的 hunk 数)。

    定位策略：优先信任 hunk 头给的 ``old_start``；若不匹配，则在全文搜索
    ``_hunk_expected_old_lines`` 的实际位置。这种「先信任、后校正」的次序让
    patch 对模型算错行偏移的情况有一定容忍度，同时不会把改动落到错误的位置。
    """
    hunks = _parse_hunks(patch)
    if not hunks:
        raise PatchError("patch 中未找到任何 hunk", detail="no_hunks")

    source_lines = source.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    # 从后往前应用，避免前面的改动影响后面 hunk 的行号。
    ordered = sorted(hunks, key=lambda h: h["old_start"], reverse=True)

    applied = 0
    for hunk in ordered:
        expected = _hunk_expected_old_lines(hunk)
        if not expected:
            continue

        start_index = _locate_hunk(source_lines, hunk["old_start"], expected)
        if start_index is None:
            raise PatchError(
                "hunk 上下文在原文中找不到匹配：@@ -{old},{oc} +{new},{nc} @@".format(
                    old=hunk["old_start"], oc=hunk["old_count"],
                    new=hunk["new_start"], nc=hunk["new_count"],
                ),
                detail="hunk_context_mismatch",
            )

        replacement = _hunk_new_lines(hunk)
        source_lines[start_index:start_index + len(expected)] = replacement
        applied += 1

    if applied == 0:
        raise PatchError("patch 解析出了 hunk 但没有任何一处被应用", detail="nothing_applied")

    return "\n".join(source_lines), applied


def _locate_hunk(source_lines, old_start, expected):
    """找到 hunk 应当作用的位置（0-based 下标）。"""
    # 优先按 hunk 头声明的位置（diff 行号是 1-based）。
    candidate = old_start - 1
    if 0 <= candidate <= len(source_lines) - len(expected):
        if source_lines[candidate:candidate + len(expected)] == expected:
            return candidate

    # 声明位置不可靠时，全文搜索唯一匹配。
    matches = []
    limit = len(source_lines) - len(expected)
    for index in range(limit + 1):
        if source_lines[index:index + len(expected)] == expected:
            matches.append(index)

    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        # 多命中时取最接近声明位置的，语义上最可能是原作者意图。
        return min(matches, key=lambda index: abs(index - (old_start - 1)))
    return None


def validate_with_git(source, patch, target_file="target.py"):
    """用 ``git apply --check`` 做权威校验。

    在一个临时 git 仓库里放好原文件，再干跑。这样校验结果与真实应用完全一致，
    且不触碰用户目录。

    :returns: (是否通过, git 输出的错误信息)
    """
    if not git_available():
        return None, "git 不可用，跳过权威校验"

    temp_dir = tempfile.mkdtemp(prefix="codepilot-apply-")
    try:
        file_path = os.path.join(temp_dir, target_file)
        parent = os.path.dirname(file_path)
        if parent and not os.path.isdir(parent):
            os.makedirs(parent)

        with open(file_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(source.replace("\r\n", "\n"))

        patch_path = os.path.join(temp_dir, "change.patch")
        with open(patch_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(patch)

        env = dict(os.environ)
        env["GIT_CONFIG_NOSYSTEM"] = "1"

        # --check 只做干跑校验，不写入任何文件。
        process = subprocess.run(
            ["git", "apply", "--check", "--verbose", "--whitespace=nowarn", patch_path],
            cwd=temp_dir,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            env=env,
            timeout=30,
        )
        output = process.stdout.decode("utf-8", errors="replace").strip()
        return process.returncode == 0, output
    except (OSError, subprocess.SubprocessError) as exc:
        return None, "git 校验执行失败：{}".format(exc)
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def prepare_patch(raw_patch, target_file):
    """把模型给的原始 patch 规整到可应用状态。

    规整包含：剥围栏、统一文件头。**不**尝试修复 hunk 计数 —— 那是模型的问题，
    悄悄改掉会让 patch 的实际语义与模型意图不一致，宁可让校验失败。
    """
    if not raw_patch or not raw_patch.strip():
        raise PatchError("patch 为空", detail="empty_patch")

    cleaned = strip_patch_fence(raw_patch)
    if not looks_like_patch(cleaned):
        raise PatchError(
            "输出不是合法的 Git Diff（缺少 ---/+++/@@ 结构）",
            detail="not_a_patch_excerpt={!r}".format(cleaned[:200]),
        )

    return normalize_patch_headers(cleaned, target_file)


def verify_and_apply(source, raw_patch, target_file="target.py", use_git=True):
    """完整流程：规整 → git 权威校验 → 内存应用。

    :returns: (新文本, 应用信息 dict)
    :raises PatchError: 任一环节失败。
    """
    patch = prepare_patch(raw_patch, target_file)

    git_ok = None
    git_output = ""
    if use_git and git_available():
        git_ok, git_output = validate_with_git(source, patch, target_file)
        if git_ok is False:
            raise PatchError(
                "git apply --check 校验未通过：{}".format(git_output[:300]),
                detail="git_check_failed",
            )

    new_text, hunk_count = apply_patch_in_memory(source, patch, target_file)

    return new_text, {
        "patch": patch,
        "hunks_applied": hunk_count,
        "git_validated": bool(git_ok),
        "git_output": git_output,
    }

"""从样例源码派生 mock 补丁夹具。

手写 unified diff 的 ``@@`` 计数极易出错，而错误的计数会让 ``git apply`` 拒绝，
把「夹具写错」伪装成「工具链有问题」。这里用 ``difflib`` 从真实源码派生，
保证夹具一定可通过校验。

用法：
    python tools/build_mock_fixtures.py
"""
from __future__ import annotations

import difflib
import json
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SRC_REL = "fixtures/sample_project/user_service.py"
OUT_REL = "fixtures/mock_responses/submit_fixes.json"

# (issue_id, 原始片段, 替换后片段)
SPECS = [
    (
        "seed-1-hardcoded-credential",
        'DB_PASSWORD = "admin123456"',
        'DB_PASSWORD = os.environ["DB_PASSWORD"]',
    ),
    (
        "seed-2-sql-injection",
        '    query = "SELECT * FROM users WHERE name = \'" + username + "\'"\n'
        '    cursor.execute(query)',
        '    cursor.execute("SELECT * FROM users WHERE name = ?", (username,))',
    ),
    (
        "seed-3-none-deref",
        '    name = user["name"]',
        '    if user is None:\n        return ""\n    name = user["name"]',
    ),
    (
        "seed-4-nested-loop",
        "    for i in range(len(items)):\n"
        "        for j in range(len(items)):\n"
        "            total += items[i] * items[j]",
        "    for value in items:\n        total += value * sum(items)",
    ),
    (
        "seed-5-weak-hash",
        "    return hashlib.md5(password.encode()).hexdigest()",
        "    return hashlib.sha256(password.encode()).hexdigest()",
    ),
    (
        "seed-7-swallowed-exception",
        "    try:\n        return a / b\n    except Exception:\n        return None",
        "    try:\n        return a / b\n    except ZeroDivisionError:\n        return None",
    ),
]


def derive_patch(source, old_snippet, new_snippet, file_name):
    """在源码里做一次替换并产出完整 unified diff。

    关键细节：必须用 ``splitlines(keepends=True)`` 且不设 ``lineterm``，
    让每行自带行尾换行符。否则 difflib 产出的 diff 末行缺少 ``\\n``，
    git 会以 ``corrupt patch`` 报 hunk 行数与声明不符而拒绝 —— 这个坑很容易
    被误判成「工具链有问题」，实际是夹具生成方式不对。
    """
    if source.count(old_snippet) != 1:
        raise SystemExit(
            "锚点不是唯一的（出现 {} 次）：{!r}".format(
                source.count(old_snippet), old_snippet[:60]
            )
        )
    patched = source.replace(old_snippet, new_snippet)
    diff = list(
        difflib.unified_diff(
            source.splitlines(keepends=True),
            patched.splitlines(keepends=True),
            fromfile="a/{name}".format(name=file_name),
            tofile="b/{name}".format(name=file_name),
            n=3,
        )
    )
    return "".join(diff)


def main():
    src_path = os.path.join(ROOT, SRC_REL)
    with open(src_path, "r", encoding="utf-8") as handle:
        source = handle.read()

    file_name = os.path.basename(SRC_REL)
    fixes = []

    for issue_id, old_snippet, new_snippet in SPECS:
        fixes.append(
            {
                "issue_id": issue_id,
                "requires_cross_file": False,
                "patch": derive_patch(source, old_snippet, new_snippet, file_name),
                "explanation": "针对 {} 的最小变更。".format(issue_id),
            }
        )

    # 重命名会波及调用方，必须显式标记跨文件而非猜测。
    fixes.append(
        {
            "issue_id": "seed-6-naming",
            "requires_cross_file": True,
            "patch": "",
            "explanation": (
                "ReadConfig 可能被其他模块导入调用，重命名需同步修改调用处，交由人工处理。"
            ),
        }
    )

    out_path = os.path.join(ROOT, OUT_REL)
    with open(out_path, "w", encoding="utf-8") as handle:
        json.dump([{"fixes": fixes}], handle, ensure_ascii=False, indent=2)

    print("已写入 {} 个修复方案 -> {}".format(len(fixes), out_path))


if __name__ == "__main__":
    main()

"""报告渲染：分层展示。

需求要求的分层：**高危完整展开 → 中低危表格 → 未通过项单列（阻塞合并）**。
渲染层不做任何判断，只消费 ``ReviewReport`` 里已经算好的状态 —— 这样
「什么算高危」「什么算阻塞」只有一处定义（``core.schemas``），不会在 HTML、
Markdown、CLI 三个渲染路径里各写一遍然后走偏。
"""
from __future__ import annotations

from core.schemas import FixStatus, MatchStatus, RunStatus

# PR 评论有长度上限，超出会被 GitHub 截断。超过则把中低危表格折叠。
MAX_COMMENT_ISSUES = 50


def _escape_cell(text):
    """把文本压成适合放进 Markdown 表格单元格的单行。"""
    if not text:
        return ""
    return str(text).replace("|", "\\|").replace("\n", " ").strip()


def _escape_code(text, limit=200):
    """把代码片段安全地放进反引号里（片段内部的反引号需要换掉）。"""
    if not text:
        return ""
    snippet = str(text).replace("`", "'")
    if len(snippet) > limit:
        snippet = snippet[:limit] + " …"
    return snippet


def _status_label(report):
    if report.status == RunStatus.PASSED:
        return "✅ **通过**"
    return "⛔ **阻塞合并**"


def _priority_badge(priority):
    return {
        "P0": "🔴 P0",
        "P1": "🟠 P1",
        "P2": "🟡 P2",
        "P3": "🔵 P3",
    }.get(priority.value if hasattr(priority, "value") else str(priority), str(priority))


def _anchor_status(located, issue_id):
    if located is None:
        return "未定位"
    if not located.is_resolved:
        return "⚠️ 未定位"
    label = {
        MatchStatus.EXACT: "精确",
        MatchStatus.NORMALIZED: "归一化",
        MatchStatus.FUZZY: "模糊",
    }.get(located.match_status, located.match_status.value)
    return "{} ({:.0%})".format(located.describe_location(), located.confidence) + \
        " · {}".format(label)


def _fix_status_label(attempt):
    return {
        FixStatus.VERIFIED: "✅ 已验证修复",
        FixStatus.APPLIED: "🔧 已应用",
        FixStatus.REGRESSED: "⚠️ 回归失败",
        FixStatus.ESCALATED: "⛔ 重试耗尽",
        FixStatus.SKIPPED_CROSS_FILE: "↗️ 需人工处理",
        FixStatus.PATCH_FAILED: "❌ 补丁无法应用",
        FixStatus.PENDING: "⏳ 待处理",
    }.get(attempt.status, attempt.status.value)


def render_markdown(report, detail="full"):
    """渲染完整 Markdown 报告。

    :param detail: ``full`` 输出全部区块；``comment`` 为 PR 评论场景，
        省略原始 JSON 附录以控制长度。
    """
    located_by_id = report.located_by_id()
    attempt_by_id = {item.issue_id: item for item in report.attempts}
    lines = []

    lines.append("## 🔍 CodePilot Agent 代码审查报告")
    lines.append("")
    lines.append("| 项目 | 值 |")
    lines.append("| --- | --- |")
    lines.append("| 目标文件 | `{}` |".format(_escape_cell(report.target_file)))
    lines.append("| 结论 | {} |".format(_status_label(report)))
    lines.append("| 问题总数 | {} |".format(len(report.issues)))
    lines.append("| 其中高危 (P0/P1) | {} |".format(len(report.blocking_issues)))
    lines.append("| 已应用补丁 | {} |".format(report.applied_patch_count))
    lines.append("| 未定位 | {} |".format(len(report.unresolved_issue_ids)))
    lines.append("| 重试耗尽 | {} |".format(len(report.escalated_issue_ids)))
    lines.append("| 需人工跨文件处理 | {} |".format(len(report.cross_file_issue_ids)))
    lines.append("| LLM 调用次数 | {} |".format(report.llm_calls))
    lines.append("")

    if not report.issues:
        lines.append("✅ 本次审查未发现问题。")
        lines.append("")
        return "\n".join(lines)

    # ---- 第一层：高危完整展开 ----
    blocking = report.blocking_issues
    lines.append("### ⛔ 高危问题（完整展开，阻塞合并）")
    lines.append("")
    if not blocking:
        lines.append("_无高危问题。_")
        lines.append("")
    for index, issue in enumerate(blocking, 1):
        located = located_by_id.get(issue.issue_id)
        attempt = attempt_by_id.get(issue.issue_id)
        lines.append("#### {idx}. {prio} · {cat} · {sev}".format(
            idx=index, prio=_priority_badge(issue.priority),
            cat=issue.category.value, sev=issue.severity.value,
        ))
        lines.append("")
        lines.append("- **位置**：`{}` {}".format(
            _escape_cell(located.file_path if located else report.target_file),
            _anchor_status(located, issue.issue_id),
        ))
        lines.append("- **问题**：{}".format(issue.description))
        if issue.suggestion:
            lines.append("- **建议**：{}".format(issue.suggestion))
        lines.append("- **代码锚点**：")
        lines.append("")
        lines.append("  ```python")
        for code_line in (issue.code_anchor or "").split("\n"):
            lines.append("  " + code_line)
        lines.append("  ```")
        lines.append("")
        if attempt is not None:
            lines.append("- **修复状态**：{}（尝试 {}/{}）".format(
                _fix_status_label(attempt), attempt.attempts, attempt.max_attempts
            ))
            if attempt.failure_reason:
                lines.append("- **失败原因**：{}".format(attempt.failure_reason))
            lines.append("")

        fix = next((f for f in report.fixes if f.issue_id == issue.issue_id), None)
        if fix is not None and fix.patch.strip():
            lines.append("  <details><summary>查看补丁</summary>")
            lines.append("")
            lines.append("  ```diff")
            for patch_line in fix.patch.rstrip("\n").split("\n"):
                lines.append("  " + patch_line)
            lines.append("  ```")
            lines.append("")
            lines.append("  </details>")
            lines.append("")
        elif fix is not None and fix.explanation:
            lines.append("  > {}".format(fix.explanation))
            lines.append("")

    # ---- 第二层：中低危表格 ----
    non_blocking = report.non_blocking_issues
    lines.append("### 📋 中低危问题")
    lines.append("")
    if not non_blocking:
        lines.append("_无中低危问题。_")
        lines.append("")
    else:
        shown = non_blocking[:MAX_COMMENT_ISSUES]
        lines.append("| 优先级 | 分类 | 严重度 | 位置 | 问题 | 代码锚点 | 修复状态 |")
        lines.append("| --- | --- | --- | --- | --- | --- | --- |")
        for issue in shown:
            located = located_by_id.get(issue.issue_id)
            attempt = attempt_by_id.get(issue.issue_id)
            lines.append("| {} | {} | {} | {} | {} | `{}` | {} |".format(
                _priority_badge(issue.priority),
                issue.category.value,
                issue.severity.value,
                _escape_cell(_anchor_status(located, issue.issue_id)),
                _escape_cell(issue.description[:160]),
                _escape_cell(_escape_code(issue.code_anchor, 60)),
                _fix_status_label(attempt) if attempt else "—",
            ))
        if len(non_blocking) > len(shown):
            lines.append("")
            lines.append("_另有 {} 条中低危问题未在表格中展开，见原始 JSON 报告。_".format(
                len(non_blocking) - len(shown)
            ))
        lines.append("")

    # ---- 第三层：未通过项单列 ----
    failed = _collect_failed(report, located_by_id, attempt_by_id)
    lines.append("### 🚧 未通过项（需要人工介入）")
    lines.append("")
    if not failed:
        lines.append("_无未通过项。_")
        lines.append("")
    else:
        for entry in failed:
            lines.append("- **{}** `{}` — {}".format(
                entry["label"], entry["issue_id"], entry["reason"]
            ))
            if entry.get("blocking"):
                lines.append("  - ⛔ 该问题为高危，**阻塞合并**")
        lines.append("")

    if detail == "full":
        lines.append("---")
        lines.append("")
        lines.append("<details><summary>原始 JSON 报告</summary>")
        lines.append("")
        lines.append("```json")
        lines.append(report.json(indent=2, ensure_ascii=False))
        lines.append("```")
        lines.append("")
        lines.append("</details>")
        lines.append("")

    return "\n".join(lines)


def _collect_failed(report, located_by_id, attempt_by_id):
    """汇总所有「未通过」的情形，每项附上阻塞标记。

    四类：未定位 / 重试耗尽 / 需跨文件人工处理 / 补丁无法应用。
    """
    issue_by_id = report.issue_by_id()
    entries = []

    for issue_id in report.unresolved_issue_ids:
        issue = issue_by_id.get(issue_id)
        entries.append({
            "label": "未定位",
            "issue_id": issue_id,
            "reason": "锚点在源码中匹配失败，行号未知，未生成变更",
            "blocking": bool(issue is not None and issue.is_blocking),
        })

    for attempt in report.attempts:
        issue = issue_by_id.get(attempt.issue_id)
        if attempt.status == FixStatus.ESCALATED:
            entries.append({
                "label": "重试耗尽",
                "issue_id": attempt.issue_id,
                "reason": "已达重试上限（{} 次）：{}".format(
                    attempt.max_attempts, attempt.failure_reason or "原问题依然存在"
                ),
                "blocking": bool(issue is not None and issue.is_blocking),
            })
        elif attempt.status == FixStatus.SKIPPED_CROSS_FILE:
            entries.append({
                "label": "需人工跨文件处理",
                "issue_id": attempt.issue_id,
                "reason": attempt.failure_reason or "修复涉及跨文件改动",
                "blocking": False,
            })
        elif attempt.status == FixStatus.PATCH_FAILED:
            entries.append({
                "label": "补丁无法应用",
                "issue_id": attempt.issue_id,
                "reason": attempt.failure_reason or "patch 校验失败",
                "blocking": bool(issue is not None and issue.is_blocking),
            })

    return entries


def render_cli_summary(report):
    """终端里的一屏摘要。返回字符串列表，由 CLI 决定着色。"""
    lines = []
    lines.append("目标文件 : {}".format(report.target_file))
    lines.append("结论     : {}".format(report.status.value))
    lines.append("问题总数 : {}  (高危 {} / 中低危 {})".format(
        len(report.issues), len(report.blocking_issues), len(report.non_blocking_issues)
    ))
    lines.append("已应用补丁: {}".format(report.applied_patch_count))
    lines.append("未定位   : {}".format(len(report.unresolved_issue_ids)))
    lines.append("重试耗尽 : {}".format(len(report.escalated_issue_ids)))
    lines.append("跨文件   : {}".format(len(report.cross_file_issue_ids)))
    lines.append("LLM 调用 : {}".format(report.llm_calls))
    return lines


def render_json(report):
    """原始 JSON 报告，供 Artifact 归档。"""
    return report.json(indent=2, ensure_ascii=False)

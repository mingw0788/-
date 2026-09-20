"""三个 Agent 的统一封装层。

每个 Agent 的职责固定为：渲染 Prompt → 调 LLM → 按 Pydantic 契约校验 → 返回结构化对象。
校验失败会抛出 ``SchemaValidationError``，由状态机决定是否重试 —— Agent 自身不做重试，
避免重试逻辑散落在多处。
"""
from __future__ import annotations

import json
import uuid

from agents.prompt_loader import load_and_render
from core.errors import LLMResponseError
from core.schemas import (
    Fix,
    FixList,
    Issue,
    IssueList,
    LocatedIssue,
    LocatedIssueList,
    ProjectContext,
)


def _to_json_block(value):
    """把数据渲染成 Prompt 里的 JSON 文本，中文不转义、缩进可读。"""
    if isinstance(value, str):
        return value
    if hasattr(value, "dict"):
        value = value.dict()
    return json.dumps(value, ensure_ascii=False, indent=2)


def _new_issue_id():
    return str(uuid.uuid4())


def _backfill_issue_ids(payload):
    """给缺失 issue_id 的条目补 UUID。

    模型偶发漏掉 ``issue_id``。这里补齐而不是报错，因为缺少 ID 不影响语义，
    但缺少锚点/分类会影响 —— 那些仍交给 Pydantic 拒绝。
    """
    issues = payload.get("issues")
    if not isinstance(issues, list):
        return payload
    for entry in issues:
        if isinstance(entry, dict) and not entry.get("issue_id"):
            entry["issue_id"] = _new_issue_id()
    return payload


def _ensure_unique_ids(issues):
    """保证 issue_id 在单次运行内唯一。

    模型有时会给多条问题复用同一个 ID（尤其复述同一模式时），这会让「按 ID 关联
    定位结果」「按 ID 计数重试」全部错位，所以必须在这里拆开。
    """
    seen = set()
    for issue in issues:
        if issue.issue_id in seen:
            issue.issue_id = _new_issue_id()
        seen.add(issue.issue_id)
    return issues


class ReviewerAgent(object):
    """Agent 1：审查。输出带代码锚点的结构化问题列表。"""

    TOOL_NAME = "submit_review"

    def __init__(self, client):
        self.client = client

    def run(self, code_chunk, project_context, file_path=""):
        from llm.tolerant_json import coerce_issues_payload, parse_json_response

        if not isinstance(project_context, ProjectContext):
            project_context = ProjectContext(**project_context)

        system = (
            "你是一位资深的安全专家和代码架构师，负责对代码进行多维度深度审查。"
            "你必须只报告真实存在的问题，并严格按要求的 JSON 结构输出。"
        )
        user = load_and_render(
            "reviewer",
            {
                "project_name": project_context.project_name,
                "project_context": _to_json_block(project_context),
                "internal_coding_standards": _to_json_block(
                    project_context.internal_coding_standards
                ),
                "dependency_versions": _to_json_block(project_context.dependency_versions),
                "related_files": _to_json_block(project_context.related_files),
                "file_path": file_path,
                "code_chunk": code_chunk,
            },
        )

        raw = self.client.complete_json(
            system=system,
            user=user,
            tool_name=self.TOOL_NAME,
            tool_description="提交代码审查结果：结构化的问题列表。",
            schema_model=IssueList,
            step="reviewer",
        )

        payload = coerce_issues_payload(raw) if not isinstance(raw, IssueList) else raw
        payload = _backfill_issue_ids(payload)

        try:
            result = IssueList(**payload)
        except Exception as exc:
            raise LLMResponseError(
                "审查 Agent 输出不满足契约：{}".format(exc),
                raw_text=json.dumps(payload, ensure_ascii=False)[:500],
                detail=str(exc),
            )

        for issue in result.issues:
            if not issue.file_path:
                issue.file_path = file_path
        result.issues = _ensure_unique_ids(result.issues)
        return result.issues


class LocatorAgent(object):
    """Agent 2：定位。

    注意职责边界：**确定性匹配是主路径**（``core.anchors``），本 Agent 只在
    三层匹配全部失败时作为兜底消歧。所以 ``run`` 接受的是 unresolved 子集，
    而不是全部问题 —— 这与需求文档里「把全部问题交给 LLM 定位」的描述不同，
    原因是那样会重新引入行号幻觉。
    """

    TOOL_NAME = "submit_locations"

    def __init__(self, client):
        self.client = client

    def run(self, original_code, unresolved_issues, file_path="", located_issues=None):
        from llm.tolerant_json import coerce_list_payload

        if not unresolved_issues:
            return []

        if located_issues is None:
            located_issues = []

        issue_payload = []
        for issue in unresolved_issues:
            issue_payload.append(
                {
                    "issue_id": issue.issue_id,
                    "category": issue.category.value,
                    "code_anchor": issue.code_anchor,
                    "description": issue.description,
                    "suggestion": issue.suggestion,
                }
            )

        system = (
            "你是一个精确的代码定位助手。当锚点匹配失败时，你的职责是判断问题代码"
            "是否真实存在于给定代码中。找不到时必须返回 null 行号，绝不能编造。"
        )
        user = load_and_render(
            "locator",
            {
                "file_path": file_path,
                "original_code": original_code,
                "unresolved_issues": _to_json_block(issue_payload),
            },
        )

        raw = self.client.complete_json(
            system=system,
            user=user,
            tool_name=self.TOOL_NAME,
            tool_description="提交兜底定位结果。找不到位置时必须返回 null 行号。",
            schema_model=LocatedIssueList,
            step="locator",
        )

        payload = coerce_list_payload(raw, "located_issues", "issue_id")
        try:
            result = LocatedIssueList(**payload)
        except Exception as exc:
            raise LLMResponseError(
                "定位 Agent 输出不满足契约：{}".format(exc),
                raw_text=json.dumps(payload, ensure_ascii=False)[:500],
                detail=str(exc),
            )

        if file_path:
            for item in result.located_issues:
                if not item.file_path:
                    item.file_path = file_path

        return result.located_issues


class FixerAgent(object):
    """Agent 3：修复。产出可验证的 Git Patch。"""

    TOOL_NAME = "submit_fixes"

    def __init__(self, client):
        self.client = client

    def run(self, original_code, located_issues, project_context, file_path=""):
        from llm.tolerant_json import coerce_list_payload

        if not located_issues:
            return []

        if not isinstance(project_context, ProjectContext):
            project_context = ProjectContext(**project_context)

        payload_in = []
        for item in located_issues:
            if hasattr(item, "dict"):
                payload_in.append(item.dict())
            else:
                payload_in.append(item)
        for entry in payload_in:
            entry["file_path"] = entry.get("file_path") or file_path

        system = (
            "你是一位高级重构工程师，负责生成最小、可验证的 Git Patch。"
            "禁止输出完整替换代码。涉及跨文件改动时必须显式标记而非猜测。"
        )
        user = load_and_render(
            "fixer",
            {
                "project_name": project_context.project_name,
                "project_context": _to_json_block(project_context),
                "internal_coding_standards": _to_json_block(
                    project_context.internal_coding_standards
                ),
                "dependency_versions": _to_json_block(project_context.dependency_versions),
                "original_code": original_code,
                "located_issues": _to_json_block(payload_in),
            },
        )

        raw = self.client.complete_json(
            system=system,
            user=user,
            tool_name=self.TOOL_NAME,
            tool_description="提交修复补丁：每个问题一个标准 Git Diff。",
            schema_model=FixList,
            step="fixer",
        )

        payload = coerce_list_payload(raw, "fixes", "issue_id")
        try:
            result = FixList(**payload)
        except Exception as exc:
            raise LLMResponseError(
                "修复 Agent 输出不满足契约：{}".format(exc),
                raw_text=json.dumps(payload, ensure_ascii=False)[:500],
                detail=str(exc),
            )

        return result.fixes

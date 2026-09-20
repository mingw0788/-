"""CodePilot Agent 异常体系。

所有异常都携带足够的上下文供 CLI 渲染与状态机决策，避免调用方只能靠字符串匹配判断类型。
"""
from __future__ import annotations


class CodePilotError(Exception):
    """所有 CodePilot 异常的基类。"""

    def __init__(self, message, *, detail=None, retryable=False):
        super().__init__(message)
        self.message = message
        self.detail = detail
        # 状态机据此决定当前步骤是重试还是直接降级到 ESCALATED。
        self.retryable = retryable


class ConfigError(CodePilotError):
    """配置文件缺失或非法。"""


class LLMError(CodePilotError):
    """LLM 调用层故障（网络、限流、鉴权）。"""


class LLMResponseError(CodePilotError):
    """LLM 返回了内容，但内容无法被解析为预期结构。"""

    def __init__(self, message, *, raw_text=None, detail=None, retryable=True):
        super().__init__(message, detail=detail, retryable=retryable)
        # 保留原文，便于写出可复现的失败报告，也便于补进 fixtures。
        self.raw_text = raw_text


class SchemaValidationError(LLMResponseError):
    """JSON 能解析，但不满足 Pydantic 契约。"""


class PatchError(CodePilotError):
    """Patch 无法生成、无法通过校验或无法应用。"""

    def __init__(self, message, *, issue_id=None, detail=None, retryable=False):
        super().__init__(message, detail=detail, retryable=retryable)
        self.issue_id = issue_id


class AnchorMatchError(CodePilotError):
    """锚点匹配过程中出现的非预期错误（匹配失败本身不算错误，走 match_status=unresolved）。"""


class BudgetExceededError(CodePilotError):
    """LLM 调用次数或 token 预算耗尽，用于防止闭环无限膨胀。"""


class StateError(CodePilotError):
    """状态机状态非法或状态快照损坏。"""

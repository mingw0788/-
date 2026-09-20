"""LLM 客户端层：真实调用与 Mock 双模式。

为什么用 tool use 而不是「在 Prompt 里求模型别加 ```json 标记」：

需求文档里的做法是把格式约束写进 Prompt 祈祷模型遵守，然后靠容错解析兜底。
更可靠的做法是把 schema 交给 API 的 tool use 机制强制约束 —— 格式由协议层保证，
容错解析退居为意外情况的第二道防线，而不是主路径。两者都留着，但主次调换了。

Mock 模式让整条流水线（含回归闭环）可以离线、可复现、零 token 跑通，
这也是 CI 里唯一能跑的模式。
"""
from __future__ import annotations

import json
import os

from core.errors import ConfigError, LLMError, SchemaValidationError
from llm.tolerant_json import parse_json_response

DEFAULT_MODEL = "claude-sonnet-5"
DEFAULT_MAX_TOKENS = 8192

# 重试次数（不含首次）。仅对可重试错误生效。
DEFAULT_RETRIES = 2


def pydantic_to_tool_schema(model, name, description):
    """把 Pydantic 模型转成 tool use 的 input_schema。

    只做必要的递归展开：``$defs`` 内联、``title`` 剥除、枚举转 ``enum``。
    不追求完整 JSON Schema 覆盖 —— tool use 只需要描述清结构，合法性仍由 Pydantic 兜底。
    """
    schema = model.schema()
    defs = schema.get("$defs", schema.get("definitions", {}))

    def deref(node, depth=0):
        if depth > 20:
            return node
        if not isinstance(node, dict):
            return node
        if "$ref" in node:
            ref_name = node["$ref"].rsplit("/", 1)[-1]
            target = defs.get(ref_name)
            if target is None:
                return {"type": "object"}
            merged = {key: value for key, value in node.items() if key != "$ref"}
            resolved = deref(target, depth + 1)
            resolved.update(merged)
            return resolved

        result = {}
        for key, value in node.items():
            if key in ("title", "$defs", "definitions"):
                continue
            if key == "anyOf":
                # Optional[X] 会被展开成 anyOf[X, null]，取非 null 的那支。
                non_null = [deref(v, depth + 1) for v in value
                            if not (isinstance(v, dict) and v.get("type") == "null")]
                if len(non_null) == 1:
                    resolved = non_null[0]
                    # 保留外层默认值信息。
                    result.update(resolved)
                    continue
                result[key] = [deref(v, depth + 1) for v in value]
                continue
            if key == "properties":
                result[key] = {k: deref(v, depth + 1) for k, v in value.items()}
                continue
            if key == "items":
                result[key] = deref(value, depth + 1)
                continue
            result[key] = value
        return result

    resolved = deref(schema)
    return {
        "name": name,
        "description": description,
        "input_schema": {
            "type": "object",
            "properties": resolved.get("properties", {}),
            "required": resolved.get("required", []),
        },
    }


class UsageRecord(object):
    """一次调用的用量记录，用于报告里的可观测性。"""

    __slots__ = ("step", "input_tokens", "output_tokens", "model", "ok", "error")

    def __init__(self, step, input_tokens=0, output_tokens=0, model="", ok=True, error=""):
        self.step = step
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.model = model
        self.ok = ok
        self.error = error


class BaseLLMClient(object):
    """客户端接口。Agent 只依赖 ``complete_json`` 这一个方法。"""

    def __init__(self):
        self.calls = []
        self.usage = []

    @property
    def call_count(self):
        return len(self.calls)

    @property
    def total_input_tokens(self):
        return sum(record.input_tokens for record in self.usage)

    @property
    def total_output_tokens(self):
        return sum(record.output_tokens for record in self.usage)

    def complete_json(self, *, system, user, tool_name, tool_description, schema_model, step=""):
        """返回 dict；调用方随后用 Pydantic 校验。"""
        raise NotImplementedError

    def _record(self, step, payload, input_tokens=0, output_tokens=0, ok=True, error=""):
        self.calls.append({"step": step, "payload": payload})
        self.usage.append(UsageRecord(step, input_tokens, output_tokens, "", ok, error))


class MockLLMClient(BaseLLMClient):
    """离线 Mock：从 fixtures 目录读预置响应。

    契约：``<fixture_dir>/<tool_name>.json`` 是**列表**，按调用次序取；
    取完后重复最后一项 —— 这样「同一问题反复修不好」的回归场景可以只写少量响应
    就构造出来，测试不必把重试次数次的内容全列一遍。

    还支持 ``{"__sequence__": [...]}`` 形式，用 ``__repeat__`` 声明前 N 项是一次性的。
    这是构造「重试后终于修好」这类场景所必需的：重试次数取决于优先级配额
    （P0/P1 三次、P2 两次、P3 一次），不给逐次控制就只能测到「一直失败」。
    """

    def __init__(self, fixture_dir, strict=True):
        super().__init__()
        if not os.path.isdir(fixture_dir):
            raise ConfigError("Mock fixture 目录不存在：{}".format(fixture_dir))
        self.fixture_dir = fixture_dir
        self.strict = strict
        self._cursors = {}
        self._cache = {}

    def _load(self, tool_name):
        if tool_name in self._cache:
            return self._cache[tool_name]

        path = os.path.join(self.fixture_dir, "{}.json".format(tool_name))
        if not os.path.isfile(path):
            if self.strict:
                raise ConfigError(
                    "Mock 缺少 fixture：{}（Mock 模式下每个 tool_name 都需要对应文件）".format(path)
                )
            self._cache[tool_name] = []
            return []

        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)

        entries = self._normalize(payload, path)
        self._cache[tool_name] = entries
        return entries

    @staticmethod
    def _normalize(payload, path):
        """把 fixture 规整成「有限序列 + 无限尾项」。"""
        if isinstance(payload, list):
            if payload and isinstance(payload[0], dict) and "__sequence__" in payload[0]:
                payload = payload[0]["__sequence__"]
                repeat = 0
            else:
                # 纯列表：最后一项作为无限重复的尾项。
                repeat = 1
        elif isinstance(payload, dict):
            if "__sequence__" in payload:
                repeat = int(payload.get("__repeat__", 0))
                payload = payload["__sequence__"]
            else:
                payload = [payload]
                repeat = 1
        else:
            raise ConfigError("Mock fixture 格式非法：{}".format(path))

        if not isinstance(payload, list) or not payload:
            raise ConfigError("Mock fixture 为空：{}".format(path))

        repeat = max(0, min(repeat, len(payload)))
        finite = payload[:len(payload) - repeat] if repeat else payload
        tail = payload[len(payload) - repeat:] if repeat else []
        return {"finite": finite, "tail": tail}

    def complete_json(self, *, system, user, tool_name, tool_description, schema_model, step=""):
        bundle = self._load(tool_name)
        finite = bundle["finite"]
        tail = bundle["tail"]
        if not finite and not tail:
            raise SchemaValidationError(
                "Mock 没有可用于 {!r} 的响应".format(tool_name), detail="empty_fixture"
            )

        index = self._cursors.get(tool_name, 0)
        if index < len(finite):
            entry = finite[index]
        elif tail:
            # 有限序列耗尽后，在尾项里循环 —— 表达「情况没有变化」。
            entry = tail[(index - len(finite)) % len(tail)]
        else:
            entry = finite[-1]

        self._cursors[tool_name] = index + 1
        self._record(step or tool_name, entry)
        return entry

    def reset(self):
        """重置调用游标，供同一个测试内多次运行复用。"""
        self._cursors = {}

    def remaining(self, tool_name):
        """还能返回多少个「一次性」响应（不含尾项）。"""
        bundle = self._load(tool_name)
        return max(0, len(bundle["finite"]) - self._cursors.get(tool_name, 0))


class AnthropicLLMClient(BaseLLMClient):
    """真实调用，走 Anthropic SDK 的 tool use 强制结构化输出。"""

    def __init__(self, model=DEFAULT_MODEL, api_key=None, max_tokens=DEFAULT_MAX_TOKENS,
                 retries=DEFAULT_RETRIES, temperature=0.0):
        super().__init__()
        try:
            import anthropic
        except ImportError as exc:  # pragma: no cover - 环境相关
            raise ConfigError(
                "未安装 anthropic SDK。请执行 pip install anthropic，或改用 --mock 模式。"
            ) from exc

        resolved_key = api_key or os.environ.get("ANTHROPIC_API_KEY")
        if not resolved_key:
            raise ConfigError(
                "缺少 ANTHROPIC_API_KEY。请在环境变量中设置，或改用 --mock 模式离线运行。"
            )

        self._anthropic = anthropic
        self.client = anthropic.Anthropic(api_key=resolved_key)
        self.model = model
        self.max_tokens = max_tokens
        self.retries = retries
        self.temperature = temperature

    def complete_json(self, *, system, user, tool_name, tool_description, schema_model, step=""):
        tool = pydantic_to_tool_schema(schema_model, tool_name, tool_description)
        last_error = None

        for attempt in range(self.retries + 1):
            try:
                response = self.client.messages.create(
                    model=self.model,
                    max_tokens=self.max_tokens,
                    temperature=self.temperature,
                    system=system,
                    messages=[{"role": "user", "content": user}],
                    tools=[tool],
                    # 强制走工具调用，把格式约束交给协议层而不是 Prompt。
                    tool_choice={"type": "tool", "name": tool_name},
                )
                payload, input_tokens, output_tokens = self._extract(response, tool_name, step)
                self._record(step or tool_name, payload, input_tokens, output_tokens)
                return payload
            except self._anthropic.APIError as exc:  # pragma: no cover - 需要网络
                last_error = exc
                self._record(step or tool_name, None, ok=False, error=str(exc))
                if attempt >= self.retries:
                    raise LLMError(
                        "调用 Anthropic API 失败（已重试 {n} 次）：{err}".format(
                            n=self.retries, err=exc
                        ),
                        detail=str(exc),
                        retryable=True,
                    )
            except SchemaValidationError as exc:
                # 结构问题重试有意义：换一次采样可能就正常了。
                last_error = exc
                self._record(step or tool_name, None, ok=False, error=str(exc))
                if attempt >= self.retries:
                    raise

        raise LLMError("调用失败且无可返回结果：{}".format(last_error), retryable=True)

    def _extract(self, response, tool_name, step):
        """从响应里取工具调用入参。

        正常情况下 tool_use 块直接给结构化入参，不需要容错解析；
        但若模型把 JSON 塞进了 text 块（例如工具被拒），退回容错解析路径。
        """
        input_tokens = getattr(getattr(response, "usage", None), "input_tokens", 0) or 0
        output_tokens = getattr(getattr(response, "usage", None), "output_tokens", 0) or 0

        text_chunks = []
        for block in getattr(response, "content", []) or []:
            block_type = getattr(block, "type", None)
            if block_type == "tool_use" and getattr(block, "name", None) == tool_name:
                payload = getattr(block, "input", None)
                if isinstance(payload, dict):
                    return payload, input_tokens, output_tokens
            elif block_type == "text":
                text_chunks.append(getattr(block, "text", "") or "")

        joined = "\n".join(text_chunks).strip()
        if not joined:
            raise SchemaValidationError(
                "响应中既没有 {} 的 tool_use，也没有可解析的文本".format(tool_name),
                detail="no_tool_use",
            )
        return parse_json_response(joined, context=tool_name), input_tokens, output_tokens


def build_client(mock=False, fixture_dir=None, model=DEFAULT_MODEL, api_key=None,
                 max_tokens=DEFAULT_MAX_TOKENS):
    """按运行模式构造客户端。mock 优先于环境变量，便于测试强制离线。"""
    if mock:
        if not fixture_dir:
            raise ConfigError("Mock 模式必须提供 fixture_dir")
        return MockLLMClient(fixture_dir)
    return AnthropicLLMClient(model=model, api_key=api_key, max_tokens=max_tokens)

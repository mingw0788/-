"""Prompt 模板加载与占位符替换。

用 ``{{var}}`` 占位 + 自定义 ``render``，而不是 ``str.format`` / f-string：
Prompt 正文里含大量 JSON 花括号，``format`` 会把它们当占位符抛 KeyError，
f-string 更会让代码片段里的引号直接炸掉模板。自定义替换只认 ``{{已知变量}}``，
其余花括号原样保留。
"""
from __future__ import annotations

import os
import re

from core.errors import ConfigError

PROMPTS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "prompts")

_PLACEHOLDER_RE = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")

_cache = {}


def load_prompt(name, prompts_dir=None):
    """从 ``prompts/<name>.md`` 读取模板，带进程内缓存。"""
    directory = prompts_dir or PROMPTS_DIR
    path = os.path.join(directory, "{}.md".format(name))
    if not os.path.isfile(path):
        raise ConfigError("Prompt 模板不存在：{}".format(path))

    if path not in _cache:
        with open(path, "r", encoding="utf-8") as handle:
            _cache[path] = handle.read()
    return _cache[path]


def render(template, variables):
    """替换 ``{{var}}``。

    未提供的变量保持原样而非清空 —— 让缺失的注入在输出里肉眼可见，
    比静默变成空字符串更容易发现接线错误。
    """

    def replace(match):
        key = match.group(1)
        if key not in variables:
            return match.group(0)
        value = variables[key]
        if value is None:
            return ""
        if not isinstance(value, str):
            value = str(value)
        return value

    return _PLACEHOLDER_RE.sub(replace, template)


def load_and_render(name, variables, prompts_dir=None):
    return render(load_prompt(name, prompts_dir), variables)


def clear_cache():
    """清空模板缓存。测试里改模板后需要调用。"""
    _cache.clear()

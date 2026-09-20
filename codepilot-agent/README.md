# CodePilot Agent

多 Agent 协作的 AI 代码审查助手。三个 Agent 分工：**审查**找出问题、**定位**把问题钉到确定的行、**修复**产出可验证的 Git Patch。修复后强制重新审查，形成闭环。

## 它解决什么问题

常见的「把文件丢给大模型问有什么问题」有三个硬伤，这个项目针对性地处理：

| 硬伤 | 表现 | 这里的做法 |
| --- | --- | --- |
| **编造行号** | 模型报告「第 87 行有 SQL 注入」，实际那行是空行 | 模型只输出**代码锚点**（原文片段），行号由确定性匹配派生；匹配不上就显式标 `unresolved`，绝不猜 |
| **改坏别的代码** | 模型直接吐一整份替换代码，顺手动了几处没提的地方 | 只接受 **Git Patch**，`git apply --check` 权威校验后才应用；且只应用于**内存副本**，不碰你的文件 |
| **修完不验证** | 补丁消除了 A 问题，同时引入了 B 问题，报告还算通过 | 修复后**强制重新审查**，同时比对「原问题是否消失」与「是否引入新问题」，后者单独记录 |

## 快速开始

```bash
pip install -r requirements.txt

# 离线 Mock 跑通全流程（零 token、可复现，CI 里用的就是这个）
python cli.py review fixtures/sample_project --mock --verbose

# 输出 Markdown 报告并归档
python cli.py review src/ --format md --report-dir ./reports

# 真实调用（需要 ANTHROPIC_API_KEY）
export ANTHROPIC_API_KEY=sk-ant-...
python cli.py review app.py --standards team_rules.txt
```

**退出码**：`0` 通过 / `1` 有高危问题阻塞合并 / `2` 执行失败。GitHub Action 靠这个约定拦截合并。

## 闭环是怎么转的

```
源码 ──▶ 审查 Agent ──▶ 问题列表（带代码锚点）
              │
              ▼
         定位层：锚点 ──▶ 行号（三层匹配：精确 → 归一化 → 模糊 0.82）
              │              │
              │              └─▶ 失败 ──▶ unresolved（无行号，不进修复层）
              ▼
         修复 Agent ──▶ Git Patch ──▶ git apply --check ──▶ 内存应用
              │                                                │
              └──────────────── 回归审查 ◀────────────────────┘
                         │
            ┌────────────┴────────────┐
        原问题消失                  引入新问题
            │                          │
         ✅ VERIFIED              ❌ 计入 regression，重试
```

**重试配额**：P0/P1 三次，P2 两次，P3 一次。同一分类**连续**失败 2 次触发早停 —— 这通常意味着模型在这个维度上系统性无能，继续重试只是烧 token。

## 设计上的几个刻意选择

**用 tool use 而不是 Prompt 里求模型输出 JSON。** 需求原文的做法是把格式约束写进 Prompt，然后靠容错解析兜底。更可靠的是把 schema 交给 API 的 `tool_choice` 强制约束 —— 格式由协议层保证，容错解析退居第二道防线而不是主路径。`llm/tolerant_json.py` 仍在，但不再是唯一指望。

**审查与定位是两个独立的模型。** `Issue` 和 `LocatedIssue` 靠 `issue_id` 关联，`LocatedIssue` 在 Pydantic 层硬约束「`unresolved` 必须没有行号」。定位失败因此是一个下游**无法误用**的显式状态，而不是某个可能被当成真值消费的 `line_start=0`。

**指纹比 UUID 可靠。** 每次重新审查模型都会生成新的 `issue_id`，拿 UUID 比对「还是不是同一个问题」必然失败。回归比对用的是 `SHA1(分类 + 归一化锚点)`。

**补丁永不写回你的文件。** 全部在内存里应用、或落在临时目录。`--write` 选项没有实现，这是有意的。

**分块是降级路径，不是默认行为。** 小文件整体送审，因为切块会割裂跨函数上下文，模型对「这个变量哪来的」判断会失准。只有超预算时才按 AST 顶层定义切。

## 项目结构

```
codepilot-agent/
├── cli.py                    # 命令行入口（退出码契约）
├── core/
│   ├── schemas.py            # 数据契约 —— 全系统的类型地基
│   ├── anchors.py            # 锚点匹配引擎（三层）
│   ├── patch.py              # 补丁解析 / git 校验 / 内存应用
│   ├── chunker.py            # AST 切块与依赖注入
│   ├── workflow.py           # 状态机 + 回归闭环
│   └── errors.py             # 异常层次
├── agents/                   # 三个 Agent 的实现
├── prompts/                  # 提示模板
├── llm/
│   ├── client.py             # Mock / Anthropic 双模式
│   └── tolerant_json.py      # 容错 JSON 解析（第二道防线）
├── report/renderer.py        # 三层报告渲染
├── fixtures/                 # 示例项目 + Mock 响应
└── tests/                    # 171 个测试（含端到端）
```

详见 [docs/architecture.md](docs/architecture.md)。

## 测试

```bash
python -m pytest -q        # 171 passed
```

全部测试离线运行，不消耗 token。Mock 模式让整条流水线（含重试与早停）可复现 —— 这是能在 CI 里跑的前提。

## CI 集成

`.github/workflows/code_review.yml` 已配好：PR 打开/更新时审查改动的 Python 文件，高危问题完整展开在评论里，中低危以表格呈现，原始 JSON 归档为 Artifact，有高危问题时 job 失败以阻塞合并。同一个 PR 的连续推送会更新同一条评论而不是堆积。

需要仓库 Secret：`ANTHROPIC_API_KEY`。

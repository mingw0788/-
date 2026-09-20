# CodePilot Agent 架构说明

面向要读或改这份代码的人。重点解释**为什么这样设计**，而不是重复代码里已经写明的**做了什么**。

---

## 1. 分层与依赖方向

```
                    cli.py
                       │
        ┌──────────────┼──────────────┐
        ▼              ▼              ▼
   core/workflow   report/renderer  core/chunker
        │
   ┌────┼────────┬─────────┐
   ▼    ▼        ▼         ▼
agents/ core/anchors core/patch core/schemas
   │                              ▲
   ▼                              │
llm/client ──────────────────────┘
```

依赖是单向的：`schemas` 在最底层，谁都可以依赖它，它不依赖任何人。`workflow` 编排 `agents`，但 Agent 之间**不互相调用** —— 它们只通过 `schemas` 里的数据结构通信。这让每个 Agent 都能单独测试，也让「换个定位算法」不会波及审查逻辑。

---

## 2. 数据契约：`core/schemas.py`

整个系统的可信度建立在这几个类型约束上。

### 2.1 `Issue` 与 `LocatedIssue` 是分开的

需求里最硬的一条是「严禁编造行号」。这条要求在类型层面是这样落的：

- `Issue`（审查 Agent 的输出）**根本没有行号字段**。模型想编也无处可编。
- `LocatedIssue` 由定位层产出，带 `match_status`。Pydantic 的 `root_validator` 硬约束：`match_status == unresolved` 时 `line_start`/`line_end` 必须为 `None`，反之 `line_start` 不能为 `None`。

这不是靠文档约定，是靠类型系统。下游如果拿到一个 `LocatedIssue` 且想读 `line_start`，必须先检查 `is_resolved`，否则拿到的是 `None` 而不是一个看似合理的假行号。

### 2.2 指纹而非 UUID

回归阶段要回答「这一轮的问题还是上一轮那个吗」。模型每次审查都会生成新的 `issue_id`，所以 UUID 比对永远返回「不是同一个」—— 这个坑很隐蔽，因为代码看起来完全正常。

`compute_fingerprint(category, code_anchor)` 用 `SHA1(分类 + 空白归一化后的锚点)` 派生稳定标识。只归一化空白、**不转小写** —— Python 大小写敏感，把 `Foo` 和 `foo` 判成同一处代码是错的。

### 2.3 `blocking_escalation` 而不是 `has_escalation`

P2/P3 问题耗尽重试**不阻塞合并**，它们只是改进建议。只有 P0/P1 的失败才拦人。这个区分写在 `blocking_escalation` 里，报告和退出码都依赖它 —— 如果搞混，一个命名规范问题就能卡住所有人的 PR，工具会很快被绕过。

---

## 3. 定位引擎：`core/anchors.py`

三层匹配，逐层放宽：

| 层 | 方法 | 说明 |
| --- | --- | --- |
| 1 | 精确 | 原文逐字符包含 |
| 2 | 归一化 | 去行尾注释、压缩空白后比较 |
| 3 | 模糊 | `difflib.SequenceMatcher`，阈值 `FUZZY_THRESHOLD = 0.82` |

三层全失败 → `unresolved`，行号为 `None`。

**为什么阈值是 0.82 而不是更低。** 实测中一个单行的长锚点（如 `accumulator = accumulator + items[i] * items[j]`）在换了变量名和结构后相似度会跌到 0.6 附近 —— 这时候它**确实不是同一处代码**，判为 unresolved 是对的。放宽到 0.7 会让它匹配到语义无关的行上，那比不匹配更糟：一个错误的行号会污染后续所有环节。

**归一化为什么保留大小写。** 见 2.2。

---

## 4. 补丁层：`core/patch.py`

### 4.1 安全不变量

**补丁永不修改用户的原始文件。** 所有应用都发生在内存字符串或临时目录中。`apply_patch_in_memory` 接收源码、返回新字符串，输入保持不变（`tests/test_patch.py::TestOriginalFileNeverTouched` 守着这条）。

### 4.2 `git apply` 是权威

内存应用器是为了在没有 git 的环境下降级，不是主路径。`verify_and_apply` 先用 `git apply --check` 校验，通过后才应用。这样做的价值在于：**我们对 diff 格式的理解可能错，git 的不会**。

### 4.3 解析器必须按声明的计数走

`_parse_hunks` 严格按 hunk 头声明的 `old_count`/`new_count` 消费行数，而不是「遇到不像 diff 的字符就停」。

这一点踩过坑：早期的实现在 hunk 边界之后继续读，于是从一个声明 7 行的 hunk 里读出了 8 行，第 7 行匹配的是下一个函数的 `def`，报出的错误是「上下文不匹配」—— 看起来像模型给错了补丁，实际是解析器越界。

`git apply` 对计数的要求是**精确相等**：上下文 3 行就必须写 `@@ -1,3 +1,3 @@`，写 4 直接 `corrupt patch`。手写补丁极易踩这个，所以测试里的补丁一律用 `difflib` 生成。

### 4.4 换行符必须归一化

Windows 上的 `\r\n` 会让 git 报 `corrupt patch` —— 注意这个错误信息**没有诊断价值**，它不告诉你是换行符的问题。所以 `strip_patch_fence` 里做了 `.replace("\r\n", "\n")` 并保证末尾有换行，在这一层就统一掉。

---

## 5. 状态机与回归闭环：`core/workflow.py`

### 5.1 阶段

`REVIEWING → LOCATING → FIXING → REGRESSING`，逐问题循环，最后 `DONE` / `ESCALATED` / `FAILED`。

### 5.2 回归检查的两面

早期实现只检查「原问题是否消失」。这有个严重漏洞：一个消除了原问题、同时引入更严重新问题的补丁，会被判为**成功**。

现在的 `_detect_introduced` 同时做两件事，且**顺序不能颠倒**：

1. 拿本轮指纹跟已知指纹集合比较 → 找出新出现的
2. 把这些新指纹登记进已知集合

顺序颠倒（先登记再比较）会让集合里永远包含当前这批，比较结果恒为空 —— 检测形同虚设，而且代码看起来完全正确。`test_patch_that_introduces_a_new_issue_is_not_verified` 守着这条。

### 5.3 与需求文档的两处偏离

实现时发现需求里的伪代码有两处自相矛盾，做了修正并在 `workflow.py` 里注明：

**(1) 重试计数按 `issue_id` 而不是按优先级。** 伪代码写的是 `retry_count[fix.priority]`，但早停逻辑判断的是「同分类**连续**失败」。按优先级计数的话，两个不同的 P1 问题会共享同一个计数器，第一个问题的重试会消耗第二个的配额 —— 语义就乱了。

**(2) 定位以确定性算法为主，LLM 只作兜底。** 需求里的 Locator Agent 提示词让模型去匹配行号，却又声称「放弃 LLM 行号」——这两句是矛盾的。实现里锚点匹配是主路径，`LocatorAgent` 降级为未匹配项的回退手段。

### 5.4 重试与早停

配额：`{P0: 3, P1: 3, P2: 2, P3: 1}`。

`CONSECUTIVE_CATEGORY_FAILURE_LIMIT = 2`：同一分类连续失败两次就停。理由是这个信号意味着模型在该维度上**系统性**无能，而不是这一次运气不好 —— 继续重试只是烧 token。注意 `PatchError` 也必须计入这个统计，否则「所有补丁都打不上」的场景会一路跑完所有问题，每个白烧一轮调用。

### 5.5 `retry_trace` 是可观测性要求

每次重试都追加一条记录，**包括补丁应用失败**。没有这条记录时，「重试过但补丁打不上」和「压根没重试」在报告里长得一模一样。

`_retry_loop` 返回 `(fixed, current_code)` 元组而不是只改 `self._code`。这是修过的一个真实 bug：调用方会用未更新的 `current_code` 覆盖掉 `self._code`，导致后续问题的补丁打在旧代码上。部分应用的补丁同样会改变代码，所以「失败」也要返回新代码。

---

## 6. 分块与依赖注入：`core/chunker.py`

用标准库 `ast`，**不用 tree-sitter**。后者在 Windows + Python 3.9 上需要编译工具链，是纯粹的部署风险；而被审语言当前只支持 Python，`ast` 够用。

**默认不切。** 只有估算 token 超预算时才按顶层类/函数切。切块会割裂跨函数的上下文，模型对「这个变量是哪来的」判断会失准。切块是降级手段。

**每个 chunk 带模块头**（import、模块级常量），否则模型看不到 import，会把 `os.environ` 里的 `os` 报成未定义名。

**语法错误的文件不拒绝审查。** 语法错误本身就是审查目标之一，整体作为一个 chunk 送审。

---

## 7. LLM 层：`llm/client.py`

### 7.1 tool use 强制结构化

把 Pydantic 模型转成 tool 的 `input_schema`，用 `tool_choice={"type": "tool", "name": ...}` 强制模型走工具调用。格式由协议层保证，不需要在 Prompt 里求模型「别加 ```json 标记」。

`tolerant_json.py` 依然保留，但退居第二道防线：只在模型把 JSON 塞进 text 块（如工具被拒）时才走这条路。

### 7.2 Mock 模式是 CI 的前提

`MockLLMClient` 从 `fixtures/mock_responses/<tool_name>.json` 读预置响应。契约是**列表按次序取，取完后重复最后一项** —— 这样「同一问题反复修不好」的场景只需写少量响应。

`{"__sequence__": [...], "__repeat__": N}` 形式用于精确控制重试序列。这是测「重试后终于成功」所必需的：重试次数取决于优先级配额，不给逐次控制就只能测到「一直失败」。

---

## 8. 报告渲染：`report/renderer.py`

三层，对应需求里的展示要求：

1. **高危（P0/P1）完整展开** —— 位置、问题、建议、锚点、补丁、修复状态，全部列出
2. **中低危（P2/P3）表格** —— 一行一个问题，紧凑
3. **未通过项单独列出** —— 未定位、重试耗尽、跨文件、引入新问题，各自标注是否阻塞

退出码与 `blocking_escalation` 绑定，不与「有问题」绑定。

---

## 9. 测试策略

| 文件 | 覆盖 |
| --- | --- |
| `test_anchors.py` | 三层匹配，含故意不匹配的坑 |
| `test_patch.py` | 解析/校验/应用，正向与必须被拒绝的负向 |
| `test_chunker.py` | 切块边界、降级路径、依赖抽取 |
| `test_tolerant_json.py` | JSON 容错与 Pydantic 契约不变量 |
| `test_workflow_regression.py` | 重试配额、早停、引入新问题检测 |
| `test_cli_chunk_merge.py` | 多 chunk 报告的归并（防止前面 chunk 的高危结论被丢弃） |
| `test_workflow_e2e.py` | 端到端：样例项目跑完整链路 + 退出码契约 + 夹具漂移守卫 |

全部离线，不消耗 token。`test_workflow_regression.py` 用 `ScriptedClient`（继承 `BaseLLMClient`）按调用次序控制返回，而不是用声明式 fixture —— 回归闭环的正确性取决于「第 N 次审查看到什么」，用代码表达时序清楚得多。

**为什么还必须有 `test_workflow_e2e.py` 这一层。** 上面那些测试各自只证明「单个模块是对的」：锚点匹配准、补丁解析严、状态机逻辑正确。但「把它们串起来、从 CLI 进去、拿到正确的退出码」是另一件事，只有从 `cli.main` 端到端跑一遍才能回答。这一层的三个要点：

1. **走真实入口**。CI 门禁依赖退出码契约（0/1/2），而契约只存在于 `cli.main` 这一层，直接调 `WorkflowRunner` 是测不到的。
2. **消费真实夹具**。`fixtures/sample_project` 里埋了 7 处雷并标注 `# [SEED:n]`，`fixtures/mock_responses` 给出对应的审查/修复响应 —— 这套东西此前只被手工敲命令行消费过。
3. **夹具漂移守卫**。mock 响应里的 patch 由 `tools/build_mock_fixtures.py` 从样例源码派生。三道守卫的敏感度并不相同：`git apply` 对行号位移是容忍的、锚点的归一化匹配层会吸收缩进变化，所以「patch 还能打上」「锚点还能定位」都发现不了「样例被插了几行」；真正兜底的是**可再生性**检查（重新派生一遍必须与仓库里的夹具逐字节一致）。

**负向测试同样重要。** 补丁层有大量「必须被拒绝」的用例：打错地方比不打更危险。测试里的补丁一律由 `difflib` 生成，避免手写 `@@` 计数引入假失败。

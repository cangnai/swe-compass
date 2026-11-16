# 03 — Feedback-Guided Repair：反馈驱动修复详细设计思路

> 对应总体规划 `00_overall_plan.md` 的第三个改进方向。不是重复增加一句“测试失败请重试”的 Prompt，而是让 Agent 更有组织地理解测试反馈、跟踪失败变化，并在必要时重新思考修复假设。

## 1. 目标与原版问题

mini-SWE-agent 已具备基本反馈循环：`execute_actions()` 调用 `Environment.execute()`，再由 `Model.format_observation_messages()` 把运行结果返回模型。模型下一轮可以自行修改代码，因此**原版并非没有自我纠错**。

待增强的是：

- 不同命令的 Observation 混在一起，测试结果可能被长日志淹没；
- 相同失败反复出现时，缺少对失败签名和修改尝试的持续跟踪；
- 模型可能继续用相近操作重试，而不是根据新证据推翻根因假设；
- 容易把普通的非零退出码、代码测试失败、系统执行异常混为一谈。

**确定的方向**：`Test Feedback Parsing` + `Failure Memory` + `Event-Triggered Reflection`，由一个 `FeedbackManager` 协调，继续使用原有模型和 Bash 执行环境。

## 2. 三个机制的设计

### 2.1 Test Feedback Parsing：识别、结构化测试反馈

从实际执行的 Action 命令和环境返回值中识别测试相关操作，分析是否为测试运行及其状态。重点支持 SWE-bench Python 仓库中常见的 `pytest` / `unittest` 与项目自带测试命令；不能仅靠 `returncode != 0` 判断“测试失败”。

有用的结构化字段包括：

| 字段 | 示例或含义 |
|---|---|
| Test command | 实际执行的测试命令 |
| Outcome | passed / failed / interrupted / inconclusive |
| Failed tests | 失败测试节点或用例名 |
| Failure type | AssertionError、ImportError、运行超时等 |
| Evidence | 关键断言差异、traceback、相关文件/行号 |
| Source | 对应的 Action/Observation 与时间顺序 |

**事实与推断分离**：测试用例名称、退出状态和 traceback 是观察到的事实；“可能是某个函数的逻辑错误”只是下一轮 Agent 应验证的假设。

尽量使用原始 `Environment.execute()` 返回的 `output`、`returncode`、`exception_info`，在格式化成模型可见 Observation **之前**提取结构化信息，以免受 `observation_template` 的长输出截断影响。模型看到的测试摘要仍应保留足够证据，能够自行决定重看完整日志。

### 2.2 Failure Memory：记录失败和修复尝试

维护**当前 Issue 内部**的失败历史，记录失败签名、出现次数、最近关联代码修改与测试命令、当前是否消失，以及新旧失败之间的变化。它属于共享 Task State 的一部分，不另建跨任务长期记忆。

建议将“同一失败”识别建立在测试节点、异常类型与稳定的断言/堆栈特征上；避免把随机路径、临时地址或时间戳当成失败身份，也不要把不同根因的失败误合并。

关键是比较**结果的变化**：某个测试从失败到通过、一个旧失败消失但新增了另一失败、原失败在多次修改后仍未变化。这比仅仅记录“pytest 返回 1”有用得多。

### 2.3 Event-Triggered Reflection：仅在有意义的事件触发反思

反思不是额外启用一个常驻 Reflection Agent，也不是每轮都调用第二个大模型。

可触发的情况：

- 同一组关键测试失败在连续修复尝试后没有改善；
- 新测试证据与当前 Bug 假设明显矛盾；
- 出现回归（原本通过的相关测试在修改后失败）；
- Agent 反复执行相近的无效修复/测试而没有新信息。

触发时，用简明的结构化内容告诉**下一次正常 LLM 调用**：之前的假设、做过的修改、已有测试证据、重复失败的变化，以及需要重新审视的问题。要求模型重估根因或选择新的验证手段，但不替模型作出无法证实的诊断。

反思结果进入当前 Task State；如果压缩历史，关键的失败证据和反思结论仍然保留。相同失败没有新证据时应抑制重复反思提示，避免进一步制造上下文冗余。

## 3. 测试驱动修复在 Agent 循环中的位置

```text
LLM 读 Issue、检索并修改代码
          ↓
使用原有 Bash 执行相关测试
          ↓
Environment.execute() 返回原始执行结果
          ↓
Feedback Manager
    ├── 判断是否属于测试命令
    ├── 解析失败/成功证据
    ├── 更新 Failure Memory / Task State
    └── 判断是否需要触发反思
          ↓
正常 Observation + 精炼的反馈信息
          ↓
下一轮 LLM 自主选择继续定位、修改或测试
```

尽可能沿用 Agent 已决定执行的测试；同时，修复代码后可以根据当前改动范围和仓库已有的测试入口，引导 Agent 选择相关测试、回归测试或 Bug 复现命令。**不强制每次修改后跑全量测试**，也不把测试通过作为系统强行结束的唯一条件。

注意区分：

- **普通非零返回**：如 `rg` 未匹配的退出码，不属于测试失败或系统异常；
- **测试失败**：测试进程正常执行但断言/用例失败，是修复反馈；
- **执行异常**：如 Docker 无法连接、环境启动失败、工具执行本身出现异常，属于 Agent/执行环境层的问题；
- **无法判断**：保留原始输出并标记未确定，不强行分类。

测试反馈来源只包括 Agent 在目标仓库中实际可获得的命令执行结果；不能读取 SWE-bench 隐藏测试、官方 Ground Truth Patch 或任务答案来指导修复。

## 4. 与 mini-SWE-agent 的接入方案

主要阅读：

- `src/minisweagent/agents/default.py` 的 `execute_actions()`、`step()`、`query()`：确定何时读取原始输出、何时把反馈送进下一轮；
- `src/minisweagent/environments/local.py` 与 `docker.py`：确认 `output`、`returncode`、异常字段及超时行为；
- `src/minisweagent/models/utils/actions_toolcall.py`：`format_toolcall_observation_messages()` 如何组织模型可见消息及 `extra.raw_output`；
- `src/minisweagent/config/benchmarks/swebench.yaml`：现有 Observation 模板与原版测试指导 Prompt。

新增 `src/minisweagent/optimization/feedback_manager.py`，由 `agents/enhanced.py` 在 Action 执行结束后调用；与 `task_state.py`、`context_manager.py` 共享关键失败事实。

接入原则：

1. 解析原始执行结果，但保留原版 `format_observation_messages()` 对 Tool Call 的正确回传，不用自己随意伪造工具消息。
2. 反思提示应作为**下一轮模型上下文的一部分**，避免破坏当前工具调用与结果的配对关系。
3. 命令执行异常与项目测试失败分开处理，系统级异常不应被误导为“尝试修复代码”。
4. 同一 LLM 推理循环继续主导定位、代码修改和测试；`FeedbackManager` 只负责反馈理解和触发依据。
5. 修改后的状态必须能被 Context Manager 正确保留，旧测试长日志则可以在过时后清理。
6. 不为此模块引入强制循环次数或任务花费上限；具体重复失败条件、测试识别规则和提示结构由 Codex 阅读本地实现后确定。

## 5. 开源参考：具体借鉴哪些实现

### Reflexion — 反馈驱动的语言反思

- 论文：[Reflexion: Language Agents with Verbal Reinforcement Learning](https://arxiv.org/abs/2303.11366)
- 编程实现：[`programming_runs/reflexion.py`](https://github.com/noahshinn/reflexion/blob/main/programming_runs/reflexion.py)
- 借鉴：执行测试 → 获得反馈 → 生成反思 → 把反馈和反思带入下一次代码生成。
- 不直接照搬：论文原实现包含多次试验与候选程序生成；我们的反思应嵌入现有单次 Issue 的 Agent 循环，并按事件触发。

### Agentless — Patch Validation

- 项目说明：[`README_swebench.md` 中 Patch Validation and Selection](https://github.com/OpenAutoCoder/Agentless/blob/main/README_swebench.md)
- 相关代码入口：[`agentless/test/`](https://github.com/OpenAutoCoder/Agentless/tree/main/agentless/test)
- 借鉴：重视复现测试与已有回归测试的证据，利用测试区别“表面修改成功”和“真正修复问题”。
- 不直接照搬：多候选 Patch 排序、利用评测提供的测试信息等离线流程；我们不能把官方隐藏测试带入 Agent 的在线反馈。

### Aider — Lint / Test feedback

- 说明：[`Linting and testing`](https://aider.chat/docs/usage/lint-test.html)
- 借鉴：在改动之后利用 lint/test 输出驱动进一步修复，而不是只靠模型主观判断修改正确。
- 不直接照搬：Aider 的命令 UI 或强制的每次编辑自动测试策略；保留当前 SWE-bench 仓库真实可执行的测试命令。

## 6. 交给 Codex 时需要保持的设计结论

- 完整实现 **测试结果解析 + 失败记忆 + 事件触发式反思**，不只是换 Prompt。
- 复用现有 Action → Environment → Observation 链路，不更改原版 Bash 与 Tool Calling 的基本协议。
- 不将普通非零退出码等同于异常，不把测试失败等同于基础设施失败。
- 反思根据事实证据更新当前 Issue 的 Task State，不生成虚假的已确认根因。
- 实际测试识别、重复判定、提示形式和代码钩子由 Codex 按本地版本确认。

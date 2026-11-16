# Context Engineering 使用与实现

增强版实现上下文工程，并可启用[仓库级检索](repository_retrieval.md)和[反馈驱动修复](feedback_guided_repair.md)。`DefaultAgent` 保留独立运行方式，新增的 `get_model_messages()` 钩子默认仍返回原始历史。`EnhancedAgent` 继承原版循环、Bash 执行、限制检查、FormatError 计费、异常处理和轨迹保存。

## 启用

`enhanced.yaml` 是叠加配置，需要与原有完整配置一起使用：

```bash
mini -c default.yaml -c enhanced.yaml -m openai/gpt-4o -t "修复问题"
```

SWE-bench 批量运行：

```bash
mini-extra swebench --subset verified --split test -c swebench.yaml -c enhanced.yaml -o runs/enhanced
```

也可在已有配置中设置 `agent.agent_class: enhanced`。单实例 SWE-bench 入口及现有可观测任务执行器均通过 Agent 工厂选择，无需另外改入口。批量入口增加了可组合的进度包装，仍然保留进度显示与预测文件格式。

配置中 `cost_limit: 0` 沿用原版的“不设费用上限”语义；需要现有预算限制时可显式覆盖。上下文模块没有新增累计费用或累计 token 限制。

## 三项机制

- **历史清理**：只对已完成交互中的旧结果进行原位替换。识别完全相同的简单读取、搜索、测试命令；同文件的不同读取范围、不同测试范围、复合命令不会直接视为等价。读取仅在前后都成功时清理；测试记录可被同一命令的新结果替代。原始输出、返回码及对应工具调用均保留在完整轨迹中。
- **窗口压缩**：输入估算达到阈值时，对较早的完整交互生成确定性的摘录检查点。保留命令、返回码、异常、首尾片段及中间诊断行，并保存该段结构化记忆。系统提示、原始任务、人工补充约束、近期交互、未完成的工具调用、媒体消息和提交交互受到保护。检查点生成后持续复用；无压缩收益的段也不会每轮重复尝试。
- **任务记忆**：记录 Issue、约束、当前阶段、路径/符号证据、修改意图、`git diff --name-only` 报告的修改文件、同一测试命令的最新结果、未解决异常、模型假设及下一步。每条记录附带原始消息索引。执行返回码属于观测事实，模型推测和仅从命令识别的修改意图标记为 `unverified`，不将命令成功等同于修复正确。

压缩采用摘录式摘要，不额外调用模型，因此不会引入额外 API 计费或生成未经证实的总结。超长日志的中间诊断行会保留，摘要长度可能超过 `summary_chars`；信息不足时由 Agent 重新读文件或运行命令。上下文模块不做语义级根因分析；仓库检索和反馈驱动修复由独立模块提供，并与共享 Task State 协作。

发生清理或压缩后，每轮请求末尾会附加当前结构化记忆，帮助模型结合历史检查点与最新工作状态。旧模型假设保留在原始轨迹和相应检查点中，工作记忆只保存当前假设；末尾更新避免每轮改写系统提示及已缓存的历史前缀。

## 窗口参数

| 参数（位于 `agent.context`） | 默认值 | 用途 |
| --- | --- | --- |
| `context_window` | `null` | 显式设置单次请求窗口，适用于代理、私有部署和模型别名 |
| `fallback_context_window` | `32768` | 本地 LiteLLM 元数据缺失时使用的保守窗口 |
| `reserved_output_tokens` | `4096` | 输出预留空间；实际取此值和模型输出上限配置中的最大值 |
| `request_overhead_tokens` | `1024` | 为工具定义和协议开销预留的输入空间 |
| `compaction_threshold` | `0.85` | 可用输入空间达到此比例时尝试压缩 |
| `keep_recent_turns` | `4` | 始终保护最近多少个完整交互，而非简单保留 N 条消息 |
| `min_elision_chars` | `1000` | 避免替换短结果，降低不必要的缓存前缀变动 |
| `summary_chars` | `1200` | 摘录的首尾总长度，中间诊断行另行保留 |

窗口优先使用显式配置，否则读取 LiteLLM 的本地 `max_input_tokens` 元数据，再使用回退值，不额外发起元数据请求。为保守保护窗口，文本 token 数使用去除轨迹专用 `extra` 后的 UTF-8 字节数估算，因此通常会提前压缩；这不是实际 API token 用量。媒体的实际计量及供应商特殊开销应通过窗口和 overhead 参数保守配置。原始 Observation 截断模板保持生效。

```bash
mini-extra swebench -c swebench.yaml -c enhanced.yaml -c agent.context.context_window=128000 -c agent.context.reserved_output_tokens=8192
```

## 轨迹与恢复

`messages` 始终保存完整原始历史。模型可见视图通过深拷贝建立，不修改原始消息及 thinking block；工具调用和结果按完整交互共同压缩，保留尚在视图中的 `tool_call_id` / `call_id` 配对。

轨迹新增 `context_engineering` 字段：

- `task_state`：当前 Issue 的独立工作记忆。
- `checkpoints`：摘要消息及其原始半开区间 `[start, end)`。
- `elisions` / `events`：清理来源、替代消息及压缩/回退事件。
- `requests`：每次构造上下文的消息来源映射、实际使用的检查点区间和清理索引、窗口来源、输出预留和保守输入估算；`source_indices` 中的 `null` 对应检查点或末尾的结构化记忆，后者另存为该请求的 `memory_message`。

检查点构造失败、摘要为空或没有压缩收益时，保留该段原始历史。供应商拒绝处理后的消息结构时，如果原始历史仍能装入窗口，增强版会回退原始历史重试一次；两次请求均经过原版限制检查。原始历史也超窗或受保护内容过大时，保留完整轨迹及记忆并明确报错，不静默截掉任务状态。API 响应及实际 usage 仍由原模型适配器保存。

可观测系统的模型和环境包装继续接收真实调用，原始 Action、Observation、响应及用量保持原有结构；新增上下文状态随完整轨迹保存。每次调用 `EnhancedAgent.run()` 都重新创建本 Issue 的管理器并重置计数和计时。

同时修复了原缓存处理器对单内容块的假设：多块消息可清除旧缓存标记，新的标记放在最后一个非 thinking 块；不改写 thinking 签名。

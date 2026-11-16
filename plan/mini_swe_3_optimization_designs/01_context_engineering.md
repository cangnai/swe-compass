# 01 — Context Engineering：上下文工程详细设计思路

> 对应总体规划 `00_overall_plan.md` 的第一个改进方向。本文件明确要解决的问题、采用的技术、参考源码及其接入 mini-SWE-agent 的方式；实际类接口和参数由 Codex 检查当前 Fork 后决定。

## 1. 目标与原版问题

mini-SWE-agent 的核心循环为 `step() → query() → execute_actions()`。`DefaultAgent.query()` 将当前 `self.messages` 传给 `model.query()`；每次模型回复与工具 Observation 又被追加到历史中。

原版 SWE-bench 配置的 `observation_template` 已有**单条工具输出截断**（输出超过 10,000 字符时取前后片段），因此我们**不是再做一个输出截断器**。真正要补齐的是：

- 累积历史可能保存大量已经过时的搜索结果、旧文件内容和重复测试日志；
- 长任务需要跨多轮保留定位结论、已改文件、失败原因和下一步，单纯删除旧消息会丢信息；
- 即使有很长的模型上下文窗口，低价值历史也可能干扰后续决策；真正接近单次请求窗口上限时，还需要安全压缩；
- `self.messages` 同时服务于执行轨迹，不能为了省上下文直接删除原始记录。

**确定的方向**：`History Elision / Tool-result Clearing` + `Context Compaction` + `Structured Task Memory`，统一组织在 `ContextManager` 中。保留原版单 Agent + Bash 的流程。

## 2. 三项机制如何组合

### 2.1 事件感知的历史清理（History Elision）

对完成的工具交互进行低价值识别，而不是机械地每轮仅保留最近 N 条。

可清理的典型信息：被更近一次搜索结果取代的旧搜索列表；同一文件被重新读取后旧的冗长展示；旧测试完整日志中已被后续测试结果取代的重复片段。

默认保护的信息：任务原始描述、系统约束、最近的工作交互、尚未解决的关键异常、已验证的定位依据、最新一次相关测试结果、任务结束与提交有关的信息。

**处理方式**：优先把旧工具结果**原位替换**为简短的清理提示/必要事实；不要粗暴删掉 assistant 的工具调用消息或孤立删除 `tool` 消息。Tool Calling 模式下必须维护 `tool_call_id` 与工具调用的配对关系，多工具调用也必须完整处理。被清理的信息只有在可重新读取、已转移到 Task State，或确实失去价值时才可处理。

SWE-agent 的 `LastNObservations` 可参考“旧 Observation 替换为省略标记”的机制；`ClosedWindowHistoryProcessor` 可参考“过时文件窗口”判断；其 `polling` 和缓存相关说明提醒我们**不要每轮大范围重写整个消息前缀**。

### 2.2 上下文压缩（Context Compaction）

压缩针对的是**单次 LLM 请求预计上下文占用**，不是一个 Issue 已经累计消耗的 Token 或费用。

- 依据实际选用模型的上下文窗口与保留输出空间判断是否接近容量边界；模型元数据不可靠时保守估算并允许配置覆盖。
- 触发时把较早的**完整交互段**压缩成任务摘要/阶段状态，保留近期交互以及系统任务约束；一次压缩后持续复用结果，不能每轮反复摘要同一段历史。
- 摘要重点保留：问题与已确认事实、相关路径/符号、错误复现方式、修改点、已有测试结论、未解决问题、下一步。推测与已验证结论应有区别。
- 当压缩失败、结果缺失或模型不接受消息结构时，应优先使用可恢复的原始历史构造安全上下文；不允许悄悄丢掉任务状态。

**特别约束**：不根据 `cumulative_tokens`、`self.cost` 或任务累计花费触发压缩，也不在此模块设置每个 Issue 的消费上限。此处的“窗口阈值”是模型单次输入的技术边界，不是评测预算。窗口很大不代表需要强制定期压缩。

### 2.3 结构化任务记忆（Structured Task Memory / Working Memory）

Task State 仅属于**当前 Issue**，不是跨任务向量记忆库。建议保存：

| 维度 | 内容 |
|---|---|
| Issue | 原始目标、约束、当前处理阶段（不强制线性执行） |
| 定位 | 关键文件/函数、候选原因、证据来源 |
| 修改 | 已改文件、修改意图、尚待确认的影响 |
| 反馈 | 当前失败测试、异常摘要、重复失败线索 |
| 下一步 | 当前要验证的假设或待执行动作 |

通过**关键事件**更新，例如发现相关函数、发生代码修改、测试结果变化、定位结论推翻。能从工具调用与结果提取的字段优先规则化记录；判断不出来就保留为未确定信息，而不是自动编造结论。必要时让 LLM 在压缩时帮助整理复杂的推理进展，但不能把模型猜测写成已证实事实。

Task State 也应作为独立数据保存在轨迹/扩展状态中，供其他两个模块共享：检索模块写入定位线索，反馈模块写入测试与失败信息。

## 3. 与 mini-SWE-agent 的接入方案

**主要阅读的原版代码**：

- `src/minisweagent/agents/default.py`：`run()`、`query()`、`execute_actions()`、`add_messages()`、`serialize()/save()`；
- `src/minisweagent/models/litellm_model.py`：`query()` 与 `_prepare_messages_for_api()`；
- `src/minisweagent/models/utils/actions_toolcall.py`：工具调用与 Observation 格式；
- `src/minisweagent/config/benchmarks/swebench.yaml`：已有 Observation 截断、Prompt 以及 API 配置。

建议新增 `optimization/context_manager.py` 与 `optimization/task_state.py`，由 `agents/enhanced.py` 管理生命周期。**不要求修改 LiteLLM 的协议或重写 Agent 主循环**。

```text
完整任务历史（用于轨迹与恢复）
     │
     ├── Task State 读取/更新关键事实
     └── Context Manager 构造模型可见视图
             ├── 事件感知旧结果清理
             ├── 检查单次上下文窗口占用
             └── 必要时摘要较早历史
                      ↓
                 model.query(...)
                      ↓
             原始回复继续写入完整历史
```

**关键边界**：

1. `full_messages` 是权威的原始轨迹；`api_messages` 是为该轮模型构造的视图，两者不能混用，也不能就地修改原始对象。
2. 保留原有 `query()` 中步数、时间、费用统计、异常及 `FormatError` 相关处理语义；可由 Codex 选择钩子或继承方式，避免复制遗漏限制检查。
3. 在原生工具调用下保留合法的消息角色与工具配对；不要只处理纯文本 Bash 格式。
4. 考虑 Anthropic thinking block、缓存前缀和 LiteLLM 的消息清理方式，避免压缩造成消息格式不合法或频繁失去缓存收益。
5. 每次新 Issue 清空对应 Task State、压缩段状态和临时上下文；不能将上一个 Issue 的历史带入新任务。
6. 压缩前后保存必要的关联信息，确保现有可观测系统仍可回溯原始 Action、Observation 和 API 用量。

## 4. 开源参考：看什么、借鉴什么

| 参考 | 具体位置 | 借鉴点 | 不直接照搬的部分 |
|---|---|---|---|
| SWE-agent | [`sweagent/agent/history_processors.py`](https://github.com/SWE-agent/SWE-agent/blob/main/sweagent/agent/history_processors.py) | `LastNObservations`、`ClosedWindowHistoryProcessor`、历史视图与缓存提示 | 不采用固定 N 轮作为唯一清理依据；消息格式要适配 mini-SWE-agent |
| mini-SWE-agent | [`agents/default.py`](https://github.com/SWE-agent/mini-swe-agent/blob/main/src/minisweagent/agents/default.py) | 模型调用前唯一清晰的上下文接入点 | 不直接清空/覆写 `self.messages` |
| mini-SWE-agent | [`models/litellm_model.py`](https://github.com/SWE-agent/mini-swe-agent/blob/main/src/minisweagent/models/litellm_model.py) | API 消息准备、缓存与格式边界 | 不在模型适配器中塞入完整 Task Memory 业务逻辑 |

## 5. 交给 Codex 时需要保持的设计结论

- 这是**完整版 Context Engineering 模块**，不是单独实现“最近 N 轮历史”的简化版本。
- 事件感知清理、窗口保护压缩、Task Memory 三项能力都属于本模块，相互协作。
- 不限制单 Issue 累计 Token/Cost；不把累计 Token 作为压缩触发条件。
- 保留完整轨迹、原生 Tool Calling 合法性、Prompt Cache 影响及异常恢复语义。
- 具体阈值、状态结构、更新规则、压缩提示词与接口签名由 Codex 对照**本地实际版本**确定，不在方案里写死。

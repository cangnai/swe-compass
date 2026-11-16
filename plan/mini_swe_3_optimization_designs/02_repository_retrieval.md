# 02 — Repository-Level Retrieval：仓库级代码检索详细设计思路

> 对应总体规划 `00_overall_plan.md` 的第二个改进方向。聚焦“Agent 如何更快找到该改的代码”，不重建新的代码编辑 Agent，也不把仓库全部塞进 Prompt。

## 1. 目标与原版问题

mini-SWE-agent 已能通过 Bash 使用 `ls`、`find`、`rg/grep`、`sed/cat` 等命令搜索代码。问题不在于它没有搜索能力，而在于**Issue → 关键词 → 文件 → 函数 → 代码片段**主要靠模型反复试探，容易造成：

- 关键词不准导致宽泛搜索和大量无关匹配；
- 同一个符号在多处出现，缺少文件/函数层级上的定位组织；
- 反复读取大文件，模型难以聚焦与报错有关的少数代码区域；
- 阅读结果只停留在当前对话里，与后续 Task State 不够一致。

**确定的方向**：结合 **Hierarchical Code Localization（分层代码定位）**、**Symbol-Aware Retrieval（符号感知检索）** 与 **Hybrid Ranking（混合排序）**，形成一个轻量的 `RepoRetriever`。它是 Bash 的辅助能力，**不是新的智能体**。

## 2. 具体检索流程

### 2.1 File-Level Localization：先找候选文件

综合三个线索来源：Issue 中明确的文件路径/模块名、异常堆栈和测试路径，以及关键词全文搜索。

优先利用 `rg --files`、`ripgrep` 与仓库目录结构产生候选列表；排除 `.git`、虚拟环境、构建产物和明显无关的生成文件，注意符合仓库的忽略规则。保留每个候选文件的**来源依据**（例如路径命中、异常栈命中、文本命中），而不是只返回一个不透明的分数。

### 2.2 Symbol-Level Localization：再找类和函数

对候选文件建立或按需读取符号索引：文件、类、函数、方法、行号范围及必要的定义信息。

- Python 仓库可使用标准库 `ast` 获得可定位的类/函数边界；
- 对其他可支持语言参考 Aider 的 Tree-sitter 思路；解析失败时保留文本检索能力，不把整个仓库检索绑定在单一解析器上；
- 将异常栈中符号、Issue 提到的 API、测试函数和源码定义建立联系。

这里只提供**定位与跳转**信息，不做静态分析器式的完整程序理解，也不自动认定搜到的函数就是 Bug 根因。

### 2.3 Hybrid Ranking & Snippet Retrieval：排序后返回有依据的片段

按路径/文件名匹配、精确符号匹配、报错栈关联、关键词匹配、相关测试引用等信号组合排序，并对同一文件的相近命中去重。

返回结果应包含：

```text
候选文件路径
相关类/函数及行号范围
命中原因（路径 / 符号 / 异常栈 / 关键词）
简短、有上下文的代码片段
```

返回片段不是越长越好：先给定位线索和必要上下文，模型需要时仍可继续通过 Bash 查看整个函数、调用处或其他文件。对索引与检索结果要考虑源码修改后的**失效与刷新**，不能持续返回已修改前的行号或旧代码。

## 3. Agent 如何调用、与上下文模块如何配合

推荐保留 mini-SWE-agent 的**单 Bash Tool**。提供一个位于任务环境中、由 Bash 可调用的仓库检索辅助命令；Agent 可以自主选择普通 Bash 或增强检索，不要求每个 Issue 先强制执行一轮独立的 LLM 定位任务。

```text
LLM 需要定位代码
       ↓
现有 Bash Tool 调用 repo_search 辅助命令
       ↓
Docker / 当前任务代码仓库（SWE-bench 通常在 /testbed）
       ↓
文件检索 → 符号定位 → 候选排序 → 代码片段
       ↓
作为普通 Observation 返回
       ↓
Context Manager 管理冗余检索结果
Task State 记录关键定位线索
```

**重要的执行环境边界**：

- 检索必须针对当前 Issue 的容器内仓库内容；不能在宿主机搜索另一个版本的代码。
- 辅助工具及其索引/缓存应放在目标仓库之外的临时工具目录，避免污染需要提交的 Patch。
- 由 Codex 检查 `Environment.execute()`、Docker 初始化方式以及仓库工作目录后，选择实际部署/调用脚本的途径。
- 搜索条件、文件路径等必须安全转义与校验，避免将 Issue 原文直接拼接成可执行 Shell 语句。
- 允许搜索后进一步使用标准 Bash，不把检索 Top-K 误当成硬约束；查不到时可回退到普通搜索。
- 代码可能包含多种语言/生成文件/符号解析失败等情况，检索层需保留稳健的降级路径。

## 4. 建议的代码落点

- 新增 `src/minisweagent/optimization/repo_retriever.py`：组织检索、索引和排序逻辑。
- 在 `agents/enhanced.py` 与 SWE-bench 对应环境初始化过程中，接入辅助检索命令、必要的提示信息及状态交互。
- 继续复用原来的 `Environment.execute()` 与 `Model.format_observation_messages()`：检索输出就是普通工具结果。
- 与 `task_state.py` 对接：记录有证据的候选文件与定位结果；与 `context_manager.py` 对接：旧搜索列表可在后续被清理。

不要为了检索把原来的 Bash Tool 改成一套多工具、多 Agent 协作协议；也不要要求全仓库一次性 Embedding 或建立向量数据库作为运行前置条件。**完整能力是分层定位 + 符号检索 + 混合排序 + 可用的 Agent 接口**，不是“先做一个简单 grep”。

## 5. 开源参考：具体借鉴哪些模块

### Aider — Repo Map

- 源码：[`aider/repomap.py`](https://github.com/Aider-AI/aider/blob/main/aider/repomap.py)
- 原理说明：[`Repository map`](https://aider.chat/docs/repomap.html)
- 借鉴：Tree-sitter 提取定义/引用、符号与文件关联、候选相关度排序、生成紧凑的代码结构信息。
- 不直接照搬：Aider 每轮向模型提供 Repo Map 的完整上下文策略及复杂依赖/排序实现。我们的检索应由 Agent **按需调用**，避免每次请求注入一份全局 Map。

### Agentless — Localization

- 源码：[`agentless/fl/localize.py`](https://github.com/OpenAutoCoder/Agentless/blob/main/agentless/fl/localize.py)
- 运行说明：[`README_swebench.md` 的 Localization 部分](https://github.com/OpenAutoCoder/Agentless/blob/main/README_swebench.md)
- 借鉴：文件级 → 类/函数级 → 精细编辑位置的分层定位思路、候选代码上下文组织方式。
- 不直接照搬：Agentless 的独立多阶段 LLM 推理和多候选 Patch 生成流程；我们仍由 mini-SWE-agent 自主决定何时搜索、阅读和修改。

### 原版 mini-SWE-agent

- [`environments/docker.py`](https://github.com/SWE-agent/mini-swe-agent/blob/main/src/minisweagent/environments/docker.py)：确认容器执行与文件环境。
- [`agents/default.py`](https://github.com/SWE-agent/mini-swe-agent/blob/main/src/minisweagent/agents/default.py)：现有 Action/Observation 循环。
- [`config/benchmarks/swebench.yaml`](https://github.com/SWE-agent/mini-swe-agent/blob/main/src/minisweagent/config/benchmarks/swebench.yaml)：现有 Bash 工具提示与工作目录。

## 6. 交给 Codex 时需要保持的设计结论

- 这是**完整版轻量化仓库检索能力**，包含文件级、符号级和片段级定位，以及必要的候选排序和索引更新。
- 保持单 Agent + Bash；检索工具不取代自由 Bash 探索，避免错误定位被强制固化。
- 借鉴 Aider 的符号/仓库结构思路和 Agentless 的层次化定位，但不复制其整套 Agent 运行机制。
- 检索必须在当前 Issue 的执行环境中运行，索引不得混入目标提交文件。
- 函数签名、命令格式、索引方案与第三方依赖由 Codex 对照本地代码与目标仓库特征确定。

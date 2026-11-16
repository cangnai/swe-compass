# mini-SWE-agent 执行轨迹与可观测评测平台：完整开发计划

> 版本：V2.0（四模块一体化实施版）
> 日期：2026-11-03
> 目标：在现有 mini-SWE-agent 基础上，**一次性完成四个模块的实现、集成和验收**，形成可运行、可展示、可用于简历项目的系统。
> 说明：本文是交付需求和实现约束，不表示当前功能已经完成。

## 1. 目标与实施方式

基于 mini-SWE-agent 现有的单 Agent Coding Agent，增加统一的执行轨迹、运行监控、失败分析和实验对比能力。**四个模块同时纳入本次开发范围，共用一套数据结构、存储层和界面，在同一次开发任务中全部完成并统一验收。**

| 模块 | 最终要交付的功能 |
|---|---|
| **M1 执行轨迹可视化** | 历史轨迹导入、任务列表、Step 时间线、中文消息、Action/Observation、代码 Diff、历史记录 Playback |
| **M2 可观测系统** | Run/Trace/Span 追踪、LLM/Tool 耗时、Token/Cost、错误事件、指标聚合 Dashboard |
| **M3 Agent 失败分析** | 错误分类、重复命令、重复测试失败、疑似无进展循环检测、证据与 Step 定位 |
| **M4 实验与测评系统** | 自定义任务与运行结果登记、测试结果判定、模型/Prompt/配置维度的通用对比、报表导出 |

实施要求：

1. **一次完整开发，不按模块分别提交四份互相割裂的方案，也不把已经确定的功能留给下次迭代。**
2. 开发中可以正常调试和编写测试，但最终应交付四个模块联通的可运行版本，而不是仅提交设计、目录骨架、占位函数或静态界面。
3. **不依赖 SWE-bench 数据集即可完成四个模块。** M4 先用自定义任务及其真实测试结果工作；之后增加 SWE-bench 任务与评测结果来源即可，不需要重做底层系统。
4. 复用项目已有 `.traj.json`、Inspector 和统计信息中有价值的逻辑，避免重复造轮子；新增功能以结构化数据采集、分析和可用 UI 为主。
5. 以目前本地实际安装的代码和启动方式为准；开始编写时检查必要的函数签名、轨迹格式和运行入口，这属于本次开发的常规工作，不单独设立“调研阶段”。

## 2. 对原 Agent 逻辑的要求

**原 Agent 的主体执行逻辑应保持不变。** 特别是：

- 不改变原有 `run()` / `step()` 的任务决策顺序和循环语义。
- 不改变 Prompt、模型选择、Action 解析、Shell 命令执行、Observation 回填、异常处理和任务提交的原有业务行为。
- 不为了监控功能重新实现 Coding Agent，也不让展示、诊断和指标采集结果反向影响 Agent 决策。
- 既有启动方式和原始轨迹保存方式尽可能维持兼容；关闭新增功能后，Agent 仍能像原来一样运行。

**实现方式：** 优先在新增模块中使用 wrapper、子类、回调/钩子或外围调用层采集；只有确实无法获取必要数据时，才在原源码边界增加少量**纯观测性埋点**。这不等于修改 Agent 算法或执行逻辑。若不可避免修改原文件，应列出修改的函数、原因与影响，并验证运行行为一致。

建议关注但不要机械地全部修改：

| 代码位置 | 需要获取的信息 |
|---|---|
| `agents/default.py` 的 `run()` / `step()` 等 | Run/Step 开始和结束、执行步数、终止原因、消息关联 |
| `models/litellm_model.py` 的模型调用边界 | 请求耗时、模型名、Token Usage、Cost、调用异常 |
| `environments/local.py` 的 `execute()` | 命令、工作目录、执行耗时、返回码、异常与输出 |
| 原有轨迹保存与 Inspector 相关实现 | `.traj.json`、配置、消息、已有统计字段与代码复用 |

> 具体函数名称与数据可用性，以仓库当前实际代码为准。绝不能为了实现指定的埋点位置而改变核心 Agent 行为。

## 3. 四模块共用的完整架构

```text
手动任务 / 自定义测试任务                  历史 .traj.json
           │                                   │
           ▼                                   ▼
     mini-SWE-agent                       轨迹解析器
  Agent Loop / Model / Tool                    │
           │ 只读观测采集                       │
           ▼                                   │
    Trace / Span / Event 统一结构 ◄──────────────┘
           │
           ▼
    SQLite + 原始轨迹文件
           │
      ┌────┼───────────┬───────────┐
      ▼    ▼           ▼           ▼
    M1     M2          M3          M4
  轨迹页  监控页      失败分析页   实验对比页
      └────┴───────────┴───────────┘
                       │
                统一本地 Dashboard
```

**统一关联主键：**

- `task_id`：逻辑任务；同一任务可以运行多次。
- `run_id` / `trace_id`：一次实际 Agent 执行。
- `step_index`：任务内的执行步数。
- `span_id`、`parent_span_id`：模型调用、工具执行和 Step 的父子关系。
- `experiment_id`：用于比较的一组 Run；不是预先写死的实验组。

### 3.1 数据结构与口径

| 对象 | 建议字段 |
|---|---|
| Task | `task_id`, `title`, `description`, `repo_path`, `base_revision`, `test_command`（可为空） |
| Run/Trace | `run_id`, `task_id`, `model`, `config_snapshot`, `start_time`, `end_time`, `duration_ms`, `exit_status`, `submission`, `trajectory_path` |
| Span | `span_id`, `run_id`, `parent_span_id`, `step_index`, `kind`, `start_time`, `end_time`, `duration_ms`, `status` |
| LLM 信息 | `model`, `input_tokens`, `output_tokens`, `total_tokens`, `cost_usd`, `error` |
| Tool 信息 | `tool_name`, `command`, `cwd`, `returncode`, `duration_ms`, `exception_info`, `output_reference` |
| Event | `event_id`, `run_id`, `step_index`, `type`, `severity`, `evidence` |
| Evaluation | `run_id`, `evaluator`, `verdict`, `test_summary`, `evidence_path`, `evaluated_at` |
| Experiment | `experiment_id`, `name`, `run_ids`, `created_at` |

- 运行时采集真实开始/结束时间及耗时。历史轨迹如果缺少耗时、Token、费用或命令返回码，显示 `unknown` / `null`，不伪造数据。
- `Submitted` 只是 Agent 提交结果，不代表 Bug 修复成功；只有实际测试或评测判定才可标记 `passed` / `resolved`。
- 一个工具命令返回非零不等于整次任务失败；运行中的异常事件与最终任务状态分别保存。
- 保留原始轨迹，不破坏旧 JSON；界面统一解码中文 Unicode 转义。
- 对长 Tool 输出显示摘要，同时保留可检索的完整输出或文件引用。

## 4. M1：执行轨迹可视化

**功能必须一次实现完整：**

1. **任务/运行列表**：从原 `.traj.json` 批量导入旧记录，并显示实时采集的新 Run；支持按任务、模型、运行状态检索。
2. **Step 时间线**：展示每一步模型决策、工具调用与反馈，点击后打开对应详情。
3. **消息展示**：System / User / Assistant / Tool（或 Observation）正文、原始消息 JSON；正确显示中文明文。
4. **Action / Observation 关联**：命令、工作目录、返回码、输出、异常；兼容文本动作和原生 `tool_calls`，必要时使用 `tool_call_id` 关联。
5. **代码修改 Diff**：优先读取轨迹或 Git 中已有的 Patch/Diff；对新运行可采集适当的工作区变更信息。无快照或 Diff 时如实显示“无可用 Diff”，不能凭消息编造代码差异。
6. **历史记录 Playback**：可以按“上一步/下一步”顺序查看动作和结果，或直接定位 Step；这里的回放是**查看已保存记录**。

**验收：** 能导入真实旧轨迹；新 Run 结束后能在同一页面打开；Step 数量与 Action/Observation 对应正确；中文可读；代码 Diff 和历史 Playback 可用。

## 5. M2：可观测系统

**运行时采集：**

- Run/Step/LLM/Tool 的 Trace/Span 及调用关系。
- 每次模型请求、命令执行、整次任务的耗时；LLM 与 Tool 耗时拆解。
- Input/Output Token、费用（来自可信 API Usage / 计费信息；不可得时标未知）。
- 模型调用次数、工具调用次数、非零退出码、格式解析失败、API/Tool 异常、终止原因。
- 每次执行使用的模型和 Agent 配置快照，便于定位某次运行。

**Dashboard 页面：**

- 总任务数、完成/异常数量、平均执行步数、平均耗时、Token 和已知费用。
- 按 Run、Step、模型的耗时/Token/Cost 统计与筛选。
- LLM 与 Tool 时延比较、耗时最长的步骤、工具调用/报错分布。
- 从指标图表跳转到相关 Trace 和具体 Step。

**验收：** 执行一个新任务后可查看结构化 Trace 和逐次真实耗时；已有轨迹在缺少实时信息时也能正常展示；聚合数据和原始记录可核对；不影响原 Agent 正常运行。

## 6. M3：Agent 失败分析

对 M2 采集的事件和 M1 的消息/动作做规则分析，并给出证据，不只显示错误总数。

| 检测类型 | 检测和展示方式 |
|---|---|
| 格式/解析错误 | 模型输出无法解析 Action 或 Tool Call 时，显示错误消息及对应 Step |
| 工具执行错误 | 非零返回码、异常、超时；保留命令、错误摘要和 Step |
| 运行终止异常 | 记录超时、达到限制、未捕获异常等实际终止原因 |
| 重复命令 | 对规范化后的命令进行连续/窗口内重复检测，显示重复次数和位置 |
| 重复测试失败 | 从测试执行结果和输出中提取失败特征，识别连续相似失败 |
| 疑似无进展循环 | 多轮重复动作、测试失败特征不变且没有可观察到的有效变化时提示“疑似”，附证据 |

**诊断结果页面**包含：诊断类型、严重级别、触发规则、相关 Step、关联命令和输出片段、支持判断的证据。支持从诊断直接跳转对应轨迹位置。

重复执行 `pytest` 可能是正常修复流程；必须结合错误输出和代码变更判断，避免简单地把“同命令执行多次”全部定性为失败。

**验收：** 用已知正常、工具失败、测试重复失败和循环轨迹验证诊断；每条诊断都可以追踪到原始证据；规则结果与任务最终成功/失败分开显示。

## 7. M4：实验与测评系统

**本次就做成真正能用的通用模块，不等待 SWE-bench。**

1. **任务登记**：录入已有或新建自定义 Bug 修复任务，包括描述、运行目录、初始版本和可选的验证命令。
2. **运行记录**：每次执行自动或手工关联 `task_id`、模型、Prompt/配置快照、Run/Trace 和监控指标。
3. **任务结果判定**：支持执行真实测试命令并记录退出码、摘要、证据；支持标记 `passed`、`failed`、`not_evaluated`、`evaluation_error`，未运行测试不得默认为通过。
4. **通用对比**：用户在界面选择同一个或多个任务的若干 Run，比较模型、配置、测试判定、步数、时延、Token、费用、错误及诊断分布。**不预设 Baseline / Exp-A / Exp-B 等实验组，不预先规定未来要比较哪种策略。**
5. **结果输出**：能够查看、筛选并导出 CSV/JSON 的运行和对比统计结果，每行可追溯到对应 Run/Trace。
6. **未来数据集接入能力**：任务和评测记录对外部 `task_id`、评测器来源和评测结果保持通用字段。日后要用 SWE-bench 时，增加数据导入与官方评测适配即可；**本次交付不依赖下载或运行 SWE-bench**。

自定义任务的多次运行应考虑代码状态隔离/重置，避免第一次修复后第二次在已经修复的代码上测评，使比较失真。

**验收：** 使用至少两个真实的自定义 Run 演示对比页面，能看到独立评测结果及资源消耗差异，能导出文件；没有评测结果的 Run 被正确标注为未评测。

## 8. 推荐的项目结构

目录结构根据现有工程布局适度调整，不以完全一致的文件名为验收目标。

```text
mini-swe-agent/
├── src/minisweagent/
│   └── observability/
│       ├── schema.py             # Run/Trace/Span/Event/Evaluation 等模型
│       ├── instrumentation.py    # 非侵入式采集与 Agent 接入
│       ├── trajectory_parser.py  # 历史轨迹导入
│       ├── storage.py            # SQLite/原始轨迹关联
│       ├── metrics.py            # 指标聚合
│       ├── diagnostics.py        # 失败/循环诊断
│       └── experiments.py        # 任务、运行、评测、对比
├── dashboard/
│   └── app.py                    # 统一 UI（四个功能页）
├── tests/
│   └── observability/           # 真实记录 + 构造测试样例
└── docs/
    └── observability_usage.md   # 安装、启动和使用方式
```

**技术栈建议：** Python、SQLite、Streamlit、Plotly、pytest。若需要单独 API 服务，再选 FastAPI；不要为本地单机 Dashboard 引入没有实际用途的复杂基础设施。

## 9. 一次性交付清单和统一验收

请把下面全部视为**同一轮开发任务的完成条件**，不是四轮开发计划。

### 可运行成果

- [ ] 统一 Trace/Span/Event 数据模型、存储及历史轨迹导入。
- [ ] M1：任务列表、轨迹详情、Step 时间线、消息、Action/Observation、代码 Diff、Playback。
- [ ] M2：实时 LLM/Tool 数据采集、指标聚合、完整 Dashboard。
- [ ] M3：失败分类、重复调用/测试失败/疑似循环识别、证据定位。
- [ ] M4：自定义任务/Run/Experiment、真实结果判定、通用对比、导出。
- [ ] 四个功能共享同一数据源，页面之间可通过 Run/Step 跳转。
- [ ] 保留原 Agent 原有运行方式和原始 `.traj.json` 兼容性。
- [ ] 提供必要的运行命令、依赖说明、简要使用文档和演示样例。

### 统一测试（完成四模块开发后整体检查）

- [ ] 导入既有 `.traj.json`，中文、消息顺序和工具反馈展示正确。
- [ ] 执行一个新的 Agent 修复任务，自动产生 Trace/Span 与指标。
- [ ] 展示一段含错误/反复测试的轨迹，诊断跳转到实际 Step。
- [ ] 对同一个自定义任务的两个 Run 进行结果与资源指标对比。
- [ ] 检查异常和缺字段轨迹时，不产生虚假的耗时、费用或“成功”。
- [ ] 原始 Agent 的动作解析、代码修改、工具执行、提交等行为保持不变；相关原有测试仍通过。
- [ ] 所有页面和模块之间的数据关联正确，无仅用于展示的伪造静态数字。

### 最终交付说明必须写清

1. 新增、修改了哪些文件；如改动原 Agent 文件，说明其仅为数据采集的最小必要改动。
2. 四模块分别已实现什么，哪些细节因本地版本实际限制无法获得，要真实说明。
3. 如何安装依赖、运行 Agent、启动 Dashboard、导入旧轨迹以及进行自定义任务对比。
4. 跑过哪些测试，结果如何；提供可访问的真实演示数据，不拿模拟指标冒充真实实验结果。
5. 是否改变原 Agent 的业务行为；若声称没有改变，应给出验证依据。

## 10. 交给 Codex 的执行要求

> 请先阅读当前 mini-SWE-agent 仓库的实际代码、现有 `.traj.json` 和启动方式，然后**按照本文一次性完成 M1、M2、M3、M4 四个模块**，不要按四个阶段分别只做一个模块，也不要先给计划就停止。
> 请直接实现统一数据结构、运行时采集、历史轨迹解析、存储、Dashboard、失败分析、自定义任务评测与通用对比，并把四者完整集成起来；最后统一运行测试和给出演示。
> 优先新增独立模块或外围 wrapper，不改变 Agent 的 Prompt、循环、Action 解析和命令执行等主体逻辑。确需修改原文件时只加入最小观测埋点，并列明原因。
> 使用现有轨迹与自定义 Bug 任务完成演示；不需要现在获取 SWE-bench 数据集，也不要预设未来的模型对比实验组。
> **本次完成标准是四个模块可实际运行和互相联动，而不是仅有项目目录、方案或占位功能。**

## 11. 参考源码

- mini-SWE-agent：https://github.com/SWE-agent/mini-swe-agent
- Agent 核心逻辑：`src/minisweagent/agents/default.py`
- 模型封装：`src/minisweagent/models/litellm_model.py`
- 本地执行环境：`src/minisweagent/environments/local.py`
- 原有轨迹保存与 Inspector：以仓库当前版本对应实现为准

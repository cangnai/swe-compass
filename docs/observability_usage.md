# mini-SWE-agent 观测、诊断与评测

四个模块共用 SQLite 中的 Task / Run / Span / Event / Evaluation / Experiment。历史导入与运行时采集使用同一结构，所有页面可通过 Run / Step 定位证据。既有 `mini`、Inspector、Prompt、Agent 循环、解析器和环境执行代码没有因本功能而修改。

## 安装与快速演示

在仓库根目录执行（Python 3.10+，需 Git；演示还需 pytest）：

```powershell
python -m pip install -e ".[observability]" pytest
python -m minisweagent.run.observe demo
python -m minisweagent.run.observe serve --db .mini-observe/demo/traces.db
```

打开 <http://127.0.0.1:8501>。安装后也可用 `mini-observe` 替代 `python -m minisweagent.run.observe`。

若直接从源码启动、没有执行 editable install，PowerShell 中先设置：

```powershell
$env:PYTHONPATH = "$PWD/src"
$env:MSWEA_GLOBAL_CONFIG_DIR = "$PWD/.mini-observe/config"
$env:MSWEA_SILENT_STARTUP = "1"
$env:PYTHONUTF8 = "1"
```

第二项是原项目支持的全局配置目录选项，在限制写入用户目录的环境中使用。已有 API 环境变量仍可使用；如依赖原全局 `.env`，应保留原目录或显式加载所需变量。

`demo` 导入 `tests/test_data` 中两条实际旧轨迹，然后在新建的演示 Git 仓库中复现 `divide(5, 2)` 错误地返回 `2` 的 Bug。两次运行都从同一初始 commit 克隆独立工作区：

- 第一次连续执行三次真实 pytest，保留未修复状态并提交，独立评测为 `failed`。
- 第二次复现问题、真实编辑源文件、执行 pytest 并提交，独立评测为 `passed`，可查看 Git Diff。

演示使用仓库已有 `DeterministicModel` 提供预先编写的响应，**不是在线 LLM 修复能力或模型优劣实验**。命令执行、代码修改、测试退出码、调用与运行耗时均实测。没有 API Usage / 账单，因此 Token、Cost 为 null。两次响应序列属于演示输入，不是预设的未来实验组。重复执行 demo 会添加新的任务与两个独立 Run，不覆盖之前的运行。

演示产物保存在 `.mini-observe/demo/`：

- `traces.db`：全部关联数据；`artifacts/`：原轨迹与完整评测证据。
- `demo-manifest.json`：最近演示的 task_id / run_ids。
- `demo-comparison.json`、`demo-comparison.csv`：真实对比结果。
- `demo-sources/`：可检查的初始 Bug 仓库；`workspaces/`：每次 Run 的独立代码状态。

## 页面使用

**M1 轨迹**：采用任务概览、可点击的 Step 列表和 Step 详情。初始化 System/User Prompt 单独折叠；每步按“LLM 决策 → Action / Tool Call → Observation”展示，多工具调用按 call_id 配对，无 ID 的旧记录按顺序配对。命令与输出各展示一次，用户干预与未匹配结果单独保留。长命令、日志默认折叠，日志可筛选、下载；原始消息与 Span 放在高级详情中。缺失耗时、Token 等不会补造，步骤结果显示“未记录”。步骤状态、Agent 结束状态与独立评测分别展示。代码 Diff 只来自记录中的 patch 或 Git，无法归属到某一步的 Diff 单独展示。Step 点击、前后导航与 URL 定位保持同步；旧数据库可直接浏览，无需重新导入。Playback 仅浏览记录，不执行命令。

**M2 监控**：分为“运行总览”和“单次运行详情”。总览展示任务、运行、提交、异常计数，以及已记录 Token、API 费用、平均运行耗时；每项资源统计注明运行和调用的覆盖范围，缺失值不会当作 0。按任务名称与运行序号选择记录后，可查看 Run → Step → LLM / Tool 调用链：展开 Step，再点击调用节点查看模型、Token、费用、耗时、命令及返回码。异常步骤与已记录耗时最长的步骤可定位到 M1；只保留逐步调用耗时与 Token / 费用分布图，历史数据不足时说明原因并省略图表。Run、Step 墙钟耗时与调用累计耗时分别统计，不相加；LLM 耗时为 model.query 的测量范围，包含其内部重试和解析。运行总费用不会分摊为虚构的逐次费用。原始 Trace、内部 ID 和 CSV/JSON 导出放在折叠的高级详情中。无需迁移或重新导入已有数据库，刷新记录即可读取其他进程写入的运行快照。

M1/M2 共用执行状态：返回码 0 为“正常退出”，非零且无执行器异常为“非零退出”，明确的执行器异常为“执行异常”，明确的跳过记录为“动作未执行”；无足够证据为“未记录”。非零退出只描述进程结果，不判断是否符合复现或调试预期。模型或 Action 解析异常单独标注。多动作步骤按执行异常、模型或解析异常、非零退出、未执行、提交、正常退出的优先级展示摘要；M2 筛选按每个调用的证据匹配，所以同一步可同时出现在“非零退出”和“执行异常”中。历史 Span 的旧 `error` 标签按实际返回码与异常信息重新解释，不需迁移数据库。Run 异常终止计数仅依据结束状态，任务是否解决仅依据独立评测。

**M3 失败分析**：按任务与运行选择，依次展示诊断概览、中文诊断列表和可展开详情；每条说明发生情况、触发依据和后续步骤，可跳转 M1 的对应 Step。原始诊断 JSON 放在高级详情。普通非零退出、单纯命令重复、相邻两次测试失败不单独产生诊断，执行结果仍完整保留在 M1/M2。概览与筛选分为确定异常、人工干预、行为风险、执行记录，互不混计。模型调用和解析异常、工具执行器异常及 Agent 异常终止属于确定异常；用户拒绝和明确人工中断属于人工干预；步数/费用/运行时间限制属于受控结束，中断原因不明和未注明原因的未执行动作只作执行记录。疑似无进展要求至少连续三步的同一命令、同一已记录工作目录、相同非零返回码与非空失败特征、每步只有该动作、完整前后代码快照一致且无人工介入，并明确标为启发式风险；同一连续片段合并为一条。缺少快照不会被当作代码未变化。每次读取运行时按当前规则重算诊断，旧数据库诊断行仍保留用于追溯，不参与当前统计。诊断、Agent 结束状态与任务独立评测分别展示。

**M4 任务与对比**：登记任务，选择配置启动 Agent；也能关联已有 Run、对保留工作区执行验证。选择任意 Run（同任务或跨任务），比较模型、配置、Prompt、初始版本、测试判定、耗时、步数、Token、Cost 与诊断分布，可保存 Experiment 并下载 CSV/JSON。没有评测的 Run 显示 `not_evaluated`；`Submitted` 永远不会自动变成 `passed`。

## 导入历史记录

```powershell
mini-observe import tests/test_data --db .mini-observe/traces.db
mini-observe import path/to/example.traj.json --task-id my-bug --db .mini-observe/traces.db
```

也可在侧栏批量上传。支持旧版消息数组、新版 `{info, messages}`、文本 bash 动作、原生 tool_calls 和 Responses API output。多动作使用 tool_call_id 配对；历史 XML/JSON observation 均可提取。导入以内容摘要生成稳定 Run ID，重复导入不会重复计数。原始 JSON 按字节保留。缺少开始/结束时间、返回码、Usage 或计费信息时保持 null，不用消息时间间隔估造耗时。顶层未知或格式损坏的文件会明确报错，不静默导入为成功记录。

可直接打开原 Inspector 查看保存的 `.traj.json`，新增 Trace 格式不替换它。

## 自定义真实任务

创建 `task.json`（将路径、commit SHA 和验证命令换成你的任务）：

```json
{
  "task_id": "my-bug",
  "title": "修复函数边界行为",
  "description": "请修复目标函数并验证负数、零和小数输入。",
  "repo_path": "C:/work/my-repo",
  "base_revision": "完整初始commit的SHA",
  "test_command": "python -m pytest -q",
  "source": "custom",
  "external_id": ""
}
```

```powershell
mini-observe task task.json
mini-observe run my-bug -c mini.yaml -c mini-api.yaml
mini-observe serve
```

配置使用原项目的配置解析和递归合并，模型/环境工厂也直接复用原实现。默认选 `DefaultAgent`；可在配置指定 `agent.agent_class=interactive` 等原有 Agent 类。原有 Prompt 按配置原样传入。默认每次从 `base_revision` 创建独立本地 Git clone，不修改源任务目录；不会复制未提交文件，因此测试和问题代码应在初始 commit 中。clone 的 Git hook 不随源仓库复制。

`--no-isolate` 显式允许在原目录运行；它不重置任何文件。评测前会检查该 Run 保存的末态，代码改变时拒绝拿新状态代替旧 Run 的结果。默认独立工作区更适合重复实验。隔离入口面向本地 Git 仓库；容器/远程环境应在其实际代码目录接入 `Collector` 并提供相应评测适配，不能把宿主机工作区的测试冒充远程结果。

```powershell
mini-observe evaluate RUN_ID --timeout 120
mini-observe compare RUN_ID_1 RUN_ID_2 --output comparison.csv
mini-observe compare RUN_ID_1 RUN_ID_2 --output comparison.json
```

运行配置不指定测试命令时保持 `not_evaluated`。测试正常退出码 0 为 `passed`，非零为 `failed`，启动异常/超时/工作区末态不匹配为 `evaluation_error`。测试完整输出、cwd、命令、退出码、时延和测试前后状态保存在独立证据文件。再次评测保留之前证据文件，数据库展示最新判定。

外部评测结果通过 `mini-observe evaluation-import evaluation.json` 登记。字段包括 `run_id`、`evaluator`、`verdict`、`test_summary`、`evidence_path`、`returncode`、`details`；passed/failed 必须附现有证据文件，该文件复制到 artifacts。为将来数据集接入保留 `Task.source/external_id` 和评测器来源，不依赖 SWE-bench。

## 接入代码与真实限制

新增文件集中在 `src/minisweagent/observability/` 与 `run/observe.py`。既有工程文件仅修改 `pyproject.toml`（可选 UI 依赖及 CLI 入口）和 `.gitignore`（忽略本地数据库、依赖与工作区）。没有修改 Agent、模型、环境、Prompt、Inspector 的源码；本仓库原先已有的未提交修改保留。

`ObservedAgentMixin` 仅包围原 `run()`、`step()`、`save()`；模型与环境代理原样传递调用和结果。Scope 记录异常后返回 False，原异常继续进入原 Agent 的处理路径。原始轨迹格式与用户指定的 output_path 仍可使用。

边界与口径：

- LLM Span 计量一次 `model.query()`，包含底层重试/解析开销。现有 API 抽象没有逐次 HTTP 重试回调，因此不声称具有每个 HTTP attempt 的独立 Span。
- 费用仅使用轨迹现有的正计费数据；原模型 `ignore_errors` 返回的 0 可能表示未知，因此保守显示 null。Usage 缺失不按字符数估算；已知值汇总附已知调用数，避免混淆部分覆盖。
- 环境在提交时会抛出 `Submitted`，没有返回 output dict；此工具 Span 的返回码/输出保持未知，状态为 submitted。其他命令的返回码与完整输出原样记录。
- 工作区快照适用于有初始提交且根目录对应的 Git 仓库，跟踪已追踪文件 Diff 和未忽略新文件内容摘要；不会解析 `.git` 内部内容，不跟踪被忽略的文件，也不覆盖嵌套子模块的内部变动。无快照时不编造 Diff 或自动评测判定。
- 配置快照隐藏常见密钥字段；原始轨迹按原项目序列化保留，可能含配置或命令中的敏感内容。数据库和 artifacts 按本地开发数据管理。
- 采集会增加计时/文件系统/SQLite 开销；故障不会被静默吞掉。没有把采集结果提供给 Agent 作决策。

## 验证

```powershell
python -m pytest tests/observability -q
python -m pytest tests/agents tests/models tests/run/test_inspector.py tests/run/test_save.py tests/config tests/utils -q
python -m ruff check src/minisweagent/observability src/minisweagent/run/observe.py tests/observability
```

新增测试使用实际旧轨迹、原有 DeterministicModel、原动作解析器、真实 LocalEnvironment / Git / pytest 和 Streamlit AppTest；不 mock 或 patch API、工具、存储或评测结果。覆盖原/观测 Agent 的 Prompt、消息、动作、代码修改、提交结果一致性，以及异常继续抛出、超时、独立工作区、未知字段、证据关联、对比导出和四个 UI 页面的操作。

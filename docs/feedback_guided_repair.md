# Feedback-Guided Repair 使用与实现

第三项优化在现有 Context Engineering 和 Repository-Level Retrieval 上增加测试反馈解析、当前 Issue 的失败记忆和事件触发式反思。Agent 循环、Bash 工具、模型接口、环境实现、提交和轨迹保存继续复用原有逻辑。

## 启用

`enhanced.yaml` 同时启用三项优化，叠加到现有完整配置即可：

```bash
mini -c default.yaml -c enhanced.yaml -m openai/gpt-4o -t "修复问题"
mini-extra swebench --subset verified --split test -c swebench.yaml -c enhanced.yaml -o runs/enhanced
```

直接构造增强类时传入 `feedback={"enabled": True}`。默认关闭以兼容此前仅使用上下文工程的调用方；配置可独立控制三项能力。

```yaml
agent:
  agent_class: enhanced
  feedback:
    enabled: true
    repeat_failure_threshold: 3
    test_command_patterns: []
    max_evidence_chars: 1600
    max_failures: 20
    max_attempts: 5
```

`repeat_failure_threshold` 是连续观察到同组失败后提示重新分析的阈值，不是执行次数或费用上限。`max_failures` 限制每次解析返回的失败条目及模型记忆中的失败记录数；超出时标记 `truncated`，不据此推断其他失败消失。`max_attempts` 控制每个失败保存的近期尝试和共享反思历史。完整执行输出、反馈运行历史和反思事件仍保存于轨迹，模型视图只保留有限证据摘录。

## 反馈解析

`DefaultAgent.process_action_output(action, output)` 是新增的默认原样返回钩子。原有 `execute_actions()` 在执行每个动作之后、调用原版 Observation formatter 之前调用它。`EnhancedAgent` 在此解析 `output`、`returncode`、`exception_info` 及环境异常类型，并把结构化结果附加到 `extra.test_feedback`；原始输出和返回码不变。

默认识别 pytest、`python -m unittest`、tox/nox、coverage、Django `manage.py test`、`runtests.py`、`setup.py test`、常见测试脚本和复现脚本，以及常见 env/uv/poetry/pipenv 包装和 `cd ... && ...`。不会执行或展开命令字符串；`echo pytest`、读取旧测试日志等不会仅凭文本内容成为一次测试运行。

自定义项目入口可以显式补充匹配实际命令的正则，例如：

```yaml
agent:
  feedback:
    enabled: true
    test_command_patterns:
      - 'python checks\.py(?:\s|$)'
```

解析以 pytest/unittest 文本格式为重点，提取失败节点、异常类型、断言、文件行号、通过节点和运行统计。其他测试工具仍可被识别为测试命令；输出缺少可确认的测试结果时保持 `inconclusive`。未知格式不会因非零退出而被强行判为代码失败。

| 情况 | 分类与结果 | 行为 |
| --- | --- | --- |
| `rg` 无匹配、普通命令非零退出 | `command / nonzero` | 不创建测试失败或反思 |
| pytest/unittest 报告断言、用例或收集失败 | `test / failed` | 记录失败证据，更新失败变化 |
| 有明确执行统计和成功证据 | `test / passed` | 只更新这次覆盖范围；不自动结束任务 |
| 未发现测试、仅跳过、缺少结果、未知输出 | `test / inconclusive` | 保留事实，不清除历史失败 |
| KeyboardInterrupt | `test / interrupted` | 不当作一次有效修复失败计数 |
| 环境抛出并报告的超时、信号终止、Docker 连接失败、缺少执行器 | `system_error / interrupted` 或 `inconclusive` | 提示检查环境，不推断源代码根因 |

导入依赖导致的 pytest 收集错误属于已观察到的项目测试失败；启动 `python -m pytest` 本身找不到 pytest 属于执行器问题。异常优先依据环境异常字段判定。Shell 掩盖测试退出码时，明确的失败报告仍是失败证据。

## Failure Memory

每个失败签名结合测试节点、异常类型及稳定断言/异常特征；规范化临时测试目录、地址、时间戳和堆栈行号，保留有意义的断言值。签名是启发式身份，不能证明两个失败具有相同根因。

每条记录保存首次/最近来源消息索引、出现次数、测试范围、近期修改关联及状态：

- `active`：最近相关结果仍观察到该失败。
- `not_observed`：同一范围的完整结果中不再观察到它，但不能确认它已通过。
- `resolved`：明确通过的节点，或同一范围的可信通过结果覆盖了原失败；仍只代表已执行的测试范围。

命令范围保留工作目录、测试选择条件和环境设置，仅合并 `-q/-v` 等输出形式差异。不同测试范围的成功不会清除其他范围的失败。早停、收集错误、结果截断或多次测试混在一个动作中时采用保守更新，不声称覆盖所有旧失败。

修改关联复用现有 Task State 的命令与 `git diff --name-only` 证据，带来源和状态；编辑命令成功不等于已确认补丁，更不等于根因正确。系统执行异常不增加失败次数，也不消除已有失败。每次 `EnhancedAgent.run()` 重建反馈管理器，历史不会跨 Issue 传播。

## Reflection 与上下文协作

触发条件包括连续同组失败没有改善、同一测试范围从通过变为失败，以及新的失败证据与 Agent 明确的“全部测试通过/问题已修复”声明冲突。自由文本的根因假设不会被启发式解析器自动认定为错误；声明和下一次反思响应都标记为 `unverified`。

反思事件记录来源、触发原因、此前假设、近期修改、失败变化和重新验证建议。提示要求下一轮正常模型调用重新审视假设，必要时用普通 Bash 或仓库检索检查节点、异常和位置。它不启动第二个模型、额外 Agent 或自动测试循环，也不强制每次修改后运行全量测试。

同组失败没有新信息时抑制重复事件；下一条正常助手消息作为该事件的未验证响应保存。如果同批工具结果随后出现系统异常，暂缓反思提示和响应关联，先处理环境问题，恢复测试后再使用仍然相关的反思。后续通过结果已消除相关失败时，尚未使用的反思会被标记为已被新结果取代。

Context Manager 先按现有规则关联 Action 与 Observation，再更新共享的 `test_feedback`、`failures`、`reflections`。模型视图末尾增加一个通过原版 `Model.format_message()` 构造的反馈消息，不改写原工具结果，也不拆开工具调用和返回的配对。原始轨迹中不插入伪造的工具消息。

反馈消息参与上下文容量计算、压缩与原始历史回退；检查点和任务记忆保留失败来源与关键事实。结构化数据来自格式化前的完整输出，因此长日志中间的失败不会因 Observation 模板的首尾截断而消失。模型当前请求的实际反馈消息记录在 `context_engineering.requests[].feedback_message` 中。

轨迹新增 `feedback_guided_repair`，保存配置、运行历史、失败记录、反思和待响应事件。原有可观测工具状态继续反映真实进程执行结果，测试语义通过 Observation 的 `extra.test_feedback` 和反馈状态单独保存；模型调用次数、费用和提交语义保持原有方式。

## 验证

`tests/agents/test_feedback_guided_repair.py` 覆盖常见命令及包装、pytest/unittest/收集/复现反馈、非零返回与系统异常、签名稳定性、范围与失败变化、重复反思抑制、反思响应、三种消息协议、多工具结果、长输出压缩、真实超时、任务重用及可观测链路。使用真实确定性模型和环境，不增加测试 mock/patch。

真实 Docker 测试使用已缓存的 SWE-bench 镜像，不自动拉取：

```bash
MSWEA_FEEDBACK_TEST_IMAGE=swebench/sweb.eval.x86_64.psf_1776_requests-2317:latest pytest tests/agents/test_feedback_guided_repair.py -q
```

测试只读取 Agent 实际执行得到的反馈；实现不读取隐藏测试、官方补丁或评测答案。

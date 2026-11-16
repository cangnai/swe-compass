# Repository-Level Retrieval 使用与实现

此模块对应第二项优化，在现有 `EnhancedAgent` 和 Context Engineering 上扩展仓库定位。继续使用原版 Agent 循环、模型接口、单 Bash Tool、`Environment.execute()` 和 Observation 格式。第三项[反馈驱动修复](feedback_guided_repair.md)可利用检索来进一步验证测试反馈提供的定位线索。

## 启用

`enhanced.yaml` 叠加配置同时启用上下文工程、仓库检索和反馈驱动修复：

```bash
mini -c default.yaml -c enhanced.yaml -m openai/gpt-4o -t "修复问题"
mini-extra swebench --subset verified --split test -c swebench.yaml -c enhanced.yaml -o runs/enhanced
```

直接实例化 `EnhancedAgent` 时，设置 `retrieval={"enabled": True}`。检索配置默认关闭，以兼容已有仅使用 Context Engineering 的调用方；`enhanced.yaml` 显式开启。原版 `DefaultAgent` 不部署工具，也不修改执行环境。

```yaml
agent:
  agent_class: enhanced
  retrieval:
    enabled: true
    python: python
    top_k: 5
    snippet_lines: 24
    candidate_limit: 40
    max_file_bytes: 1000000
```

`python` 是任务环境中的 Python 命令或解释器路径。其他参数分别限制返回片段数、每段行数、进入符号定位的候选文件数和单文件字节数。可用 `-c agent.retrieval.enabled=false` 保留仅上下文工程的运行方式。

## Bash 调用与执行环境

每次任务开始时，通过所选 `Environment.execute()` 将压缩的独立脚本写入该环境的临时工具目录，并把完整调用命令加入系统提示。SWE-bench 启动命令执行完成后才创建并运行 Agent，因此部署与搜索使用同一个容器、同一份工作树。无需修改 Docker 镜像、环境实现或 benchmark 调度器。

模型按需从 Bash 调用系统提示中的命令，例如：

```bash
python -B /tmp/mini-repo-search-XXXX/repo_search.py 'Parser.decode_payload traceback src/parser.py:42'
```

实际命令自带配置参数。`--root` 默认是该次命令的工作目录，可显式指定 `/testbed` 或另一个目录。模型仍可自由使用 `rg`、`grep`、`sed`、测试与编辑命令，Top-K 只是此次返回范围。查询以独立参数传给检索器，内部搜索用参数列表和固定字符串匹配执行，不将查询拼成 Shell。

脚本只依赖 Python 标准库，容器不需要安装 mini-SWE-agent、Typer、Tree-sitter 或向量数据库。容器入口使用标准库参数解析；宿主机的独立入口使用 Typer：

```bash
python -m minisweagent.run.utilities.repo_search 'EnhancedAgent ContextManager' --root . --top-k 5
```

工具目录经过仓库边界检查，脚本和临时工具不写入目标工作树。没有持久索引或结果缓存，容器销毁时工具目录随之销毁；本地运行的目录保留在系统临时目录中。部署失败时，提示模型继续使用普通 Bash，并在轨迹中保留失败输出。

## 分层检索

1. **文件定位**：优先使用 `rg --files`，从查询中的文件路径、点分模块名、文件名、Python traceback、`path:line` 和关键词收集候选。排除版本控制目录、虚拟环境、构建产物、依赖目录和二进制文件。Git 忽略规则由 ripgrep 或回退的 `git ls-files --exclude-standard` 处理。两者均不可用时使用支持基本忽略模式的文件遍历，并明确提示其限制。
2. **符号定位**：对候选 Python 文件按需解析 AST，保留类、函数、异步函数、方法、嵌套定义和装饰器覆盖的起止行。明确提及的测试文件中的调用名称用于关联源码定义。其他语言和 Python 解析失败时保留文本定位，不增加解析器依赖。
3. **混合排序**：组合文件路径/模块、文件名、关键词覆盖、测试引用、精确符号和 traceback 行信号。路径与堆栈比宽泛关键词具有更高权重，精确符号定义优先于一般文本引用。重复出现同一关键词不额外累加分数。每个结果附有可读的命中原因，不将排序分数解释为根因置信度。
4. **片段返回**：按相关行选择有限上下文，标明相对路径、片段与符号行范围、符号名称和命中理由；同一文件内相邻或重叠片段去重。单行输出也有长度上限，避免生成文件撑大 Observation。

每次搜索重新枚举当前文件、读取当前内容并解析候选 AST，因此创建、删除、修改和语法暂时无效的文件都会反映在下一次调用中。结果记录内容摘要 `revision`，便于区分不同版本的定位证据。旧轨迹始终是当时的观测，修改后应重新检索或读取文件。

## Context Engineering 与可观测性

检索输出是普通 Bash Observation；原始命令、输出、返回码和工具调用 ID 沿用现有格式。输出末尾的 `REPO_SEARCH_METADATA` 提供结构化证据，Task State 将候选文件、符号、范围、命中理由、内容摘要和原始消息索引记录到 `files`、`symbols`、`localizations`。这些记录标记为 `candidate`，不自动判定 Bug 根因。

同一文件的新定位替换该文件旧的检索符号和定位记录。同一仓库、同一查询重新检索时，会移除该查询之前返回但本次不再返回的候选记忆，包括无匹配的情况。重复的简单检索命令可被原有历史清理机制识别；仅清理模型可见视图，原始 Observation 保留在完整轨迹中。压缩和任务记忆继续由现有 Context Manager 管理，覆盖文本动作、Chat Completions 工具调用和 Responses 工具调用的配对。

轨迹新增 `repository_retrieval`，保存配置、实际调用命令和部署结果。已有可观测环境包装器也会记录真实部署命令，因此启用检索后每次任务会增加一次工具部署记录；它不产生额外模型调用或 API 费用。

## 验证

`tests/agents/test_repository_retrieval.py` 覆盖混合排序、嵌套/异步/装饰器 AST 边界、测试引用、片段去重、文件变更刷新、语法失败与多语言回退、无 ripgrep/Git 回退、二进制与大文件、字面查询安全、本地部署、三种消息协议、上下文清理及可观测链路。

真实 Docker 测试只使用已有镜像，避免测试时自动拉取大型 SWE-bench 镜像：

```bash
MSWEA_RETRIEVAL_TEST_IMAGE=swebench/sweb.eval.x86_64.psf_1776_requests-2317:latest pytest tests/agents/test_repository_retrieval.py -q
```

没有可访问 Docker 或指定镜像时，该项测试明确跳过。

# mini-SWE-agent 概述

- mini-SWE-agent 实现了一个 AI 软件工程智能体，用于解决 GitHub issue 以及类似的编程挑战。
- 本项目的理念是编写最简单、最精简、最易读的智能体。

项目结构如下：

```bash
minisweagent/__init__  # 所有基类的协议/接口
minisweagent/agents  # 智能体的控制流程与循环
minisweagent/environments  # 执行智能体的动作
minisweagent/models  # 语言模型接口
minisweagent/run  # 作为入口的运行脚本
```

- 本项目采用多态设计：每个类都应保持简单，同时提供不同的实现供选择。
- 每个使用场景都应从一个运行脚本开始，由该脚本选择一个智能体类、一个环境类和一个模型类来运行。

# 代码风格指南

1. 面向 Python 3.10 或更高版本。
2. 使用类型注解。使用 `list`，而不是 `List`。
3. 使用 `pathlib`，而不是 `os.path`。优先使用 `Path.read_text()`，而不是 `with ...open()` 结构。
4. 使用 `typer` 添加命令行接口。
5. 尽量少写代码注释，仅对逻辑上特别难理解的部分进行说明。
6. 除非明确要求，否则不要向 README 追加内容。
7. 使用 `jinja` 格式化模板。
8. 使用 `dataclass` 管理配置。
9. 除非明确要求，否则不要捕获异常。
10. 编写简洁、短小、精简的代码。
11. 大多数情况下，避免仅为了将变量传给函数而初始化变量。应直接将表达式传给函数。
12. 并非每个异常都必须捕获。异常是向用户展示问题的一种有效方式。
13. 本仓库推崇精简的代码，请尽可能保持简洁。

第 11 条的示例：

```python
# 不推荐
a = func()
Class(a)

# 推荐
Class(func())
```

## 测试风格

1. 使用 `pytest`，而不是 `unittest`。
2. <IMPORTANT>除非明确要求，否则不要对任何内容进行 mock/patch（模拟或替换）。</IMPORTANT>
3. 避免编写无实质意义的测试。每个测试应至少覆盖一个可能出错的点，最好覆盖多个。
4. 避免像这样将代码拆成多行：`a=func()\n assert a=b`。应直接写成 `assert func() == b`。
5. `pytest.mark.parametrize` 的第一个参数应为元组（不能是字符串，也不能是列表！），第二个参数必须是列表（不能是元组！）。

第 4 条的示例：

```python
# 不推荐
result = func()
assert result == b

# 推荐
assert func() == b
```

# 提交信息

提交信息使用以下格式：

- `ci: description`：用于所有与测试相关的变更，以及 GitHub 工作流等变更。
- `dev: description`：用于开发相关的变更，包括更新 Cursor 或 Claude 规则。
- `fix(component): description`：用于修复缺陷。
- `feat(component): description`：用于新增功能。
- `enh(component): description`：用于功能增强。
- `docs: description`：用于文档变更。
- `ref(component): description`：用于重构。
- `chore: description`：用于维护任务（pre-commit 钩子、导入等）。

其中，`description` 表示变更描述，`component` 表示组件名称。通常，描述应侧重于变更的意图，而不是实现细节。

## 风格说明

<IMPORTANT>**不要**在提交信息或其尾注中添加 `Co-authored-by: Cursor` 行。</IMPORTANT>

## 审查

准备提交信息时，请指出提交前应解决的严重问题。不要指出风格问题或细微变更。

请指出以下问题：

- 任何可能意外引发未处理异常的情况。
- 任何看起来逻辑错误或前后不一致的情况。
- 对协议/接口进行了破坏性变更，却未相应更新实现。

将以下风格问题标记为轻微问题：

- 导入语句不在文件顶部。

## 组件

在 `fix`、`feat`、`enh` 和 `ref` 类型的提交中，括号内使用以下组件名称：

- `models`：模型接口的变更（litellm、anthropic、openai、portkey、openrouter）。
- `agents`：智能体类的变更（default、interactive、multimodal）。
- `env`：环境的变更（docker、local、singularity、bubblewrap、swerex）。
- `config`：配置文件或配置处理的变更。
- `run`：运行脚本的变更（mini、hello_world）。
- `benchmarks`：基准测试运行器的变更（swebench、inspector）。
- `cli`：命令行参数处理的变更。
- `deps`：依赖更新。

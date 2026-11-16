#!/usr/bin/env python3

"""Run mini-SWE-agent in your local environment. This is the default executable `mini`."""
# 在本地环境中运行 mini-SWE-agent。这是默认命令 mini 的入口。
# Read this first: https://mini-swe-agent.com/latest/usage/mini/  (usage)

import os
from pathlib import Path
from typing import Any

import typer
from rich.console import Console

from minisweagent import global_config_dir
from minisweagent.agents import get_agent
from minisweagent.agents.utils.prompt_user import _multiline_prompt
from minisweagent.config import builtin_config_dir, get_config_from_spec
from minisweagent.environments import get_environment
from minisweagent.models import get_model
from minisweagent.run.utilities.config import configure_if_first_time
from minisweagent.utils.serialize import UNSET, recursive_merge

DEFAULT_CONFIG_FILE = Path(os.getenv("MSWEA_MINI_CONFIG_PATH", builtin_config_dir / "mini.yaml"))
DEFAULT_OUTPUT_FILE = global_config_dir / "last_mini_run.traj.json"


_HELP_TEXT = """在本地环境中运行 mini-swe-agent。

[not dim]
使用说明：[bold green]https://mini-swe-agent.com/latest/usage/mini/[/bold green]
[/not dim]
"""
# 在本地环境中运行 mini-SWE-agent。
# 更多使用说明：https://mini-swe-agent.com/latest/usage/mini/

_CONFIG_SPEC_HELP_TEXT = """配置文件路径、文件名或键值对。

[bold red]注意：[/bold red][red]设置此选项后，默认配置文件不会自动加载。[/red]
请显式指定默认配置，例如：[bold green]-c mini.yaml <其他配置>[/bold green]

多份配置将按顺序递归合并。

示例：

[bold red]-c model.model_kwargs.temperature=0[/bold red] [red]此写法遗漏了默认配置文件。[/red]

[bold green]-c mini.yaml -c model.model_kwargs.temperature=0.5[/bold green]

[bold green]-c swebench.yaml agent.mode=yolo[/bold green]
"""
# 配置文件路径、文件名或键值对。
# 重要：设置此选项后，将不再使用默认配置文件。
# 因此需要显式指定默认配置文件，例如：-c mini.yaml <其他选项>。
# 多份配置将以递归方式合并。
# 示例：
# -c model.model_kwargs.temperature=0
# 上例遗漏了默认配置文件！请参阅上方说明。
# -c mini.yaml -c model.model_kwargs.temperature=0.5
# -c swebench.yaml agent.mode=yolo

console = Console(highlight=False)
app = typer.Typer(rich_markup_mode="rich")


# fmt: off
@app.command(help=_HELP_TEXT)
def main(
    model_name: str | None = typer.Option(None, "-m", "--model", help="使用的模型名称",),
    model_class: str | None = typer.Option(None, "--model-class", help="模型类，例如 'litellm' 或完整导入路径", rich_help_panel="高级选项"),
    agent_class: str | None = typer.Option(None, "--agent-class", help="智能体类，例如 'interactive' 或完整导入路径", rich_help_panel="高级选项"),
    environment_class: str | None = typer.Option(None, "--environment-class", help="执行环境类，例如 'local' 或完整导入路径", rich_help_panel="高级选项"),
    task: str | None = typer.Option(None, "-t", "--task", help="任务或问题描述", show_default=False),
    yolo: bool = typer.Option(False, "-y", "--yolo", help="自动执行命令，不逐步确认"),
    cost_limit: float | None = typer.Option(None, "-l", "--cost-limit", help="费用上限（美元），设为 0 表示不限制"),
    config_spec: list[str] = typer.Option([str(DEFAULT_CONFIG_FILE)], "-c", "--config", help=_CONFIG_SPEC_HELP_TEXT),
    output: Path | None = typer.Option(DEFAULT_OUTPUT_FILE, "-o", "--output", help="运行记录的保存路径"),
    exit_immediately: bool = typer.Option(False, "--exit-immediately", help="任务完成后直接退出，不再询问是否继续", rich_help_panel="高级选项"),
) -> Any:
    # fmt: on
    configure_if_first_time()

    # Build the config from the command line arguments
    console.print(f"正在合并运行配置：[bold green]{config_spec}[/bold green]")
    configs = [get_config_from_spec(spec) for spec in config_spec]
    configs.append({
        "run": {
            "task": task or UNSET,
        },
        "agent": {
            "agent_class": agent_class or UNSET,
            "mode": "yolo" if yolo else UNSET,
            "cost_limit": cost_limit if cost_limit is not None else UNSET,
            "confirm_exit": False if exit_immediately else UNSET,
            "output_path": output or UNSET,
        },
        "model": {
            "model_class": model_class or UNSET,
            "model_name": model_name or UNSET,
        },
        "environment": {
            "environment_class": environment_class or UNSET,
        },
    })
    config = recursive_merge(*configs)

    if (run_task := config.get("run", {}).get("task", UNSET)) is UNSET:
        console.print("[bold yellow]请输入你想完成的任务：")
        run_task = _multiline_prompt()
        console.print("[bold green]已收到任务，开始处理。[/bold green]")

    model = get_model(config=config.get("model", {}))
    env = get_environment(config.get("environment", {}), default_type="local")
    agent = get_agent(model, env, config.get("agent", {}), default_type="interactive")
    agent.run(run_task)
    if (output_path := config.get("agent", {}).get("output_path")):
        console.print(f"运行记录已保存到：[bold green]'{output_path}'[/bold green]")
    return agent


if __name__ == "__main__":
    app()

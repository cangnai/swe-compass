#!/usr/bin/env python3

"""管理全局配置文件。

也可以直接编辑配置目录中的 `.env` 文件。

文件位置：[bold green]{global_config_file}[/bold green]。
"""

import os
import subprocess

from dotenv import load_dotenv, set_key, unset_key
from rich.console import Console
from rich.rule import Rule
from typer import Argument, Typer

from minisweagent import global_config_file


def _reload_config():
    load_dotenv(dotenv_path=global_config_file, override=True)


app = Typer(
    help=__doc__.format(global_config_file=global_config_file),  # type: ignore
    no_args_is_help=True,
    rich_markup_mode="rich",
    add_completion=False,
)
console = Console(highlight=False)


_SETUP_HELP = """首次使用需要设置全局配置。

你可以手动编辑配置文件，也可以使用 [bold green]mini-extra config set[/bold green] 或 [bold green]mini-extra config edit[/bold green] 命令。

接下来请填写模型名称和 API 密钥。

以下是模型名称及其对应的密钥变量示例：

[bold green]anthropic/claude-opus-4-6-20260205[/bold green] ([bold green]ANTHROPIC_API_KEY[/bold green])
[bold green]openai/gpt-5.4[/bold green] 或 [bold green]openai/gpt-5.4-mini[/bold green] ([bold green]OPENAI_API_KEY[/bold green])
[bold green]gemini/gemini-3-pro-preview[/bold green] ([bold green]GEMINI_API_KEY[/bold green])

[bold]注意：模型名称应包含提供商前缀，例如 "openai/"。[/bold]

[bold yellow]任何选项都可以留空跳过。[/bold yellow]

使用说明：https://mini-swe-agent.com/latest/quickstart/
模型排行榜：https://swebench.com/
"""


def prompt(*args, **kwargs):
    # Defer import to avoid slow import module
    from prompt_toolkit.shortcuts.prompt import prompt as _prompt

    return _prompt(*args, **kwargs)


def configure_if_first_time():
    if not os.getenv("MSWEA_CONFIGURED"):
        console.print(Rule())
        setup()
        console.print(Rule())


@app.command()
def setup():
    """设置全局配置文件。"""
    console.print(_SETUP_HELP.format(global_config_file=global_config_file))
    default_model = prompt(
        "请输入默认模型名称（例如 openai/gpt-6.1-sol）：",
        default=os.getenv("MSWEA_MODEL_NAME", ""),
    ).strip()
    if default_model:
        set_key(global_config_file, "MSWEA_MODEL_NAME", default_model)
    console.print(
        "[bold yellow]如果已经通过环境变量设置密钥，可以跳过接下来的密钥设置。[/bold yellow]"
    )
    key_name = prompt("请输入密钥变量名（例如 OPENAI_API_KEY）：").strip()
    key_value = None
    if key_name:
        key_value = prompt("请输入 API 密钥（例如 sk-...）：", default=os.getenv(key_name, "")).strip()
        if key_value:
            set_key(global_config_file, key_name, key_value)
    if not key_value:
        console.print(
            "[bold red]尚未填写 API 密钥。[/bold red]如果已通过环境变量设置密钥，可以继续使用。"
        )
    set_key(global_config_file, "MSWEA_CONFIGURED", "true")
    _reload_config()
    console.print(
        "\n[bold yellow]配置完成。[/bold yellow]如需重新设置，请运行 [bold green]mini-extra config setup[/bold green]。"
    )


@app.command()
def set(
    key: str | None = Argument(None, help="要设置的变量名"),
    value: str | None = Argument(None, help="要设置的值"),
):
    """设置全局配置中的变量。"""
    if key is None:
        key = prompt("请输入要设置的变量名：")
    if value is None:
        value = prompt(f"请输入 {key} 的值：")
    set_key(global_config_file, key, value)
    _reload_config()


@app.command()
def unset(key: str | None = Argument(None, help="要删除的变量名")):
    """删除全局配置中的变量。"""
    if key is None:
        key = prompt("请输入要删除的变量名：")
    unset_key(global_config_file, key)
    _reload_config()


@app.command()
def edit():
    """编辑全局配置文件。"""
    editor = os.getenv("EDITOR", "nano")
    subprocess.run([editor, global_config_file])
    _reload_config()


if __name__ == "__main__":
    app()

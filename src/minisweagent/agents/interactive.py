"""A small generalization of the default agent that puts the user in the loop.

There are three modes:
- human: commands issued by the user are executed immediately
- confirm: commands issued by the LM but not whitelisted are confirmed by the user
- yolo: commands issued by the LM are executed immediately without confirmation
"""
# 对默认智能体做少量扩展，让用户参与执行过程。
# 提供三种模式：
# - human：立即执行用户发出的命令。
# - confirm：语言模型发出的命令若不在白名单内，需经用户确认后执行。
# - yolo：立即执行语言模型发出的命令，无需确认。

import re
import sys
from typing import Literal, NoReturn

from rich.console import Console
from rich.rule import Rule

from minisweagent.agents.default import AgentConfig, DefaultAgent
from minisweagent.agents.utils.prompt_user import _multiline_prompt, prompt_session
from minisweagent.exceptions import LimitsExceeded, Submitted, TimeExceeded, UserInterruption
from minisweagent.models.utils.content_string import get_content_string

console = Console(highlight=False)


class InteractiveAgentConfig(AgentConfig):
    mode: Literal["human", "confirm", "yolo"] = "confirm"
    """Whether to confirm actions."""
    # 是否需要确认动作。
    whitelist_actions: list[str] = []
    """Never confirm actions that match these regular expressions."""
    # 匹配这些正则表达式的动作无需确认。
    confirm_exit: bool = True
    """If the agent wants to finish, do we ask for confirmation from user?"""
    # 智能体准备结束时，是否向用户请求确认？


class InteractiveAgent(DefaultAgent):
    _MODE_COMMANDS_MAPPING = {"/u": "human", "/c": "confirm", "/y": "yolo"}
    _MODE_LABELS = {"human": "人工操作", "confirm": "逐步确认", "yolo": "自动执行"}
    _ROLE_LABELS = {
        "system": "系统提示词",
        "user": "用户任务",
        "tool": "命令执行结果",
        "function_call_output": "命令执行结果",
        "exit": "最终答复",
        "unknown": "消息",
    }

    def __init__(self, *args, config_class=InteractiveAgentConfig, **kwargs):
        super().__init__(*args, config_class=config_class, **kwargs)
        self.cost_last_confirmed = 0.0

    def _interrupt(self, content: str, *, itype: str = "UserInterruption") -> NoReturn:
        raise UserInterruption({"role": "user", "content": content, "extra": {"interrupt_type": itype}})

    def add_messages(self, *messages: dict) -> list[dict]:
        # Extend supermethod to print messages
        # 扩展父类方法，增加消息打印功能。
        for msg in messages:
            role = msg.get("role") or ("assistant" if msg.get("object") == "response" else msg.get("type", "unknown"))
            content = get_content_string(msg)
            if role == "assistant":
                console.print(
                    f"\n[red][bold]mini-swe-agent[/bold]（第 [bold]{self.n_calls}[/bold] 步，费用 [bold]${self.cost:.2f}[/bold]）：[/red]\n",
                    end="",
                    highlight=False,
                )
            else:
                console.print(f"\n[bold green]{self._ROLE_LABELS.get(role, role)}[/bold green]：\n", end="", highlight=False)
            console.print(content, highlight=False, markup=False)
        return super().add_messages(*messages)

    def query(self) -> dict:
        # Extend supermethod to handle human mode
        # 扩展父类方法，支持 human 模式。
        if self.config.mode == "human":
            match command := self._prompt_and_handle_slash_commands("[bold yellow]>[/bold yellow] "):
                case "/y" | "/c":
                    pass
                case _:
                    msg = {
                        "role": "user",
                        "content": f"用户命令：\n```bash\n{command}\n```",
                        "extra": {"actions": [{"command": command}]},
                    }
                    self.add_messages(msg)
                    return msg
        try:
            with console.status("正在等待模型回复……"):
                return super().query()
        except TimeExceeded:
            # A wall-clock limit can't be lifted by raising the step/cost limits
            # (the next query re-checks the clock and raises again), so prompting
            # would loop forever. Always stop cleanly instead.
            # 提高步数或费用上限无法解除实际运行时间限制，
            # 因为下次查询会再次检查时间并抛出异常；继续提示用户会导致无限循环。
            # 因此始终正常终止运行。
            raise
        except LimitsExceeded:
            if not self._stdin_is_interactive():
                # No terminal to prompt for new limits -- e.g. an unattended
                # `--yolo` run, or any run with stdin redirected from /dev/null
                # (CI, a sandbox). Stop cleanly so the trajectory is saved with a
                # LimitsExceeded exit status, instead of crashing on EOFError when
                # reading input.
                # 没有终端可供用户输入新的限制，例如无人值守的 --yolo 运行，
                # 或标准输入重定向自 /dev/null 的运行（如 CI 或沙箱）。
                # 正常终止，让执行轨迹以 LimitsExceeded 退出状态保存，
                # 避免读取输入时因 EOFError 而崩溃。
                raise
            console.print(
                f"已达到运行上限：最多 {self.config.step_limit} 步，费用 ${self.config.cost_limit}。\n"
                f"当前已执行 {self.n_calls} 步，费用 ${self.cost:.2f}。"
            )
            self.config.step_limit = int(input("请输入新的步数上限："))
            self.config.cost_limit = float(input("请输入新的费用上限（美元）："))
            return super().query()

    @staticmethod
    def _stdin_is_interactive() -> bool:
        """Whether an interactive terminal is available to prompt the user.

        Returns False for unattended runs (e.g. `--yolo` in CI, or inside a
        sandbox with stdin redirected from /dev/null), where calling ``input()``
        would raise ``EOFError`` and crash the run.
        """
        # 是否有可用于向用户请求输入的交互式终端。
        # 无人值守运行时返回 False，例如在 CI 中使用 --yolo，
        # 或在沙箱内将标准输入重定向自 /dev/null；这些情况下调用 input()
        # 会抛出 EOFError，导致运行异常终止。
        try:
            return sys.stdin is not None and sys.stdin.isatty()
        except (ValueError, OSError):
            return False

    def step(self) -> list[dict]:
        # Override the step method to handle user interruption
        # 重写 step 方法，处理用户中断。
        try:
            console.print(Rule())
            return super().step()
        except KeyboardInterrupt:
            interruption_message = self._prompt_and_handle_slash_commands(
                "\n\n[bold yellow]运行已中断。[/bold yellow] "
                "[green]请输入补充说明或命令[/green]（输入 /h 查看帮助）"
                "\n[bold yellow]>[/bold yellow] "
            ).strip()
            if not interruption_message or interruption_message in self._MODE_COMMANDS_MAPPING:
                interruption_message = "已处理临时中断。"
            self._interrupt(f"用户中断了运行：{interruption_message}")

    def execute_actions(self, message: dict) -> list[dict]:
        # Override to handle user confirmation and confirm_exit, with try/finally to preserve partial outputs
        # 重写此方法，处理用户确认和 confirm_exit，并通过 try/finally 保留已产生的部分输出。
        actions = message.get("extra", {}).get("actions", [])
        commands = [action["command"] for action in actions]
        outputs = []
        try:
            self._ask_confirmation_or_interrupt(commands)
            for action in actions:
                outputs.append(self.env.execute(action))
        except Submitted as e:
            outputs.append({"output": e.messages[0].get("content", ""), "returncode": 0, "exception_info": ""})
            self._check_for_new_task_or_submit(e)
        finally:
            result = self.add_messages(
                *self.model.format_observation_messages(message, outputs, self.get_template_vars())
            )
        return result

    def _add_observation_messages(self, message: dict, outputs: list[dict]) -> list[dict]:
        return self.add_messages(*self.model.format_observation_messages(message, outputs, self.get_template_vars()))

    def _check_for_new_task_or_submit(self, e: Submitted) -> NoReturn:
        """Check if user wants to add a new task or submit."""
        # 检查用户是想添加新任务，还是提交当前结果。
        if self.config.confirm_exit:
            message = (
                "[bold yellow]任务即将结束。[/bold yellow] "
                "[bold green]输入新任务可继续[/bold green]，按[bold]回车[/bold]结束 "
                "（输入 [bold]/h[/bold] 查看帮助）\n"
                "[bold yellow]>[/bold yellow] "
            )
            user_input = self._prompt_and_handle_slash_commands(message).strip()
            if user_input == "/u":  # directly continue
                # 直接继续执行。
                self._interrupt("已切换到人工操作模式。")
            elif user_input in self._MODE_COMMANDS_MAPPING:  # ask again
                # 再次询问用户。
                return self._check_for_new_task_or_submit(e)
            elif user_input:
                self._interrupt(f"用户添加了新任务：{user_input}", itype="UserNewTask")
        raise e

    def _should_ask_confirmation(self, action: str) -> bool:
        return self.config.mode == "confirm" and not any(re.match(r, action) for r in self.config.whitelist_actions)

    def _ask_confirmation_or_interrupt(self, commands: list[str]) -> None:
        if not any(self._should_ask_confirmation(c) for c in commands):
            return
        prompt = (
            f"[bold yellow]是否执行 {len(commands)} 条命令？[/] [green]按[bold]回车[/]确认[/]，"
            "[red]输入[bold]意见[/]拒绝执行[/]，或输入 [blue][bold]/h[/] 查看帮助[/]\n"
            "[bold yellow]>[/bold yellow] "
        )
        match user_input := self._prompt_and_handle_slash_commands(prompt).strip():
            case "" | "/y":
                pass  # confirmed, do nothing
                # 已确认，无需额外处理。
            case "/u":  # Skip execution action and get back to query
                # 跳过动作执行，返回查询阶段。
                self._interrupt("命令未执行，正在切换到人工操作模式。", itype="UserRejection")
            case _:
                self._interrupt(
                    f"命令未执行，用户拒绝执行并提出以下意见：{user_input}",
                    itype="UserRejection",
                )

    def _prompt_and_handle_slash_commands(self, prompt: str, *, _multiline: bool = False) -> str:
        """Prompts the user, takes care of /h (followed by requery) and sets the mode. Returns the user input."""
        # 提示用户输入，处理 /h（显示帮助后重新提示输入）并设置模式，返回用户输入。
        console.print(prompt, end="")
        if _multiline:
            return _multiline_prompt()
        user_input = prompt_session.prompt("")
        if user_input == "/m":
            return self._prompt_and_handle_slash_commands(prompt, _multiline=True)
        if user_input == "/h":
            console.print(
                f"当前模式：[bold green]{self._MODE_LABELS[self.config.mode]}[/bold green]\n"
                "[bold green]/y[/bold green] 切换到[bold yellow]自动执行[/bold yellow]模式（执行模型命令前不再确认）\n"
                "[bold green]/c[/bold green] 切换到[bold yellow]逐步确认[/bold yellow]模式（执行前请求确认）\n"
                "[bold green]/u[/bold green] 切换到[bold yellow]人工操作[/bold yellow]模式（执行你输入的命令）\n"
                "[bold green]/m[/bold green] 输入多行补充说明",
            )
            return self._prompt_and_handle_slash_commands(prompt)
        if user_input in self._MODE_COMMANDS_MAPPING:
            if self.config.mode == self._MODE_COMMANDS_MAPPING[user_input]:
                return self._prompt_and_handle_slash_commands(
                    f"[bold red]当前已经是{self._MODE_LABELS[self.config.mode]}模式。[/bold red]\n{prompt}"
                )
            self.config.mode = self._MODE_COMMANDS_MAPPING[user_input]
            console.print(f"已切换到[bold green]{self._MODE_LABELS[self.config.mode]}[/bold green]模式。")
            return user_input
        return user_input

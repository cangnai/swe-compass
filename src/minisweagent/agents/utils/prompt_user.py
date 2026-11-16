from prompt_toolkit.formatted_text.html import HTML
from prompt_toolkit.history import FileHistory
from prompt_toolkit.shortcuts import PromptSession

from minisweagent import global_config_dir

_history = FileHistory(global_config_dir / "interactive_history.txt")
prompt_session = PromptSession(history=_history)
_multiline_prompt_session = PromptSession(history=_history, multiline=True)


def _multiline_prompt() -> str:
    return _multiline_prompt_session.prompt(
        "",
        bottom_toolbar=HTML(
            "提交消息：<b fg='yellow' bg='black'>先按 Esc，再按回车</b> | "
            "浏览历史：<b fg='yellow' bg='black'>上/下方向键</b> | "
            "搜索历史：<b fg='yellow' bg='black'>Ctrl+R</b>"
        ),
    )

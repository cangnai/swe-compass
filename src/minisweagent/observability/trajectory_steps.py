"""A lossless presentation model shared by historical imports and runtime traces."""

import json
import re
from dataclasses import dataclass, field

from minisweagent.observability.execution_status import tool_status
from minisweagent.observability.schema import Run, Span

COMMAND_BLOCK = re.compile(r"```(?:mswea_bash_command|bash|sh)[ \t]*\r?\n(.*?)```", re.DOTALL)


def text_content(value: str | list | dict | None) -> str:
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        return text_content(value.get("text") or value.get("content"))
    return "\n\n".join(
        text_content(item)
        for item in value or []
        if isinstance(item, dict) and item.get("type") not in {"tool_use", "tool_result"}
    )


def response_text(message: dict) -> str:
    parts = [text_content(message.get("content"))]
    parts.extend(
        text_content(item.get("content"))
        for item in message.get("output", []) or []
        if isinstance(item, dict) and item.get("type") == "message"
    )
    return "\n\n".join(p for p in parts if p)


def is_assistant(message: dict) -> bool:
    return (
        message.get("role") == "assistant"
        or message.get("object") == "response"
        or bool(message.get("extra", {}).get("actions"))
    )


def grouped_indices(messages: list[dict]) -> list[list[int]]:
    groups: list[list[int]] = [[]]
    for i, message in enumerate(messages):
        if is_assistant(message) or message.get("extra", {}).get("interrupt_type") == "FormatError":
            groups.append([])
        groups[-1].append(i)
    return groups


@dataclass
class Observation:
    output: str | None = None
    stdout: str | None = None
    stderr: str | None = None
    returncode: int | None = None
    exception: str | None = None
    exception_type: str | None = None
    call_id: str | None = None
    message_index: int | None = None


def observation(message: dict) -> dict:
    extra = message.get("extra", {})
    content = message.get("content", message.get("output", ""))
    text = text_content(content)
    data = content if isinstance(content, dict) else {}
    if isinstance(content, str) and content.lstrip().startswith("{"):
        try:
            decoded = json.loads(content)
        except json.JSONDecodeError:
            decoded = None
        if isinstance(decoded, dict):
            data = decoded
    rc = re.search(r"<returncode>\s*(-?\d+)\s*</returncode>", text)
    output = re.search(r"<output>\n?(.*?)</output>", text, re.DOTALL)
    exception = re.search(r"<exception>\s*(.*?)</exception>", text, re.DOTALL)
    return {
        "returncode": extra.get("returncode", data.get("returncode", int(rc[1]) if rc else None)),
        "output": extra.get(
            "raw_output",
            data.get("output", output[1] if output else None if {"stdout", "stderr"} & data.keys() else text),
        ),
        "stdout": extra.get("stdout", data.get("stdout")),
        "stderr": extra.get("stderr", data.get("stderr")),
        "exception_info": extra.get("exception_info", data.get("exception_info", exception[1] if exception else None)),
        "exception_type": extra.get(
            "exception_type", data.get("exception_type", (data.get("extra") or {}).get("exception_type"))
        ),
        "tool_call_id": message.get("tool_call_id", message.get("call_id")),
    }


def make_observation(data: dict, index: int | None = None) -> Observation:
    return Observation(
        **{key: data.get(key) for key in ("output", "stdout", "stderr", "returncode")},
        exception=data.get("exception_info") or data.get("error"),
        exception_type=data.get("exception_type")
        or data.get("error_type")
        or (data.get("extra") or {}).get("exception_type"),
        call_id=data.get("tool_call_id"),
        message_index=index,
    )


@dataclass
class Action:
    name: str = "shell"
    command: str | None = None
    arguments: dict | str | None = None
    call_id: str | None = None
    message_index: int | None = None
    observation: Observation | None = None
    status: str = "unrecorded"
    duration_ms: float | None = None
    cwd: str | None = None
    association: str = ""
    span: Span | None = None

    @property
    def summary(self) -> str:
        return command_summary(self.command) if self.command else self.name


def command_summary(command: str) -> str:
    if match := re.search(r"(?:^|\n|&&|;)\s*cat\s*>\s*['\"]?([^\s'\"<>]+)", command):
        return f"写入文件 · {match[1]}"[:48]
    if re.search(r"\b(?:pytest|unittest)\b", command.split("<<", 1)[0]):
        return "运行测试"
    if re.search(r"\bgit\s+diff\s+--check\b", command):
        return "检查 Git 空白格式"
    if re.search(r"\bgit\s+diff\b", command):
        return "查看 Git 差异"
    if re.search(r"\bpython[\d.]*\s+", command):
        return "执行 Python 脚本"
    if match := re.search(r"(?:^|&&|;)\s*(?:cat|nl\s+-ba|head|tail)\s+([^\s;&]+)", command):
        return f"查看文件 · {match[1]}"[:48]
    if re.search(r"\b(?:ls|find|pwd)\b", command):
        return "查看目录与文件"
    if "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" in command:
        return "发送提交指令"
    return "执行 Shell 命令"


def parsed_actions(message: dict, index: int | None = None) -> list[Action]:
    calls = list(message.get("tool_calls") or [])
    calls.extend(c for c in message.get("output", []) or [] if isinstance(c, dict) and c.get("type") == "function_call")
    calls.extend(c for c in message.get("content", []) or [] if isinstance(c, dict) and c.get("type") == "tool_use")
    result = []
    for call in calls:
        function = call.get("function", call)
        arguments = function.get("arguments", function.get("input", {}))
        if isinstance(arguments, str):
            try:
                arguments = json.loads(arguments)
            except json.JSONDecodeError:
                pass
        result.append(
            Action(
                name=function.get("name", "shell"),
                command=arguments.get("command") if isinstance(arguments, dict) else None,
                arguments=arguments,
                call_id=call.get("call_id") or call.get("id"),
                message_index=index,
            )
        )
    if "actions" in message.get("extra", {}):
        normalized = []
        for i, action in enumerate(message["extra"]["actions"]):
            call_id = action.get("tool_call_id")
            native = next((c for c in result if call_id and c.call_id == call_id), None)
            if native is None and not call_id and i < len(result):
                native = result[i]
            normalized.append(
                Action(
                    name=action.get("tool_name", native.name if native else "shell"),
                    command=action.get("command"),
                    arguments=native.arguments if native else action,
                    call_id=call_id or (native.call_id if native else None),
                    message_index=index,
                    cwd=action.get("cwd"),
                )
            )
        return normalized
    return result or [
        Action(command=m[1].strip(), message_index=index) for m in COMMAND_BLOCK.finditer(response_text(message))
    ]


def decision_text(message: dict, calls: list[Action]) -> str:
    text = response_text(message)
    commands = {c.command.strip() for c in calls if c.command}
    return COMMAND_BLOCK.sub(lambda m: "" if m[1].strip() in commands else m[0], text).strip()


@dataclass
class Step:
    index: int
    message_indices: list[int] = field(default_factory=list)
    decisions: list[str] = field(default_factory=list)
    actions: list[Action] = field(default_factory=list)
    notes: list[dict] = field(default_factory=list)
    unmatched_observations: list[Observation] = field(default_factory=list)
    spans: list[Span] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    duration_ms: float | None = None
    status: str = "unrecorded"
    diff: str | None = None
    human: bool = False

    @property
    def summary(self) -> str:
        return (self.actions[0].summary if self.actions else "调用反馈" if self.errors else "模型回复") + (
            f" · {len(self.actions)} 个动作" if len(self.actions) > 1 else ""
        )

    @property
    def returncodes(self) -> str:
        codes = list(
            dict.fromkeys(
                str(a.observation.returncode)
                for a in self.actions
                if a.observation and a.observation.returncode is not None and a.status != "not_executed"
            )
        )
        return "返回码 " + ", ".join(codes[:3]) + ("…" if len(codes) > 3 else "") if codes else ""


@dataclass
class Trace:
    steps: list[Step]
    initial_indices: list[int]
    exit_indices: list[int]


def is_observation(message: dict) -> bool:
    extra = message.get("extra", {})
    text = text_content(message.get("content"))
    return (
        message.get("role") == "tool"
        or message.get("type") == "function_call_output"
        or bool({"raw_output", "returncode", "exception_info"} & extra.keys())
        or bool(re.search(r"<returncode>|<output>|\"(?:returncode|stdout|stderr)\"\s*:", text))
    )


def action_status(action: Action) -> str:
    obs = action.observation
    attrs = dict(action.span.attributes) if action.span else {}
    if obs:
        attrs.update(returncode=obs.returncode, exception_info=obs.exception, exception_type=obs.exception_type)
    return tool_status(attrs, action.span.status if action.span else "unknown")


def build_trace(run: Run) -> Trace:
    messages = run.messages
    groups = grouped_indices(messages)
    step_spans = sorted((s for s in run.spans if s.kind == "step"), key=lambda s: s.step_index)
    explicit = any("message_indices" in s.attributes for s in step_spans)
    indexed = (
        [(s.step_index, s.attributes.get("message_indices", [])) for s in step_spans]
        if explicit
        else list(enumerate(groups[1:], 1))
    )
    if not explicit:
        indexed.extend((s.step_index, []) for s in step_spans if s.step_index not in {i for i, _ in indexed})
    steps = []
    for index, indices in sorted(indexed):
        step = Step(index, [i for i in indices if 0 <= i < len(messages)])
        step.spans = [s for s in run.spans if s.step_index == index and s.kind in {"step", "llm", "tool"}]
        root = next((s for s in step.spans if s.kind == "step"), None)
        step.duration_ms = root.duration_ms if root else None
        for i in step.message_indices:
            message = messages[i]
            extra = message.get("extra", {})
            if is_assistant(message):
                calls = parsed_actions(message, i)
                step.actions.extend(calls)
                if text := decision_text(message, calls):
                    step.decisions.append(text)
                step.human = step.human or message.get("role") == "user"
            elif extra.get("interrupt_type") == "FormatError":
                step.errors.append(response_text(message))
                original = extra.get("model_response")
                response = extra.get("response", {})
                if isinstance(response, dict):
                    original = original or response_text(response)
                    if not original and response.get("choices"):
                        original = response_text(response["choices"][0].get("message", {}))
                if original:
                    step.decisions.append(original)
            elif message.get("role") == "exit":
                continue
            elif extra.get("interrupt_type") or message.get("role") == "system":
                step.notes.append(message)
            else:
                obs = make_observation(observation(message), i)
                call = next(
                    (
                        a
                        for a in step.actions
                        if a.observation is None and (a.call_id == obs.call_id if obs.call_id else not a.call_id)
                    ),
                    None,
                )
                if call is not None:
                    call.observation = obs
                    call.association = "call_id" if obs.call_id else "position"
                elif is_observation(message):
                    step.unmatched_observations.append(obs)
                else:
                    step.notes.append(message)
        for span in (s for s in step.spans if s.kind == "tool"):
            attrs = span.attributes
            call_id = attrs.get("tool_call_id")
            call = next(
                (
                    a
                    for a in step.actions
                    if a.span is None and (a.call_id == call_id if call_id else a.command == attrs.get("command"))
                ),
                None,
            )
            if call is None:
                if run.source != "runtime" and step.actions:
                    continue
                call = Action(name=attrs.get("tool_name", "shell"), command=attrs.get("command"), call_id=call_id)
                step.actions.append(call)
            call.span = span
            call.duration_ms = span.duration_ms
            call.cwd = attrs.get("cwd") or call.cwd
            if any(
                attrs.get(k) is not None
                for k in (
                    "output",
                    "stdout",
                    "stderr",
                    "returncode",
                    "exception_info",
                    "exception_type",
                    "extra",
                    "error",
                    "error_type",
                )
            ):
                measured = make_observation(attrs)
                if call.observation is None:
                    call.observation = measured
                else:
                    for key in ("output", "stdout", "stderr", "returncode", "exception", "exception_type"):
                        if (value := getattr(measured, key)) is not None and (
                            run.source == "runtime" or getattr(call.observation, key) is None
                        ):
                            setattr(call.observation, key, value)
        for span in step.spans:
            if span.kind != "tool" and span.status == "error":
                error = span.attributes.get("error") or (span.attributes.get("error_type") if not step.errors else None)
                if error and error not in step.errors:
                    step.errors.append(error)
        for call in step.actions:
            call.status = action_status(call)
            obs = call.observation
            if obs and obs.output and obs.output.lstrip().startswith("diff --git"):
                step.diff = obs.output
        statuses = {a.status for a in step.actions}
        step.status = (
            "execution_error"
            if "execution_error" in statuses
            else "call_error"
            if step.errors
            else "nonzero_exit"
            if "nonzero_exit" in statuses
            else "not_executed"
            if "not_executed" in statuses
            else "submitted"
            if "submitted" in statuses
            else "ok"
            if statuses == {"ok"}
            else "unrecorded"
        )
        if root and root.status == "running":
            step.status = "running"
        elif "interrupted" in statuses or root and root.status == "interrupted" and step.status == "unrecorded":
            step.status = "interrupted"
        steps.append(step)
    assigned = {i for step in steps for i in step.message_indices}
    return Trace(
        steps,
        [i for i in range(len(messages)) if i not in assigned and messages[i].get("role") != "exit"],
        [i for i, m in enumerate(messages) if m.get("role") == "exit"],
    )

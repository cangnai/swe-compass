import hashlib
import json
from pathlib import Path

from minisweagent.models.utils.content_string import get_content_string
from minisweagent.observability.schema import Run, Span, Task
from minisweagent.observability.storage import Store
from minisweagent.observability.trajectory_steps import (
    build_trace,
    grouped_indices,
    is_assistant,
    observation,
    parsed_actions,
)


def message_groups(messages: list[dict]) -> list[list[dict]]:
    return [[messages[i] for i in group] for group in grouped_indices(messages)]


def actions(message: dict) -> list[dict]:
    if "actions" in message.get("extra", {}):
        return message["extra"]["actions"]
    return [
        {"command": call.command} | ({"tool_call_id": call.call_id} if call.call_id else {})
        for call in parsed_actions(message)
        if call.command is not None
    ]


def llm_attributes(message: dict, model: str = "unknown") -> dict:
    extra = message.get("extra", {})
    response = extra.get("response") or message
    response = response if isinstance(response, dict) else {}
    usage = response.get("usage") or extra.get("usage") or {}
    usage = usage if isinstance(usage, dict) else {}
    cost = extra.get("cost")
    return {
        "model": response.get("model", model),
        "input_tokens": usage.get("prompt_tokens", usage.get("input_tokens")),
        "output_tokens": usage.get("completion_tokens", usage.get("output_tokens")),
        "total_tokens": usage.get("total_tokens"),
        "cost_usd": cost if isinstance(cost, int | float) and cost > 0 else None,
    }


def parse_trajectory(data: dict | list, task_id: str = "", run_id: str = "") -> tuple[Task, Run]:
    if not isinstance(data, dict | list) or (isinstance(data, dict) and not {"messages", "trajectory"} & data.keys()):
        raise ValueError("Expected a message array or a trajectory object with messages")
    messages = data if isinstance(data, list) else data.get("messages", data.get("trajectory", []))
    if not isinstance(messages, list) or any(not isinstance(m, dict) for m in messages):
        raise ValueError("Trajectory messages must be an array of objects")
    info = (data.get("info") or {}) if isinstance(data, dict) else {}
    config = info.get("config") or {}
    description = next((get_content_string(m) for m in messages if m.get("role") == "user"), "Imported trajectory")
    identity = hashlib.sha256(json.dumps(data, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:24]
    task = Task(
        task_id or "import-" + hashlib.sha256(description.encode()).hexdigest()[:16],
        description[:100],
        description,
        source="trajectory",
        external_id=data.get("instance_id", "") if isinstance(data, dict) else "",
    )
    run = Run(
        run_id or "import-" + identity,
        task.task_id,
        model=(config.get("model") or {}).get("model_name", "unknown"),
        config_snapshot=config,
        messages=messages,
        exit_status=info.get("exit_status") or "unknown",
        submission=info.get("submission", ""),
        metadata={"model_stats": info.get("model_stats") or {}},
    )
    for message in reversed(messages):
        if message.get("role") == "exit":
            run.exit_status = message.get("extra", {}).get("exit_status", run.exit_status)
            run.submission = message.get("extra", {}).get("submission", run.submission)
            break
    for view in build_trace(run).steps:
        index = view.index
        step = Span(run.run_id, "step", index, status=view.status, attributes={"message_indices": view.message_indices})
        run.spans.append(step)
        for i in view.message_indices:
            message = messages[i]
            if message.get("extra", {}).get("interrupt_type") == "FormatError":
                run.spans.append(
                    Span(
                        run.run_id,
                        "llm",
                        index,
                        parent_span_id=step.span_id,
                        status="error",
                        attributes=llm_attributes(message, run.model)
                        | {"error_type": "FormatError", "error_messages": [message]},
                    )
                )
                continue
            if not is_assistant(message):
                continue
            run.spans.append(
                Span(
                    run.run_id, "llm", index, parent_span_id=step.span_id, attributes=llm_attributes(message, run.model)
                )
            )
        for call in view.actions:
            output = call.observation
            run.spans.append(
                Span(
                    run.run_id,
                    "tool",
                    index,
                    parent_span_id=step.span_id,
                    status=call.status,
                    attributes={
                        "tool_name": call.name,
                        "command": call.command,
                        "arguments": call.arguments,
                        "cwd": call.cwd,
                        "tool_call_id": call.call_id,
                    }
                    | (
                        {
                            "returncode": output.returncode,
                            "output": output.output,
                            "stdout": output.stdout,
                            "stderr": output.stderr,
                            "exception_info": output.exception,
                            "exception_type": output.exception_type,
                        }
                        if output
                        else {}
                    ),
                )
            )
    patches = []
    if isinstance(data, dict):
        for candidate in (data.get("patch"), data.get("diff"), info.get("patch"), info.get("diff"), run.submission):
            if isinstance(candidate, str) and (candidate.startswith("diff --git") or "\n--- " in candidate):
                patches.append(candidate)
    for message in messages:
        text = observation(message)["output"] if not is_assistant(message) else ""
        if isinstance(text, str) and text.lstrip().startswith("diff --git"):
            patches.append(text)
    run.diff = patches[-1] if patches else None
    return task, run


def import_trajectory(store: Store, path: Path, task_id: str = "") -> Run:
    from minisweagent.observability.diagnostics import diagnose

    task, run = parse_trajectory(json.loads(path.read_text(encoding="utf-8-sig")), task_id)
    if task.task_id not in {t.task_id for t in store.tasks()}:
        store.save_task(task)
    run.trajectory_path = str(store.preserve(path))
    run.metadata["original_path"] = str(path.resolve())
    run.events = diagnose(run)
    store.save_run(run)
    return run

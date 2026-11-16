"""Evidence-based diagnostics; repeated commands alone never imply stalled progress."""

import hashlib
import re
from collections import Counter
from dataclasses import asdict

from minisweagent.observability.execution_status import abnormal_run
from minisweagent.observability.monitoring import monitor_run
from minisweagent.observability.schema import Event, Run, Span
from minisweagent.observability.trajectory_steps import Action, response_text

RULE_VERSION = "4"
HUMAN_INTERRUPTS = {"UserRejection", "UserInterruption", "UserNewTask"}
CONTROLLED_ENDINGS = {"LimitsExceeded", "TimeExceeded", "Interrupted", "KeyboardInterrupt"}
FLOW_INTERRUPTS = HUMAN_INTERRUPTS | CONTROLLED_ENDINGS | {"Submitted", "InterruptAgentFlow"}


def event_group(event: Event) -> str:
    if (
        event.type == "human_intervention"
        or (event.type == "action_not_executed" and event.evidence.get("human_intervention"))
        or (
            event.type == "termination"
            and event.severity == "info"
            and event.evidence.get("exit_status") in HUMAN_INTERRUPTS
        )
    ):
        return "人工干预"
    if event.type == "suspected_no_progress":
        return "行为风险"
    return "确定异常" if event.severity == "error" else "执行记录"


def recorded_exception(span: Span) -> bool:
    return span.attributes.get("error_type") not in FLOW_INTERRUPTS and (
        span.status == "error" or bool(span.attributes.get("error_type"))
    )


def normalize_command(command: str | None) -> str:
    command = (command or "").strip()
    return command if "\n" in command or "\r" in command else " ".join(re.findall(r""""[^"]*"|'[^']*'|\S+""", command))


def failure_signature(output: str) -> str:
    lines = [
        line.strip()
        for line in output.splitlines()
        if re.search(r"FAILED\s|AssertionError|^E\s|\bFAIL(?:\s|:)|\d+ failed\b|tests? failed\b", line, re.IGNORECASE)
    ]
    return re.sub(r"\b\d+(?:\.\d+)?s\b|0x[0-9a-f]+", "<variable>", "\n".join(lines))


def action_evidence(action: Action, step: int, ordinal: int) -> dict:
    attrs = dict(action.span.attributes) if action.span else {}
    obs = action.observation
    return attrs | {
        "span_id": action.span.span_id if action.span else None,
        "command": action.command,
        "cwd": action.cwd,
        "tool_call_id": action.call_id,
        "action_index": ordinal,
        "step_index": step,
        "status": action.status,
        "returncode": obs.returncode if obs else None,
        "output": obs.output if obs else None,
        "stdout": obs.stdout if obs else None,
        "stderr": obs.stderr if obs else None,
        "exception_info": obs.exception if obs else None,
        "exception_type": obs.exception_type if obs else None,
        "observation": asdict(obs) if obs else None,
    }


def code_state(records: list[dict]) -> str:
    before = [r.get("state_before") for r in records]
    after = [r.get("state_hash") for r in records]
    if any(a and b and a != b for a, b in zip(before, after)) or len({s for s in before + after if s}) > 1:
        return "changed"
    return "unchanged" if all(before) and all(after) else "unrecorded"


def tool_event(evidence: dict) -> str:
    if evidence["status"] == "not_executed":
        return "action_not_executed"
    return (
        "tool_timeout"
        if "Timeout" in str(evidence.get("exception_type") or "")
        or re.search(r"timed out|TimeoutExpired|TimeoutError", str(evidence.get("exception_info") or ""), re.IGNORECASE)
        else "tool_error"
    )


def diagnose(run: Run) -> list[Event]:
    monitor = monitor_run(run)
    events: list[Event] = []

    def add(kind: str, severity: str, step: int, rule: str, evidence: dict, identity: str = "") -> None:
        event = Event(
            run.run_id,
            kind,
            severity,
            step,
            rule,
            {"confidence": "observed"} | evidence | {"rule_version": RULE_VERSION},
        )
        event.event_id = hashlib.sha256(f"{run.run_id}:{kind}:{step}:{identity or rule}".encode()).hexdigest()
        events.append(event)

    tools, interventions = [], []
    for view in monitor.steps:
        step = view.step
        feedback = [
            run.messages[i]
            for i in step.message_indices
            if run.messages[i].get("extra", {}).get("interrupt_type") in HUMAN_INTERRUPTS
        ]
        if feedback:
            interventions.append(step.index)
        if any(note.get("role") == "user" for note in step.notes):
            interventions.append(step.index)
        format_spans = [s for s in view.llm if s.attributes.get("error_type") == "FormatError"]
        for span in view.llm:
            attrs = span.attributes
            if recorded_exception(span):
                add(
                    "format_error" if attrs.get("error_type") == "FormatError" else "api_error",
                    "error",
                    step.index,
                    "Action parser explicitly reported FormatError"
                    if attrs.get("error_type") == "FormatError"
                    else "Model query explicitly recorded an exception",
                    {"span_id": span.span_id, **attrs},
                    span.span_id,
                )
        if not format_spans:
            for i in step.message_indices:
                message = run.messages[i]
                if message.get("extra", {}).get("interrupt_type") == "FormatError":
                    add(
                        "format_error",
                        "error",
                        step.index,
                        "Action parser explicitly reported FormatError",
                        {"message": response_text(message), "raw_message": message},
                        str(i),
                    )
        skipped = False
        for ordinal, action in enumerate(step.actions, 1):
            evidence = action_evidence(action, step.index, ordinal)
            identity = action.span.span_id if action.span else f"action-{ordinal}"
            if action.status == "not_executed":
                skipped = True
                add(
                    "action_not_executed",
                    "info",
                    step.index,
                    "Observation explicitly records an unexecuted action",
                    evidence
                    | {
                        "reason": [response_text(m) for m in feedback],
                        "interventions": feedback,
                        "human_intervention": bool(feedback),
                        "confidence": "observed",
                    },
                    identity,
                )
            else:
                tools.append(evidence)
                if action.status == "execution_error":
                    add(
                        tool_event(evidence),
                        "error",
                        step.index,
                        "Executor explicitly recorded an exception",
                        evidence | {"confidence": "observed"},
                        identity,
                    )
        if feedback and not skipped:
            add(
                "human_intervention",
                "info",
                step.index,
                "Trajectory explicitly records user intervention",
                {"reason": [response_text(m) for m in feedback], "interventions": feedback, "confidence": "observed"},
            )

    for span in monitor.orphan_llm:
        if recorded_exception(span):
            add(
                "format_error" if span.attributes.get("error_type") == "FormatError" else "api_error",
                "error",
                0,
                "Unassigned model call explicitly recorded an exception",
                {"span_id": span.span_id, **span.attributes},
                span.span_id,
            )
    for ordinal, action in enumerate(monitor.orphan_tools, 1):
        evidence = action_evidence(action, 0, ordinal)
        if action.status in {"execution_error", "not_executed"}:
            add(
                tool_event(evidence),
                "error" if action.status == "execution_error" else "info",
                0,
                "Unassigned tool call retains its recorded outcome",
                evidence,
                f"orphan-{ordinal}",
            )

    assigned = {i for view in monitor.steps for i in view.step.message_indices}
    for i, message in enumerate(run.messages):
        if i not in assigned and message.get("extra", {}).get("interrupt_type") in HUMAN_INTERRUPTS:
            add(
                "human_intervention",
                "info",
                0,
                "Unassigned message explicitly records user intervention",
                {"reason": [response_text(message)], "interventions": [message]},
                str(i),
            )

    streak: list[dict] = []

    def finish_streak() -> None:
        if len(streak) >= 3:
            add(
                "suspected_no_progress",
                "warning",
                streak[-1]["step_index"],
                "At least three adjacent identical failures in the same cwd, with measured unchanged code and no intervention",
                {
                    "command": streak[-1]["command"],
                    "steps": [r["step_index"] for r in streak],
                    "count": len(streak),
                    "signature": streak[-1]["signature"],
                    "records": list(streak),
                    "code_state": "unchanged",
                    "confidence": "suspected",
                },
                f"progress-{streak[0]['step_index']}",
            )

    safe_steps = {
        view.step.index
        for view in monitor.steps
        if len(view.step.actions) == 1 and not view.step.human and view.step.index not in interventions
    }
    for current in tools:
        signature = failure_signature("\n".join(str(current.get(k) or "") for k in ("output", "stdout", "stderr")))
        eligible = (
            current["status"] == "nonzero_exit"
            and normalize_command(current["command"])
            and signature
            and current.get("cwd")
            and current["step_index"] in safe_steps
            and code_state([current]) == "unchanged"
        )
        if streak and not (
            eligible
            and current["step_index"] == streak[-1]["step_index"] + 1
            and normalize_command(current["command"]) == normalize_command(streak[-1]["command"])
            and current["returncode"] == streak[-1]["returncode"]
            and signature == streak[-1]["signature"]
            and current["cwd"] == streak[-1]["cwd"]
            and current["state_hash"] == streak[-1]["state_hash"]
        ):
            finish_streak()
            streak = []
        if eligible:
            streak.append(current | {"signature": signature})
    finish_streak()
    run_errors = [s for s in run.spans if s.kind == "run" and recorded_exception(s)]
    if abnormal_run(run.exit_status) or run_errors:
        add(
            "termination",
            "info" if run.exit_status in CONTROLLED_ENDINGS | HUMAN_INTERRUPTS and not run_errors else "error",
            max((v.step.index for v in monitor.steps), default=0),
            "Run ended with recorded termination reason",
            {
                "exit_status": run.exit_status,
                "submission": run.submission,
                "exit_message": next((m for m in reversed(run.messages) if m.get("role") == "exit"), {}),
                "run_errors": [asdict(s) for s in run_errors],
            },
        )
    return sorted(events, key=lambda event: event.step_index)


def distribution(run: Run) -> dict[str, int]:
    return dict(Counter(e.type for e in run.events))

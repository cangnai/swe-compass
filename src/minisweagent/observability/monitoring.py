"""Read-only monitoring summaries with explicit measurement coverage."""

import math
from dataclasses import dataclass, replace
from statistics import mean

from minisweagent.observability.execution_status import abnormal_run
from minisweagent.observability.schema import Run, Span
from minisweagent.observability.trajectory_steps import Action, Step, action_status, build_trace, make_observation


def number(value) -> int | float | None:
    return (
        value
        if isinstance(value, int | float) and not isinstance(value, bool) and math.isfinite(value) and value >= 0
        else None
    )


@dataclass(frozen=True)
class Measurement:
    value: int | float | None
    known: int
    total: int
    source: str = "calls"

    @property
    def complete(self) -> bool:
        return self.total > 0 and self.known == self.total


def measured(values: list, *, average: bool = False, source: str = "calls") -> Measurement:
    known = [n for value in values if (n := number(value)) is not None]
    return Measurement((mean(known) if average else sum(known)) if known else None, len(known), len(values), source)


@dataclass
class Usage:
    tokens: Measurement
    input_tokens: Measurement
    output_tokens: Measurement
    cost: Measurement
    llm_ms: Measurement
    tool_ms: Measurement


def usage(llm: list[Span], tools: list[Action]) -> Usage:
    return Usage(
        **{
            key: measured([s.attributes.get(field) for s in llm])
            for key, field in (
                ("tokens", "total_tokens"),
                ("input_tokens", "input_tokens"),
                ("output_tokens", "output_tokens"),
                ("cost", "cost_usd"),
            )
        },
        llm_ms=measured([s.duration_ms for s in llm]),
        tool_ms=measured([a.duration_ms for a in tools if a.status != "not_executed"]),
    )


@dataclass
class StepMonitor:
    step: Step
    llm: list[Span]
    usage: Usage


@dataclass
class RunMonitor:
    run: Run
    steps: list[StepMonitor]
    usage: Usage
    call_cost: Measurement
    orphan_llm: list[Span]
    orphan_tools: list[Action]

    @property
    def tools(self) -> list[Action]:
        return [a for s in self.steps for a in s.step.actions] + self.orphan_tools


def parent_step(span: Span, by_id: dict[str, Span]) -> int:
    parent = span.parent_span_id
    visited = {span.span_id}
    while parent in by_id and parent not in visited:
        visited.add(parent)
        ancestor = by_id[parent]
        if ancestor.kind == "step":
            return ancestor.step_index
        parent = ancestor.parent_span_id
    return span.step_index


def monitor_run(run: Run) -> RunMonitor:
    by_id = {s.span_id: s for s in run.spans}
    aliases = {
        item["id"]: item["call_id"]
        for message in run.messages
        for item in message.get("output", []) or []
        if isinstance(item, dict) and item.get("type") == "function_call" and item.get("id") and item.get("call_id")
    }
    spans = []
    for span in run.spans:
        attributes = span.attributes
        if span.kind == "tool" and attributes.get("tool_call_id") in aliases:
            attributes = attributes | {"tool_call_id": aliases[attributes["tool_call_id"]]}
        spans.append(
            replace(span, step_index=parent_step(span, by_id), attributes=attributes)
            if span.kind in {"llm", "tool"}
            else span
        )
    trace = build_trace(replace(run, spans=spans))
    steps = []
    for step in trace.steps:
        llm = [s for s in step.spans if s.kind == "llm" and not (step.human and run.source == "historical")]
        steps.append(StepMonitor(step, llm, usage(llm, step.actions)))
    assigned = {s.span_id for view in steps for s in view.step.spans if s.kind == "llm"} | {
        a.span.span_id for view in steps for a in view.step.actions if a.span
    }
    orphan_llm = [s for s in spans if s.kind == "llm" and s.span_id not in assigned]
    orphan_tools = []
    for span in spans:
        if span.kind != "tool" or span.span_id in assigned:
            continue
        action = Action(
            name=span.attributes.get("tool_name", "shell"),
            command=span.attributes.get("command"),
            arguments=span.attributes.get("arguments"),
            observation=make_observation(span.attributes),
            duration_ms=span.duration_ms,
            span=span,
        )
        action.status = action_status(action)
        orphan_tools.append(action)
    tools = [a for view in steps for a in view.step.actions] + orphan_tools
    summary = usage([s for view in steps for s in view.llm] + orphan_llm, tools)
    model_stats = run.metadata.get("model_stats") or {}
    expected_calls = number(model_stats.get("api_calls"))
    if expected_calls is not None and int(expected_calls) == expected_calls:
        for key in ("tokens", "input_tokens", "output_tokens", "cost", "llm_ms"):
            metric = getattr(summary, key)
            setattr(summary, key, replace(metric, total=max(metric.total, int(expected_calls))))
    call_cost = summary.cost
    aggregate_cost = number(model_stats.get("instance_cost"))
    if not summary.cost.complete and aggregate_cost is not None and aggregate_cost > 0:
        summary.cost = Measurement(aggregate_cost, 1, 1, "run")
    return RunMonitor(run, steps, summary, call_cost, orphan_llm, orphan_tools)


@dataclass
class Overview:
    tasks: int
    runs: int
    submitted: int
    abnormal: int
    running: int
    unrecorded: int
    tokens: Measurement
    cost: Measurement
    average_ms: Measurement
    token_complete_runs: int
    cost_complete_runs: int
    token_known_calls: int
    cost_known_calls: int
    llm_calls: int
    aggregate_cost_runs: int


def overview(monitors: list[RunMonitor]) -> Overview:
    runs = [m.run for m in monitors]
    return Overview(
        tasks=len({r.task_id for r in runs}),
        runs=len(runs),
        submitted=sum(r.exit_status == "Submitted" for r in runs),
        abnormal=sum(abnormal_run(r.exit_status) for r in runs),
        running=sum(r.exit_status == "running" for r in runs),
        unrecorded=sum(r.exit_status in {"unknown", "", None} for r in runs),
        tokens=measured([m.usage.tokens.value for m in monitors], source="runs"),
        cost=measured([m.usage.cost.value for m in monitors], source="runs"),
        average_ms=measured([r.duration_ms for r in runs], average=True, source="runs"),
        token_complete_runs=sum(m.usage.tokens.complete for m in monitors),
        cost_complete_runs=sum(m.usage.cost.complete for m in monitors),
        token_known_calls=sum(m.usage.tokens.known for m in monitors),
        cost_known_calls=sum(m.call_cost.known for m in monitors),
        llm_calls=sum(m.usage.tokens.total for m in monitors),
        aggregate_cost_runs=sum(m.usage.cost.source == "run" for m in monitors),
    )


def run_state(status: str) -> str:
    return {
        "Submitted": "已提交",
        "running": "运行中",
        "unknown": "结束状态未记录",
        "": "结束状态未记录",
        None: "结束状态未记录",
        "LimitsExceeded": "达到运行上限",
        "TimeExceeded": "运行超时",
        "RepeatedFormatError": "连续格式错误",
        "Interrupted": "已中断",
    }.get(status, f"异常终止（{status}）")

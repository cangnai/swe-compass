from collections import Counter
from statistics import mean

from minisweagent.observability.diagnostics import distribution
from minisweagent.observability.execution_status import abnormal_run, tool_status
from minisweagent.observability.monitoring import number
from minisweagent.observability.schema import Run


def known_sum(values: list) -> float | int | None:
    known = [n for value in values if (n := number(value)) is not None]
    return sum(known) if known else None


def run_metrics(run: Run) -> dict:
    llm = [s for s in run.spans if s.kind == "llm"]
    tool_actions = [s for s in run.spans if s.kind == "tool"]
    states = {s.span_id: tool_status(s.attributes, s.status) for s in tool_actions}
    tools = [s for s in tool_actions if states[s.span_id] != "not_executed"]
    result = {
        "run_id": run.run_id,
        "trace_id": run.trace_id,
        "task_id": run.task_id,
        "model": run.model,
        "exit_status": run.exit_status,
        "steps": sum(s.kind == "step" for s in run.spans),
        "duration_ms": run.duration_ms,
        "llm_calls": len(llm),
        "tool_calls": len(tools),
        "tool_actions": len(tool_actions),
        "tool_skipped": len(tool_actions) - len(tools),
        "llm_ms": known_sum([s.duration_ms for s in llm]),
        "tool_ms": known_sum([s.duration_ms for s in tools]),
        "llm_ms_known_calls": sum(number(s.duration_ms) is not None for s in llm),
        "tool_ms_known_calls": sum(number(s.duration_ms) is not None for s in tools),
        "errors": sum(states[s.span_id] == "execution_error" for s in tools) + sum(s.status == "error" for s in llm),
        "model_errors": sum(s.status == "error" for s in llm),
        "diagnostics": distribution(run),
        "tool_nonzero": sum(states[s.span_id] == "nonzero_exit" for s in tools),
        "tool_exceptions": sum(states[s.span_id] == "execution_error" for s in tools),
    }
    for key in ("input_tokens", "output_tokens", "total_tokens", "cost_usd"):
        values = [number(s.attributes.get(key)) for s in llm]
        result[key] = known_sum(values)
        result[key + "_known_calls"] = sum(v is not None for v in values)
    model_stats = run.metadata.get("model_stats") or {}
    expected = max(len(llm), number(model_stats.get("api_calls")) or 0)
    result["llm_expected_calls"] = expected
    result["total_tokens_complete"] = expected > 0 and result["total_tokens_known_calls"] == expected
    result["cost_usd_complete"] = expected > 0 and result["cost_usd_known_calls"] == expected
    cost = number(model_stats.get("instance_cost"))
    if not result["cost_usd_complete"] and cost is not None and cost > 0:
        result["cost_usd"] = cost
        result["cost_source"] = "trajectory model_stats aggregate"
        result["cost_usd_complete"] = True
    return result


def aggregate(runs: list[Run]) -> dict:
    rows = [run_metrics(r) for r in runs]
    durations = [n for r in runs if (n := number(r.duration_ms)) is not None]
    return {
        "tasks": len({r.task_id for r in runs}),
        "runs": len(runs),
        "submitted": sum(r.exit_status == "Submitted" for r in runs),
        "abnormal": sum(abnormal_run(r.exit_status) for r in runs),
        "average_steps": mean([r["steps"] for r in rows]) if rows else None,
        "average_duration_ms": mean(durations) if durations else None,
        "duration_known_runs": len(durations),
        "total_tokens": known_sum([r["total_tokens"] for r in rows]),
        "known_cost_usd": known_sum([r["cost_usd"] for r in rows]),
        "token_known_runs": sum(r["total_tokens"] is not None for r in rows),
        "token_complete_runs": sum(r["total_tokens_complete"] for r in rows),
        "cost_known_runs": sum(r["cost_usd"] is not None for r in rows),
        "cost_complete_runs": sum(r["cost_usd_complete"] for r in rows),
        "models": dict(Counter(r.model for r in runs)),
    }

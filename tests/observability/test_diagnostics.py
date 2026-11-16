import json
import os
from pathlib import Path

import pytest

from minisweagent.observability.diagnostic_details import explain
from minisweagent.observability.diagnostics import RULE_VERSION, diagnose, event_group, normalize_command
from minisweagent.observability.metrics import run_metrics
from minisweagent.observability.monitoring import monitor_run
from minisweagent.observability.schema import Event, Run, Span, Task
from minisweagent.observability.storage import Store
from minisweagent.observability.trajectory_parser import import_trajectory, parse_trajectory

DATA = Path(__file__).parents[1] / "test_data"
DASHBOARD = Path(__file__).parents[2] / "src/minisweagent/observability/dashboard.py"


def repeated_run(*, snapshots=True) -> Run:
    run = Run("repeat", "task", source="runtime", exit_status="Submitted")
    for step in range(1, 4):
        root = Span(run.run_id, "step", step, attributes={"message_indices": []})
        run.spans.extend(
            [
                root,
                Span(
                    run.run_id,
                    "tool",
                    step,
                    parent_span_id=root.span_id,
                    attributes={
                        "command": "python -m pytest -q",
                        "cwd": "/repo",
                        "returncode": 1,
                        "output": f"FAILED test_bug.py::test_fraction - AssertionError\n1 failed in {step}.01s",
                        "state_before": "same" if snapshots else None,
                        "state_hash": "same" if snapshots else None,
                    },
                ),
            ]
        )
    return run


@pytest.mark.parametrize(
    ("variation", "expected"),
    [
        ("unchanged", True),
        ("unknown", False),
        ("after_only", False),
        ("changed", False),
        ("intervention", False),
        ("different_failure", False),
        ("different_code", False),
        ("successful", False),
        ("empty_output", False),
        ("no_test_failure", False),
        ("skip_between", False),
        ("unknown_cwd", False),
        ("different_cwd", False),
        ("two_failures", False),
    ],
)
def test_no_progress_requires_consecutive_failure_evidence_known_code_and_no_intervention(variation, expected):
    run = repeated_run(snapshots=variation != "unknown")
    tools = [s for s in run.spans if s.kind == "tool"]
    if variation == "after_only":
        for tool in tools:
            tool.attributes.pop("state_before")
    elif variation == "changed":
        tools[1].attributes["state_hash"] = "changed"
    elif variation == "intervention":
        run.messages.append(
            {"role": "user", "content": "I changed the code", "extra": {"interrupt_type": "UserRejection"}}
        )
        next(s for s in run.spans if s.kind == "step" and s.step_index == 2).attributes["message_indices"] = [0]
    elif variation == "different_failure":
        tools[1].attributes["output"] = "FAILED test_other.py::test_other - AssertionError"
    elif variation == "different_code":
        tools[1].attributes["returncode"] = 2
    elif variation == "successful":
        tools[1].attributes.update(returncode=0, output="1 passed")
    elif variation in {"empty_output", "no_test_failure"}:
        for tool in tools:
            tool.attributes["output"] = "" if variation == "empty_output" else "python is not installed"
    elif variation == "skip_between":
        run.spans.insert(
            4,
            Span(
                run.run_id,
                "tool",
                2,
                status="not_executed",
                attributes={"command": "skipped", "returncode": -1, "exception_info": "action was not executed"},
            ),
        )
    elif variation == "two_failures":
        run.spans = run.spans[:-2]
    elif variation in {"unknown_cwd", "different_cwd"}:
        tools[1].attributes["cwd"] = None if variation == "unknown_cwd" else "/other-repo"
    before = run.to_dict()
    events = diagnose(run)
    assert any(event.type == "suspected_no_progress" for event in events) == expected
    assert run.to_dict() == before
    assert len({event.event_id for event in events}) == len(events)
    assert [event.event_id for event in events] == [event.event_id for event in diagnose(run)]
    if expected:
        risk = next(event for event in events if event.type == "suspected_no_progress")
        detail = explain(risk, monitor_run(run))
        assert detail.confidence == "启发式风险" and risk.evidence["confidence"] == "suspected"
    assert not any(event.type in {"tool_nonzero", "repeated_command", "repeated_test_failure"} for event in events)


def test_successful_repeated_command_is_not_a_diagnostic():
    run = repeated_run()
    for span in run.spans:
        if span.kind == "tool":
            span.attributes.update(returncode=0, output="3 passed")
    events = diagnose(run)
    assert not events
    run.spans[-1].attributes["command"] = "echo other"
    assert not any(event.type in {"repeated_test_failure", "suspected_no_progress"} for event in diagnose(run))


@pytest.mark.parametrize(
    ("returncode", "output"),
    [
        (1, "AssertionError: expected repro"),
        (2, "whitespace error"),
        (-1, "no exception recorded"),
        (-9, "TimeoutExpired"),
    ],
)
def test_nonzero_result_remains_in_trace_and_metrics_but_not_diagnostics(returncode, output):
    _, run = parse_trajectory(
        {
            "info": {"exit_status": "Submitted"},
            "messages": [
                {"role": "assistant", "extra": {"actions": [{"command": "python reproduce.py"}]}},
                {"role": "user", "extra": {"returncode": returncode, "raw_output": output}},
            ],
        }
    )
    assert not diagnose(run)
    assert monitor_run(run).steps[0].step.actions[0].observation.returncode == returncode
    assert run_metrics(run)["tool_nonzero"] == 1 and run_metrics(run)["tool_exceptions"] == 0
    run.messages = []
    run.spans = [span for span in run.spans if span.kind == "tool"]
    assert not diagnose(run) and len(monitor_run(run).orphan_tools) == 1


def test_long_failure_streak_is_one_risk_and_separate_streaks_remain_distinct():
    run = repeated_run()
    for step in range(4, 10):
        run.spans.extend(
            [
                Span(run.run_id, "step", step, attributes={"message_indices": []}),
                Span(run.run_id, "tool", step, attributes=dict(run.spans[1].attributes)),
            ]
        )
    events = diagnose(run)
    assert [(event.type, event.step_index, event.evidence["count"]) for event in events] == [
        ("suspected_no_progress", 9, 9)
    ]
    assert events[0].evidence["steps"] == list(range(1, 10))
    next(span for span in run.spans if span.kind == "tool" and span.step_index == 5).attributes.update(
        returncode=0, output="1 passed"
    )
    assert [event.evidence["steps"] for event in diagnose(run)] == [[1, 2, 3, 4], [6, 7, 8, 9]]


@pytest.mark.parametrize(
    ("status", "group", "severity"),
    [
        ("LimitsExceeded", "执行记录", "info"),
        ("TimeExceeded", "执行记录", "info"),
        ("Interrupted", "执行记录", "info"),
        ("UserInterruption", "人工干预", "info"),
        ("RuntimeError", "确定异常", "error"),
        ("RepeatedFormatError", "确定异常", "error"),
    ],
)
def test_run_termination_is_distinct_from_executor_error_and_controlled_flow(status, group, severity):
    run = Run("ending", "task", exit_status=status)
    events = diagnose(run)
    assert [(event.type, event.severity) for event in events] == [("termination", severity)]
    assert event_group(events[0]) == group == explain(events[0], monitor_run(run)).category
    assert events[0].evidence["rule_version"] == RULE_VERSION
    run.spans.append(Span(run.run_id, "llm", status="interrupted", attributes={"error_type": "UserInterruption"}))
    assert [event.type for event in diagnose(run)] == ["termination"]


def test_model_api_error_and_unassigned_intervention_have_explicit_evidence():
    run = Run("api", "task", exit_status="Submitted")
    run.spans.append(Span(run.run_id, "llm", status="error", attributes={"error_type": "ConnectionError"}))
    run.messages = [{"role": "user", "content": "Pause", "extra": {"interrupt_type": "UserInterruption"}}]
    events = diagnose(run)
    assert {(event.type, event_group(event)) for event in events} == {
        ("api_error", "确定异常"),
        ("human_intervention", "人工干预"),
    }
    assert next(event for event in events if event.type == "human_intervention").evidence["reason"] == ["Pause"]


def test_explicit_run_exception_is_not_hidden_by_fallback_interrupted_status():
    run = Run("early-error", "task", exit_status="Interrupted")
    run.spans.append(
        Span(run.run_id, "run", status="error", attributes={"error_type": "ValueError", "error": "bad template"})
    )
    events = diagnose(run)
    assert [(event.type, event_group(event), event.severity) for event in events] == [
        ("termination", "确定异常", "error")
    ]
    detail = explain(events[0], monitor_run(run))
    assert "明确记录了异常" in detail.happened and "bad template" in detail.evidence
    assert "无法判断" in detail.subsequent[0]


def test_command_normalization_preserves_shell_line_breaks_and_quoted_whitespace():
    assert normalize_command(' echo   "a  b" ') == 'echo "a  b"'
    assert normalize_command("echo a\necho b") != normalize_command("echo a echo b")


def test_multiple_calls_rejection_reason_timeout_and_model_errors_are_separate():
    _, run = parse_trajectory(
        {
            "messages": [
                {
                    "role": "assistant",
                    "extra": {"actions": [{"command": "python -m pytest", "tool_call_id": str(i)} for i in range(3)]},
                },
                {
                    "role": "tool",
                    "tool_call_id": "0",
                    "extra": {"returncode": 1, "raw_output": "FAILED test_a - TimeoutExpired"},
                },
                {
                    "role": "tool",
                    "tool_call_id": "1",
                    "extra": {
                        "returncode": -1,
                        "exception_info": "executor timed out",
                        "exception_type": "TimeoutExpired",
                    },
                },
                {
                    "role": "tool",
                    "tool_call_id": "2",
                    "extra": {"returncode": -1, "exception_info": "action was not executed"},
                },
                {
                    "role": "user",
                    "content": "Do not run the remaining command",
                    "extra": {"interrupt_type": "UserRejection"},
                },
                {"role": "user", "content": "format invalid", "extra": {"interrupt_type": "FormatError"}},
            ]
        }
    )
    events = diagnose(run)
    assert {event.type for event in events} == {"tool_timeout", "action_not_executed", "format_error"}
    assert sum(event.type == "format_error" for event in events) == 1
    assert sum(event.type == "tool_timeout" for event in events) == 1
    rejected = next(event for event in events if event.type == "action_not_executed")
    detail = explain(rejected, monitor_run(run))
    assert detail.title == "用户拒绝执行动作" and detail.category == "人工干预"
    assert "Do not run the remaining command" in detail.evidence
    assert rejected.evidence["returncode"] == -1 and rejected.evidence["action_index"] == 3
    assert not any(event.type == "suspected_no_progress" for event in events)


def test_skip_without_reason_is_not_invented_as_human_rejection():
    _, run = parse_trajectory(
        {
            "messages": [
                {"role": "assistant", "extra": {"actions": [{"command": "echo skip"}]}},
                {"role": "user", "extra": {"returncode": -1, "exception_info": "action was not executed"}},
            ]
        }
    )
    detail = explain(diagnose(run)[0], monitor_run(run))
    assert detail.title == "动作未执行" and detail.category == "执行记录"
    assert any("没有记录具体原因" in line for line in detail.evidence)


def test_unassigned_timeout_retains_evidence_without_inventing_a_step():
    run = Run("orphan", "task")
    run.spans.append(
        Span(
            run.run_id,
            "tool",
            0,
            attributes={"command": "sleep 10", "returncode": -1, "exception_type": "TimeoutExpired"},
        )
    )
    events = diagnose(run)
    assert [(event.type, event.step_index) for event in events] == [("tool_timeout", 0)]
    assert events[0].evidence["exception_type"] == "TimeoutExpired"
    assert explain(events[0], monitor_run(run)).category == "确定异常"


def test_intervention_does_not_override_recorded_executed_action():
    _, run = parse_trajectory(
        {
            "messages": [
                {"role": "assistant", "extra": {"actions": [{"command": "echo done"}]}},
                {"role": "user", "extra": {"returncode": 0, "raw_output": "done"}},
                {
                    "role": "user",
                    "content": "Do not continue",
                    "extra": {"interrupt_type": "UserRejection"},
                },
            ]
        }
    )
    monitor = monitor_run(run)
    assert monitor.steps[0].step.actions[0].status == "ok"
    assert [event.type for event in diagnose(run)] == ["human_intervention"]
    detail = explain(diagnose(run)[0], monitor)
    assert detail.title == "用户拒绝后续操作" and detail.category == "人工干预"
    assert "不据此推翻" in detail.happened and "Do not continue" in detail.evidence


def test_stored_legacy_events_are_recomputed_without_losing_raw_database_evidence(tmp_path):
    store = Store(tmp_path / "traces.db")
    run = import_trajectory(store, DATA / "github_issue.traj.json")
    original = run.to_dict()
    old = Event(run.run_id, "tool_error", "error", 1, "legacy wrong classification", {"old": True})
    run.events = [old, Event(run.run_id, "tool_nonzero", "warning", 1, "legacy warning", {})]
    store.save_run(run)
    fresh = store.run(run.run_id)
    assert not fresh.events
    assert run_metrics(fresh)["diagnostics"] == {}
    assert fresh.messages == original["messages"] and [as_span.attributes for as_span in fresh.spans] == [
        s["attributes"] for s in original["spans"]
    ]
    with store.connect() as db:
        assert json.loads(db.execute("SELECT body FROM events WHERE event_id=?", (old.event_id,)).fetchone()[0])[
            "evidence"
        ] == {"old": True}


def test_followup_only_claims_observed_next_steps_and_same_command_results():
    run = repeated_run()
    next(s for s in run.spans if s.kind == "tool").attributes["exception_info"] = "executor startup error"
    run.spans[-1].attributes.update(returncode=0, output="3 passed")
    event = next(event for event in diagnose(run) if event.type == "tool_error")
    detail = explain(event, monitor_run(run))
    assert "后续还有 2 个已记录 Step。" in detail.subsequent
    assert any("Step 2" in line and "非零退出" in line for line in detail.subsequent)
    assert any("独立评测" in line for line in detail.subsequent)


def open_diagnostics(store):
    from streamlit.testing.v1 import AppTest

    app = AppTest.from_file(str(DASHBOARD), default_timeout=30)
    app.query_params["page"] = "失败分析"
    app.run()
    next(w for w in app.text_input if w.label.startswith("SQLite")).set_value(str(store.path)).run()
    assert not app.exception
    return app


def test_page_selection_empty_results_advanced_evidence_and_m1_links(tmp_path):
    store = Store(tmp_path / "traces.db")
    empty = open_diagnostics(store)
    assert not empty.exception and not empty.json and not empty.dataframe
    first = repeated_run()
    store.save_task(Task(first.task_id, "重复失败演示"))
    store.save_run(first)
    second = import_trajectory(store, DATA / "local.traj.json")
    app = open_diagnostics(store)
    next(w for w in app.selectbox if w.label == "诊断任务").set_value(first.task_id).run()
    assert not app.exception and not app.dataframe
    assert next(w for w in app.metric if w.label == "确定异常").value == "0"
    assert next(w for w in app.metric if w.label == "行为风险").value == "1"
    assert not any(w.label.startswith("筛选") for w in app.selectbox)
    assert all(not w.proto.expanded for w in app.expander)
    assert len(app.json) == sum(len(w.get("json")) for w in app.get("popover"))
    assert all(first.run_id not in w.label for w in app.expander)
    assert any("在轨迹页查看 Step" in w.value and "?page=" in w.value for w in app.markdown)
    next(w for w in app.radio if w.label == "诊断范围").set_value("确定异常").run()
    assert not app.exception and not app.get("popover")
    next(w for w in app.selectbox if w.label == "诊断任务").set_value(second.task_id).run()
    assert not app.exception and app.query_params["run"] in (second.run_id, [second.run_id])
    assert all(second.run_id not in option for w in app.selectbox for option in w.options)


def test_page_selects_multiple_runs_of_one_task_and_retains_m1_destination(tmp_path):
    store = Store(tmp_path / "traces.db")
    first = repeated_run()
    first.task_id = "shared"
    store.save_task(Task(first.task_id, "同任务的不同运行"))
    first.events = diagnose(first)
    store.save_run(first)
    second = import_trajectory(store, DATA / "local.traj.json", task_id="shared")
    app = open_diagnostics(store)
    next(w for w in app.selectbox if w.label == "诊断任务").set_value(first.task_id).run()
    choices = next(w for w in app.selectbox if w.label == "诊断运行")
    assert len(choices.options) == 2 and any("第 2 次运行" in label for label in choices.options)
    choices.set_value(first.run_id).run()
    assert not app.exception and app.query_params["run"] in (first.run_id, [first.run_id])
    assert any(first.run_id in w.value and f"step={first.events[0].step_index}" in w.value for w in app.markdown)
    next(w for w in app.selectbox if w.label == "诊断运行").set_value(second.run_id).run()
    assert not app.exception and app.query_params["run"] in (second.run_id, [second.run_id])


def test_overview_does_not_mix_exceptions_human_events_and_risks_or_show_full_logs(tmp_path):
    store = Store(tmp_path / "traces.db")
    run = repeated_run()
    run.exit_status = "RuntimeError"
    run.messages = [{"role": "user", "content": "Do not execute", "extra": {"interrupt_type": "UserRejection"}}]
    run.spans.extend(
        [
            Span(run.run_id, "step", 4, attributes={"message_indices": []}),
            Span(run.run_id, "llm", 4, status="error", attributes={"error_type": "ConnectionError"}),
            Span(
                run.run_id,
                "tool",
                4,
                attributes={
                    "command": "sleep 10",
                    "returncode": -1,
                    "exception_type": "TimeoutExpired",
                    "exception_info": "executor timed out\n" + "FULL_RAW_LOG\n" * 100,
                    "output": "FULL_RAW_OUTPUT\n" * 100,
                },
            ),
            Span(run.run_id, "step", 5, attributes={"message_indices": [0]}),
            Span(run.run_id, "tool", 5, attributes={"exception_info": "action was not executed", "returncode": -1}),
        ]
    )
    store.save_task(Task(run.task_id, "异常与干预分开统计"))
    store.save_run(run)
    app = open_diagnostics(store)
    assert {metric.label: metric.value for metric in app.metric} == {
        "确定异常": "3",
        "人工干预": "1",
        "行为风险": "1",
        "执行记录": "0",
    }
    assert not any("FULL_RAW" in item.value for item in app.text)
    assert any("FULL_RAW_LOG" in item.value for item in app.json)
    next(w for w in app.radio if w.label == "诊断范围").set_value("行为风险").run()
    assert not app.exception and len(app.get("popover")) == 1
    assert any("启发式风险" in item.value for item in app.markdown)


def test_buggy_real_history_rejection_reason_and_recalculated_ui(tmp_path):
    source = os.environ.get("MSWEA_TEST_TRAJECTORY")
    if not source:
        pytest.skip("Set MSWEA_TEST_TRAJECTORY to verify the user's local trajectory")
    store = Store(tmp_path / "traces.db")
    raw = Path(source).read_bytes()
    run = import_trajectory(store, Path(source))
    assert [(event.step_index, event.type) for event in run.events] == [(6, "action_not_executed")]
    detail = explain(run.events[-1], monitor_run(run))
    assert detail.title == "用户拒绝执行动作" and detail.category == "人工干预"
    assert any("CRLF" in reason and "LF" in reason for reason in detail.evidence)
    assert any("Step 7" in line for line in detail.subsequent)
    assert Path(run.trajectory_path).read_bytes() == raw == Path(source).read_bytes()
    app = open_diagnostics(store)
    assert next(w for w in app.metric if w.label == "人工干预").value == "1"
    assert next(w for w in app.metric if w.label == "确定异常").value == "0"
    assert next(w for w in app.metric if w.label == "行为风险").value == "0"
    next(w for w in app.radio if w.label == "诊断范围").set_value("人工干预").run()
    assert not app.exception and len(app.get("popover")) == 1
    assert any("用户拒绝执行动作" in w.label for w in app.expander)
    assert any("CRLF" in w.value and "LF" in w.value for w in app.text)
    assert all(not w.proto.expanded for w in app.expander)

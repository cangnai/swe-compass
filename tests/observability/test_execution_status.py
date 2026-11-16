import json
import os
from pathlib import Path

import pytest

from minisweagent.agents.default import DefaultAgent
from minisweagent.environments.local import LocalEnvironment
from minisweagent.models.test_models import DeterministicModel
from minisweagent.observability.demo import python_command
from minisweagent.observability.diagnostics import diagnose
from minisweagent.observability.execution_status import tool_status
from minisweagent.observability.instrumentation import Collector, ObserveConfig, observed_agent_class
from minisweagent.observability.metrics import run_metrics
from minisweagent.observability.monitoring import monitor_run, overview
from minisweagent.observability.schema import Run, Task
from minisweagent.observability.storage import Store
from minisweagent.observability.trajectory_parser import import_trajectory, parse_trajectory
from minisweagent.observability.trajectory_steps import build_trace

DASHBOARD = Path(__file__).parents[2] / "src/minisweagent/observability/dashboard.py"


@pytest.mark.parametrize(
    ("attributes", "legacy", "expected"),
    [
        ({"returncode": 0}, "unknown", "ok"),
        ({"returncode": 0}, "error", "ok"),
        ({"returncode": 1}, "error", "nonzero_exit"),
        ({"returncode": -9}, "unknown", "nonzero_exit"),
        ({"returncode": -1}, "unknown", "nonzero_exit"),
        ({"returncode": -1, "exception_info": "action was not executed"}, "error", "not_executed"),
        ({"returncode": -1, "exception_info": "timed out"}, "error", "execution_error"),
        ({"returncode": 0, "extra": {"exception_type": "OSError"}}, "unknown", "execution_error"),
        ({"returncode": 1, "output": "TimeoutExpired: AssertionError"}, "unknown", "nonzero_exit"),
        ({}, "unknown", "unrecorded"),
        ({}, "ok", "unrecorded"),
        ({}, "error", "execution_error"),
        ({}, "submitted", "submitted"),
        ({}, "interrupted", "interrupted"),
    ],
)
def test_state_requires_executor_evidence_not_log_words_or_legacy_nonzero_error(attributes, legacy, expected):
    assert tool_status(attributes, legacy) == expected


def test_real_local_nonzero_timeout_and_startup_error_are_distinct_and_wrapped_agent_continues(tmp_path):
    command = python_command("import sys; print('expected reproduction'); sys.exit(3)")
    output = LocalEnvironment().execute({"command": command})
    assert output["returncode"] == 3 and not output["exception_info"] and tool_status(output) == "nonzero_exit"
    timeout = LocalEnvironment(timeout=1).execute({"command": python_command("import time; time.sleep(2)")})
    assert timeout["returncode"] == -1 and timeout["extra"]["exception_type"] == "TimeoutExpired"
    assert tool_status(timeout) == "execution_error"
    startup = LocalEnvironment(cwd=str(tmp_path / "absent")).execute({"command": "echo test"})
    assert startup["returncode"] == -1 and tool_status(startup) == "execution_error"

    store = Store(tmp_path / "traces.db")
    store.save_task(Task("task", "task"))
    collector = Collector(store, ObserveConfig("task"))
    model = DeterministicModel(
        cost_per_call=0,
        outputs=[
            {"role": "assistant", "content": "actual response", "extra": {"actions": [{"command": c}]}}
            for c in (command, "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT")
        ],
    )
    agent = observed_agent_class(DefaultAgent)(
        model,
        LocalEnvironment(),
        collector=collector,
        system_template="system",
        instance_template="{{task}}",
        cost_limit=0,
    )
    assert agent.run("task")["exit_status"] == "Submitted"
    run = store.run(collector.run.run_id)
    assert next(span for span in run.spans if span.kind == "tool").status == "nonzero_exit"
    assert [view.step.status for view in monitor_run(run).steps] == ["nonzero_exit", "submitted"]
    assert (run_metrics(run)["tool_nonzero"], run_metrics(run)["tool_exceptions"], run_metrics(run)["errors"]) == (
        1,
        0,
        0,
    )
    assert not diagnose(run)
    assert overview([monitor_run(run)]).abnormal == 0


def mixed_run() -> tuple[Task, Run]:
    return parse_trajectory(
        {
            "info": {"exit_status": "Submitted"},
            "messages": [
                {"role": "user", "content": "mixed outcomes"},
                {
                    "role": "assistant",
                    "tool_calls": [
                        {
                            "id": str(i),
                            "function": {"name": "bash", "arguments": json.dumps({"command": f"actual command {i}"})},
                        }
                        for i in range(4)
                    ],
                },
                {"role": "tool", "tool_call_id": "0", "content": '{"returncode":0,"output":"ok"}'},
                {"role": "tool", "tool_call_id": "1", "content": '{"returncode":1,"output":"reproduction"}'},
                {
                    "role": "tool",
                    "tool_call_id": "2",
                    "content": '{"returncode":-1,"exception_info":"executor timed out"}',
                },
                {
                    "role": "tool",
                    "tool_call_id": "3",
                    "content": '{"returncode":-1,"exception_info":"action was not executed"}',
                },
                {"role": "user", "content": "invalid action format", "extra": {"interrupt_type": "FormatError"}},
            ],
        }
    )


def test_mixed_steps_and_legacy_error_counters_distinguish_process_and_model_outcomes():
    _, run = mixed_run()
    monitor = monitor_run(run)
    assert [a.status for a in monitor.tools] == ["ok", "nonzero_exit", "execution_error", "not_executed"]
    assert [view.step.status for view in monitor.steps] == ["execution_error", "call_error"]
    assert monitor.steps[0].step.returncodes == "返回码 0, 1, -1"
    for span in run.spans:
        if span.kind == "tool" and span.attributes.get("returncode") not in (None, 0):
            span.status = "error"
    metrics = run_metrics(run)
    assert (metrics["tool_nonzero"], metrics["tool_exceptions"], metrics["tool_skipped"], metrics["model_errors"]) == (
        1,
        1,
        1,
        1,
    )
    assert metrics["errors"] == 2 and metrics["tool_calls"] == 3
    assert overview([monitor_run(run)]).abnormal == 0


def test_filters_include_mixed_outcomes_without_treating_nonzero_as_exception(tmp_path):
    from streamlit.testing.v1 import AppTest

    store = Store(tmp_path / "traces.db")
    task, run = mixed_run()
    store.save_task(task)
    store.save_run(run)
    app = AppTest.from_file(str(DASHBOARD), default_timeout=30)
    app.query_params["page"] = "监控"
    app.run()
    next(w for w in app.text_input if w.label.startswith("SQLite")).set_value(str(store.path)).run()
    for label, nodes in (("非零退出", 5), ("执行异常", 5), ("动作未执行", 5), ("模型或解析异常", 1)):
        next(w for w in app.radio if w.label == "调用链范围").set_value(label).run()
        assert not app.exception and len(app.get("popover")) == nodes
    assert next(w for w in app.metric if w.label == "Agent 运行状态").value == "已提交"
    assert next(w for w in app.metric if w.label == "独立评测结果").value == "未评测"
    next(span for span in run.spans if span.kind == "llm").status = "error"
    store.save_run(run)
    app.run()
    assert not app.exception and len(app.get("popover")) == 6


def test_buggy_history_states_short_summaries_and_m1_m2_filters(tmp_path):
    from streamlit.testing.v1 import AppTest

    if not (source := os.environ.get("MSWEA_TEST_TRAJECTORY")):
        pytest.skip("Set MSWEA_TEST_TRAJECTORY to verify the user's local trajectory")
    store = Store(tmp_path / "traces.db")
    run = import_trajectory(store, Path(source))
    # Simulate records already imported by the old wrapper, without migrating the database.
    for span in run.spans:
        if span.kind == "tool" and span.step_index in (2, 3, 5):
            span.status = "error"
    store.save_run(run)
    trace, monitor = build_trace(run), monitor_run(run)
    assert [(s.index, s.actions[0].observation.returncode) for s in trace.steps if s.status == "nonzero_exit"] == [
        (2, 1),
        (3, 1),
        (5, 2),
    ]
    assert not any(s.status in {"execution_error", "call_error"} for s in trace.steps)
    assert [view.step.status for view in monitor.steps] == [s.status for s in trace.steps]
    assert all(len(s.summary) <= 48 and "\n" not in s.summary and "&&" not in s.summary for s in trace.steps)
    metrics = run_metrics(run)
    assert (metrics["tool_nonzero"], metrics["tool_exceptions"], metrics["errors"]) == (3, 0, 0)

    app = AppTest.from_file(str(DASHBOARD), default_timeout=30).run()
    next(w for w in app.text_input if w.label.startswith("SQLite")).set_value(str(store.path)).run()
    for i in (2, 3, 5):
        next(w for w in app.radio if w.label == "选择步骤").set_value(i).run()
        assert not app.exception
        assert any("非零退出" in w.value for w in app.caption)
    options = next(w for w in app.radio if w.label == "选择步骤").options
    assert all("&&" not in option and "```" not in option for option in options)
    assert "返回码 1" in options[1] and "返回码 2" in options[4]
    app.sidebar.radio[0].set_value("监控").run()
    next(w for w in app.radio if w.label == "调用链范围").set_value("非零退出").run()
    assert not app.exception and len(app.get("popover")) == 6
    labels = [w.label for w in app.expander if w.label.startswith("Step ")]
    assert [int(label.split(" · ")[0].split()[1]) for label in labels] == [2, 3, 5]
    assert all("未记录" not in label for label in labels)
    next(w for w in app.radio if w.label == "调用链范围").set_value("执行异常").run()
    assert not app.exception and len(app.get("popover")) == 0

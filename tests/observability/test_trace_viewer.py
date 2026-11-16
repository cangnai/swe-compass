import copy
import json
import os
from pathlib import Path

import pytest

from minisweagent.agents.default import DefaultAgent
from minisweagent.environments.local import LocalEnvironment
from minisweagent.models.test_models import DeterministicToolcallModel
from minisweagent.observability.demo import python_command
from minisweagent.observability.instrumentation import Collector, ObserveConfig, observed_agent_class
from minisweagent.observability.schema import Task
from minisweagent.observability.storage import Store
from minisweagent.observability.trajectory_parser import import_trajectory, parse_trajectory
from minisweagent.observability.trajectory_steps import build_trace

DATA = Path(__file__).parents[1] / "test_data"
DASHBOARD = Path(__file__).parents[2] / "src/minisweagent/observability/dashboard.py"


@pytest.mark.parametrize(("name", "count", "errors"), [("local", 2, []), ("github_issue", 10, [1, 8])])
def test_real_text_steps_are_ordered_lossless_and_deduplicated(name, count, errors):
    original = json.loads((DATA / f"{name}.traj.json").read_text(encoding="utf-8"))
    _, run = parse_trajectory(copy.deepcopy(original))
    trace = build_trace(run)
    assert trace.initial_indices == [0, 1] and len(trace.steps) == count
    assert [s.index for s in trace.steps if s.status == "nonzero_exit"] == errors
    assert run.messages == original and run.duration_ms is None
    assert sorted(trace.initial_indices + [i for s in trace.steps for i in s.message_indices]) == list(
        range(len(original))
    )
    for step in trace.steps:
        assert step.message_indices == [step.index * 2, step.index * 2 + 1]
        assert len(step.actions) == 1 and len(step.decisions) == 1
        assert step.actions[0].command not in step.decisions[0]
        assert step.actions[0].observation.message_index == step.index * 2 + 1
        assert step.duration_ms is None and not step.unmatched_observations
    assert trace.steps[-1].diff == run.diff


def test_multiple_calls_match_ids_not_output_order_and_keep_missing_and_orphan_results():
    data = {
        "messages": [
            {"role": "system", "content": "system"},
            {"role": "user", "content": "task"},
            {
                "object": "response",
                "output": [
                    {"type": "message", "content": [{"type": "output_text", "text": "先检查，然后验证。"}]},
                    {
                        "type": "function_call",
                        "id": "fc-one",
                        "call_id": "one",
                        "name": "bash",
                        "arguments": '{"command":"echo one"}',
                    },
                    {
                        "type": "function_call",
                        "id": "fc-two",
                        "call_id": "two",
                        "name": "inspect",
                        "arguments": '{"path":"bug.py"}',
                    },
                    {
                        "type": "function_call",
                        "id": "fc-three",
                        "call_id": "three",
                        "name": "bash",
                        "arguments": '{"command":"echo missing"}',
                    },
                ],
            },
            {
                "type": "function_call_output",
                "call_id": "two",
                "output": '{"returncode":1,"stdout":"","stderr":"actual error"}',
            },
            {"type": "function_call_output", "call_id": "wrong-id", "output": "orphan"},
            {"type": "function_call_output", "call_id": "one", "output": '{"returncode":0,"output":"one"}'},
            {"role": "exit", "content": "done", "extra": {"exit_status": "Submitted", "submission": "done"}},
        ]
    }
    original = copy.deepcopy(data)
    _, run = parse_trajectory(data)
    trace = build_trace(run)
    step = trace.steps[0]
    assert step.decisions == ["先检查，然后验证。"] and len(step.actions) == 3
    assert [a.call_id for a in step.actions] == ["one", "two", "three"]
    assert [a.status for a in step.actions] == ["ok", "nonzero_exit", "unrecorded"]
    assert step.actions[0].observation.output == "one" and step.actions[0].association == "call_id"
    assert step.actions[1].observation.stderr == "actual error" and step.actions[1].command is None
    assert step.actions[2].observation is None
    assert step.unmatched_observations[0].output == "orphan" and trace.exit_indices == [6]
    assert run.exit_status == "Submitted" and step.status == "nonzero_exit" and data == original


def test_rejection_feedback_cannot_be_consumed_as_anonymous_observation():
    _, run = parse_trajectory(
        {
            "messages": [
                {
                    "role": "assistant",
                    "content": "检查。\n```bash\npython -m pytest\n```",
                    "extra": {"actions": [{"command": "python -m pytest"}]},
                },
                {"role": "user", "content": "拒绝执行", "extra": {"interrupt_type": "UserRejection"}},
                {
                    "role": "user",
                    "content": "",
                    "extra": {"raw_output": "", "returncode": -1, "exception_info": "action was not executed"},
                },
                {"role": "assistant", "content": "只说明，没有工具调用。"},
            ]
        }
    )
    trace = build_trace(run)
    assert trace.steps[0].decisions == ["检查。"] and len(trace.steps[0].actions) == 1
    assert trace.steps[0].status == "not_executed" and trace.steps[0].notes[0]["content"] == "拒绝执行"
    assert trace.steps[0].actions[0].observation.returncode == -1
    assert trace.steps[1].status == "unrecorded" and not trace.steps[1].actions


def test_format_failure_keeps_actual_response_without_inventing_a_decision():
    _, run = parse_trajectory(
        {
            "messages": [
                {"role": "user", "content": "task"},
                {
                    "role": "user",
                    "content": "invalid action format",
                    "extra": {
                        "interrupt_type": "FormatError",
                        "response": {"choices": [{"message": {"content": "actual failed response"}}]},
                    },
                },
                {"role": "user", "content": "missing raw response", "extra": {"interrupt_type": "FormatError"}},
                {"role": "exit", "extra": {"exit_status": "RepeatedFormatError"}},
            ]
        }
    )
    trace = build_trace(run)
    assert len(trace.steps) == 2 and trace.steps[0].decisions == ["actual failed response"]
    assert trace.steps[0].errors == ["invalid action format"] and trace.steps[1].decisions == []
    assert all(s.status == "call_error" and not s.actions for s in trace.steps)


def test_existing_historical_spans_cannot_override_message_evidence_or_duplicate_native_calls():
    original = json.loads((DATA / "github_issue.traj.json").read_text(encoding="utf-8"))
    _, run = parse_trajectory(original)
    for span in run.spans:
        span.attributes.pop("message_indices", None)
        if span.kind == "tool":
            span.attributes["output"] = "obsolete display output"
    trace = build_trace(run)
    assert len(trace.steps) == 10 and all(len(s.actions) == 1 for s in trace.steps)
    assert "obsolete display output" not in [s.actions[0].observation.output for s in trace.steps]
    assert trace.steps[0].actions[0].observation.returncode == 1


def test_empty_steps_and_long_logs_keep_truthful_status_and_full_raw_evidence(tmp_path):
    from streamlit.testing.v1 import AppTest

    store = Store(tmp_path / "traces.db")
    task, run = parse_trajectory(
        {
            "messages": [
                {"role": "user", "content": "task"},
                {"role": "exit", "content": "API unavailable", "extra": {"exit_status": "RuntimeError"}},
            ]
        }
    )
    store.save_task(task)
    store.save_run(run)
    app = AppTest.from_file(str(DASHBOARD), default_timeout=30).run()
    next(w for w in app.text_input if w.label == "SQLite 数据库").set_value(str(store.path)).run()
    assert not app.exception and not any(w.label == "选择步骤" for w in app.radio)
    assert any(w.label == "执行步数" and w.value == "0" for w in app.metric)
    log = "\n".join(f"actual line {i}" for i in range(200))
    task, run = parse_trajectory(
        {
            "messages": [
                {
                    "role": "assistant",
                    "content": "执行检查。",
                    "extra": {"actions": [{"command": "actual command\n" * 20}]},
                },
                {"role": "user", "content": json.dumps({"returncode": 0, "stdout": log, "stderr": "actual stderr"})},
                {"role": "exit", "extra": {"exit_status": "Submitted"}},
            ]
        }
    )
    store.save_task(task)
    store.save_run(run)
    app.query_params.update({"run": run.run_id, "step": "1"})
    app.run()
    assert not app.exception and sum(w.value == log for w in app.code) == 1
    assert any("stdout" in w.label and not w.proto.expanded for w in app.expander)
    assert any("完整命令" in w.label and not w.proto.expanded for w in app.expander)
    assert any(w.label == "独立评测" and w.value == "未评测" for w in app.metric)
    assert sum(w.value == "actual stderr" for w in app.code) == 1
    assert all(not w.proto.expanded for w in app.expander)


def test_real_runtime_multiple_actions_merge_spans_and_messages_without_merging_repeated_commands(tmp_path):
    store = Store(tmp_path / "traces.db")
    store.save_task(Task("task", "task"))
    command = python_command("print('actual-output')")
    first = {
        "role": "assistant",
        "content": "检查两次。",
        "tool_calls": [
            {"id": cid, "function": {"name": "bash", "arguments": json.dumps({"command": command})}}
            for cid in ("a", "b")
        ],
        "extra": {"actions": [{"command": command, "tool_call_id": cid} for cid in ("a", "b")]},
    }
    model = DeterministicToolcallModel(
        outputs=[
            first,
            {
                "role": "assistant",
                "content": "提交。",
                "extra": {
                    "actions": [{"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT", "tool_call_id": "end"}]
                },
            },
        ],
        cost_per_call=0,
    )
    collector = Collector(store, ObserveConfig("task"))
    agent = observed_agent_class(DefaultAgent)(
        model,
        LocalEnvironment(),
        collector=collector,
        system_template="system",
        instance_template="{{task}}",
        cost_limit=0,
    )
    assert agent.run("task")["exit_status"] == "Submitted"
    trace = build_trace(store.run(collector.run.run_id))
    assert len(trace.steps) == 2 and trace.initial_indices == [0, 1]
    assert len(trace.steps[0].actions) == 2 and trace.steps[0].decisions == ["检查两次。"]
    assert all(
        a.observation.output.strip() == "actual-output" and a.status == "ok" and a.duration_ms > 0
        for a in trace.steps[0].actions
    )
    assert not trace.steps[0].unmatched_observations and trace.steps[0].duration_ms > 0
    assert trace.steps[1].actions[0].status == "submitted" and trace.steps[1].actions[0].observation is None


def test_ui_navigation_order_deduplication_and_collapsed_details_on_real_trajectory(tmp_path):
    from streamlit.testing.v1 import AppTest

    store = Store(tmp_path / "traces.db")
    run = import_trajectory(store, DATA / "github_issue.traj.json")
    app = AppTest.from_file(str(DASHBOARD), default_timeout=30)
    app.query_params.update({"page": "轨迹", "run": run.run_id, "step": "4"})
    app.run()
    next(w for w in app.text_input if w.label == "SQLite 数据库").set_value(str(store.path)).run()
    assert not app.exception and next(w for w in app.radio if w.label == "选择步骤").value == 4
    assert app.query_params["step"] in ("4", ["4"])
    assert [w.value for w in app.code].count("cat tests/missing_colon.py") == 1
    assert [
        w.value for w in app.markdown if w.value in {"**LLM 决策**", "**Action 1 · shell**", "**Observation 1**"}
    ] == ["**LLM 决策**", "**Action 1 · shell**", "**Observation 1**"]
    assert all(not w.proto.expanded for w in app.expander)
    assert not any("?page=" in option for w in app.radio if w.label == "选择步骤" for option in w.options)
    assert not any(value in w.value for w in app.caption for value in ("unknown", "None"))
    next(w for w in app.button if w.label == "下一步").click().run()
    assert next(w for w in app.radio if w.label == "选择步骤").value == 5 and app.query_params["step"] in ("5", ["5"])
    next(w for w in app.button if w.label == "上一步").click().run()
    assert next(w for w in app.radio if w.label == "选择步骤").value == 4
    next(w for w in app.radio if w.label == "选择步骤").set_value(8).run()
    assert app.query_params["step"] in ("8", ["8"]) and not app.exception
    app.query_params.update({"step": "2"})
    app.run()
    assert next(w for w in app.radio if w.label == "选择步骤").value == 2
    app.query_params.update({"step": "999"})
    app.run()
    assert next(w for w in app.radio if w.label == "选择步骤").value == 1 and not app.exception


def test_user_responses_trajectory(tmp_path):
    from streamlit.testing.v1 import AppTest

    source = os.environ.get("MSWEA_TEST_TRAJECTORY")
    if not source:
        pytest.skip("Set MSWEA_TEST_TRAJECTORY to verify the user's local trajectory")
    path = Path(source)
    original = path.read_bytes()
    store = Store(tmp_path / "traces.db")
    run = import_trajectory(store, path)
    trace = build_trace(run)
    assert len(trace.steps) == 9 and trace.initial_indices == [0, 1] and trace.exit_indices == [21]
    assert all(len(s.actions) == 1 and len(s.decisions) == 1 and not s.unmatched_observations for s in trace.steps)
    assert [s.index for s in trace.steps if s.status == "nonzero_exit"] == [2, 3, 5]
    assert trace.steps[5].status == "not_executed" and len(trace.steps[5].notes) == 1
    assert "16 passed" in trace.steps[6].actions[0].observation.output
    assert all(s.duration_ms is None for s in trace.steps) and run.diff is None
    assert store.evaluation(run.run_id).verdict == "not_evaluated"
    assert Path(run.trajectory_path).read_bytes() == original == path.read_bytes()
    app = AppTest.from_file(str(DASHBOARD), default_timeout=30).run()
    next(w for w in app.text_input if w.label == "SQLite 数据库").set_value(str(store.path)).run()
    for step in trace.steps:
        next(w for w in app.radio if w.label == "选择步骤").set_value(step.index).run()
        assert not app.exception
        assert [c.value for c in app.code].count(step.actions[0].command) == 1
        assert all(not w.proto.expanded for w in app.expander)
    next(w for w in app.radio if w.label == "选择步骤").set_value(6).run()
    assert any("此动作未执行" in w.value for w in app.info)

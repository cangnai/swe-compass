import copy
import csv
import io
import json
from pathlib import Path

import pytest

from minisweagent.agents.default import DefaultAgent
from minisweagent.environments.local import LocalEnvironment
from minisweagent.models.test_models import DeterministicModel
from minisweagent.models.utils.actions_text import parse_regex_actions
from minisweagent.observability.demo import demo, python_command
from minisweagent.observability.diagnostics import diagnose, normalize_command
from minisweagent.observability.experiments import compare, evaluate, export_rows, register_evaluation
from minisweagent.observability.instrumentation import Collector, ObserveConfig, observed_agent_class
from minisweagent.observability.metrics import aggregate, run_metrics
from minisweagent.observability.schema import Evaluation, Experiment, Run, Span, Task
from minisweagent.observability.storage import Store
from minisweagent.observability.trajectory_parser import actions, import_trajectory, parse_trajectory
from minisweagent.observability.workspace import git, snapshot

DATA = Path(__file__).parents[1] / "test_data"


@pytest.mark.parametrize(("name", "steps", "nonzero"), [("local", 2, 0), ("github_issue", 10, 2)])
def test_real_historical_import(tmp_path, name, steps, nonzero):
    store = Store(tmp_path / "traces.db")
    original = (DATA / f"{name}.traj.json").read_bytes()
    run = import_trajectory(store, DATA / f"{name}.traj.json")
    assert len([s for s in run.spans if s.kind == "step"]) == steps
    assert run_metrics(run)["tool_nonzero"] == nonzero
    assert Path(run.trajectory_path).read_bytes() == original
    assert run.duration_ms is None and run_metrics(run)["cost_usd"] is None
    assert store.evaluation(run.run_id).verdict == "not_evaluated"
    assert import_trajectory(store, DATA / f"{name}.traj.json").run_id == run.run_id
    assert len(store.runs()) == 1
    assert bool(run.diff) == (name == "github_issue")


def test_native_tool_call_association_usage_and_unicode(tmp_path):
    data = {
        "info": {"config": {"model": {"model_name": "model-a"}}, "exit_status": "Submitted"},
        "messages": [
            {"role": "system", "content": "中文系统"},
            {"role": "user", "content": "修复精度"},
            {
                "role": "assistant",
                "content": "开始",
                "tool_calls": [
                    {"id": "a", "function": {"arguments": '{"command":"echo a"}'}},
                    {"id": "b", "function": {"arguments": '{"command":"echo b"}'}},
                ],
                "extra": {
                    "response": {"usage": {"prompt_tokens": 20, "completion_tokens": 10, "total_tokens": 30}},
                    "cost": 0.04,
                },
            },
            {"role": "tool", "tool_call_id": "b", "content": '{"returncode": 1, "output": "乙"}'},
            {"role": "tool", "tool_call_id": "a", "content": "<returncode>0</returncode><output>甲</output>"},
        ],
    }
    source = tmp_path / "native.traj.json"
    source.write_text(json.dumps(data), encoding="utf-8")
    store = Store(tmp_path / "traces.db")
    run = import_trajectory(store, source)
    tools = [s for s in run.spans if s.kind == "tool"]
    assert [s.attributes["output"] for s in tools] == ["甲", "乙"]
    assert [s.attributes["returncode"] for s in tools] == [0, 1]
    assert run.messages[0]["content"] == "中文系统"
    assert run_metrics(run)["total_tokens"] == 30 and run_metrics(run)["cost_usd"] == 0.04
    assert aggregate([run])["submitted"] == 1 and store.evaluation(run.run_id).verdict == "not_evaluated"
    assert all(s.parent_span_id == run.spans[0].span_id for s in run.spans[1:])
    assert store.runs(model="model-a", status="Submitted", search="乙")[0].run_id == run.run_id
    assert not store.runs(model="absent")


def test_rejected_action_is_preserved_without_counting_as_execution_or_failure():
    _, run = parse_trajectory(
        {
            "messages": [
                {
                    "object": "response",
                    "extra": {"actions": [{"command": "python -m pytest", "tool_call_id": "skipped"}]},
                },
                {
                    "type": "function_call_output",
                    "call_id": "skipped",
                    "output": "",
                    "extra": {"raw_output": "", "returncode": -1, "exception_info": "action was not executed"},
                },
                {"role": "user", "content": "我已手动修复，请重新检查。", "extra": {"interrupt_type": "UserRejection"}},
                {"role": "assistant", "extra": {"actions": [{"command": "python -m pytest"}]}},
                {"role": "user", "content": "", "extra": {"raw_output": "16 passed", "returncode": 0}},
            ]
        }
    )
    run.events = diagnose(run)
    assert run_metrics(run)["tool_actions"] == 2 and run_metrics(run)["tool_calls"] == 1
    assert run_metrics(run)["tool_skipped"] == 1 and run_metrics(run)["tool_nonzero"] == 0
    assert [(e.type, e.severity, e.step_index) for e in run.events] == [("action_not_executed", "info", 1)]
    assert run.events[0].evidence["reason"] == ["我已手动修复，请重新检查。"]
    assert next(s for s in run.spans if s.kind == "tool").attributes["returncode"] == -1


def test_responses_and_missing_observation():
    message = {
        "object": "response",
        "usage": {"input_tokens": 4, "output_tokens": 2, "total_tokens": 6},
        "output": [{"type": "function_call", "call_id": "call1", "arguments": '{"command":"pwd"}'}],
    }
    original = copy.deepcopy(message)
    _, run = parse_trajectory({"messages": [message]})
    assert actions(message) == actions(message)
    assert message == original
    assert run_metrics(run)["total_tokens"] == 6
    assert [s for s in run.spans if s.kind == "tool"][0].attributes.get("returncode") is None
    assert run.exit_status == "unknown" and not run.events and run.diff is None
    assert actions({"role": "assistant", "tool_calls": [{"function": {"arguments": "{bad"}}]}) == []
    _, empty = parse_trajectory(
        {"messages": [{"role": "assistant", "extra": {"cost": None}}], "info": {"config": None, "model_stats": None}}
    )
    assert run_metrics(empty)["cost_usd"] is None


def test_historical_format_failure_has_its_own_step():
    _, run = parse_trajectory(
        {
            "messages": [
                {"role": "assistant", "content": "```bash\necho ok\n```"},
                {"role": "user", "content": "<returncode>0</returncode><output>ok</output>"},
                {
                    "role": "user",
                    "content": "format error",
                    "extra": {
                        "interrupt_type": "FormatError",
                        "cost": 0.1,
                        "response": {"usage": {"total_tokens": 12}},
                    },
                },
                {"role": "exit", "extra": {"exit_status": "RepeatedFormatError"}},
            ]
        }
    )
    run.events = diagnose(run)
    assert run_metrics(run)["steps"] == 2 and run_metrics(run)["llm_calls"] == 2
    assert run_metrics(run)["total_tokens"] == 12 and run_metrics(run)["cost_usd"] == 0.1
    assert [e.step_index for e in run.events if e.type == "format_error"] == [2]


@pytest.mark.parametrize("data", [{}, None, {"messages": ["not a message"]}])
def test_invalid_top_level_trajectories_are_not_silently_imported(data):
    with pytest.raises(ValueError, match="message"):
        parse_trajectory(data)


@pytest.mark.parametrize(
    ("states", "expected_loop"), [(["a", "a", "a"], True), (["a", "b", "c"], False), ([None, None, None], False)]
)
def test_diagnostics_use_failure_and_state(states, expected_loop):
    run = Run("run", "task", exit_status="Submitted")
    for index, state in enumerate(states, 1):
        run.spans.append(Span(run.run_id, "step", index, attributes={"message_indices": []}))
        run.spans.append(
            Span(
                run.run_id,
                "tool",
                index,
                attributes={
                    "command": "python -m pytest -q",
                    "cwd": "/repo",
                    "returncode": 1,
                    "output": f"FAILED test_bug.py::test_fraction - AssertionError\n1 failed in {index}.01s",
                    "state_hash": state,
                    "state_before": state,
                },
            )
        )
    events = diagnose(run)
    assert all(e.type == "suspected_no_progress" for e in events)
    assert any(e.type == "suspected_no_progress" for e in events) == expected_loop
    assert all(e.step_index in (1, 2, 3) and e.evidence for e in events)
    assert not any(e.type == "termination" for e in events)
    run.spans[-1].attributes.update(returncode=0, output="3 passed")
    assert not any(e.type == "suspected_no_progress" for e in diagnose(run))
    assert normalize_command(' echo   "a  b" ') == 'echo "a  b"'


class ParsedTextModel(DeterministicModel):
    """A scripted text source using the repository's actual action parser."""

    def query(self, *args, **kwargs):
        message = super().query(*args, **kwargs)
        message["extra"]["actions"] = parse_regex_actions(
            message["content"], action_regex=r"```bash\n(.*?)```", format_error_template="{{error}}"
        )
        return message


def scripted(commands: list[str], model_class=DeterministicModel):
    return model_class(
        outputs=[
            {"role": "assistant", "content": f"```bash\n{command}\n```", "extra": {"actions": [{"command": command}]}}
            for command in commands
        ],
        cost_per_call=0,
    )


def create_repo(path: Path) -> Path:
    path.mkdir()
    (path / "bug.py").write_text("value = 1\n", encoding="utf-8")
    git(path, "init", "--quiet")
    git(path, "add", ".")
    git(path, "-c", "user.name=test", "-c", "user.email=test@localhost", "commit", "--quiet", "-m", "bug")
    return path


def test_wrapper_preserves_real_agent_behavior_and_spans(tmp_path):
    repo = create_repo(tmp_path / "repo")
    store = Store(tmp_path / "traces.db")
    store.save_task(Task("task", "中文任务", repo_path=str(repo)))
    command = python_command("print('observation'); from pathlib import Path; Path('bug.py').write_text('value = 2')")
    config = {"system_template": "Same system", "instance_template": "{{task}}", "cost_limit": 0}
    commands = [command, "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"]
    regular = DefaultAgent(scripted(commands), LocalEnvironment(cwd=str(repo)), **config)
    expected = regular.run("中文修复")
    expected_messages = copy.deepcopy(regular.messages)
    (repo / "bug.py").write_text("value = 1\n", encoding="utf-8")
    collector = Collector(store, ObserveConfig("task", str(repo)))
    observed = observed_agent_class(DefaultAgent)(
        scripted(commands), LocalEnvironment(cwd=str(repo)), collector=collector, **config
    )
    assert observed.run("中文修复") == expected
    for actual, expected_message in zip(observed.messages, expected_messages):
        assert actual["role"] == expected_message["role"] and actual["content"] == expected_message["content"]
        assert actual.get("extra", {}).get("actions") == expected_message.get("extra", {}).get("actions")
    run = store.run(collector.run.run_id)
    assert (repo / "bug.py").read_text(encoding="utf-8") == "value = 2"
    assert run.diff and "+value = 2" in run.diff
    assert run.duration_ms > 0 and run.exit_status == "Submitted"
    assert {s.kind for s in run.spans} == {"run", "step", "llm", "tool"}
    assert all(s.duration_ms >= 0 and s.start_time <= s.end_time for s in run.spans)
    assert all(s.parent_span_id in {x.span_id for x in run.spans} for s in run.spans if s.kind != "run")
    assert run_metrics(run)["total_tokens"] is None
    assert json.loads(Path(run.trajectory_path).read_text(encoding="utf-8"))["info"]["exit_status"] == "Submitted"


def test_format_error_api_exception_and_limits_persist_without_swallowing(tmp_path):
    store = Store(tmp_path / "traces.db")
    store.save_task(Task("task", "task"))
    collector = Collector(store, ObserveConfig("task"))
    model = scripted(["echo ignored"], ParsedTextModel)
    model.config.outputs[0]["content"] = "invalid action"
    agent = observed_agent_class(DefaultAgent)(
        model,
        LocalEnvironment(),
        collector=collector,
        system_template="system",
        instance_template="{{task}}",
        max_consecutive_format_errors=1,
    )
    assert agent.run("task")["exit_status"] == "RepeatedFormatError"
    run = store.run(collector.run.run_id)
    assert {e.type for e in run.events} == {"format_error", "termination"}
    assert next(e for e in run.events if e.type == "format_error").step_index == 1
    collector = Collector(store, ObserveConfig("task"))
    agent = observed_agent_class(DefaultAgent)(
        scripted([]), LocalEnvironment(), collector=collector, system_template="system", instance_template="{{task}}"
    )
    with pytest.raises(IndexError):
        agent.run("task")
    assert store.run(collector.run.run_id).exit_status == "IndexError"
    assert any(e.type == "api_error" for e in store.run(collector.run.run_id).events)
    collector = Collector(store, ObserveConfig("task"))
    agent = observed_agent_class(DefaultAgent)(
        scripted(["echo ok"]),
        LocalEnvironment(),
        collector=collector,
        system_template="system",
        instance_template="{{task}}",
        step_limit=1,
        cost_limit=0,
    )
    assert agent.run("task")["exit_status"] == "LimitsExceeded"
    assert store.run(collector.run.run_id).events[-1].evidence["exit_status"] == "LimitsExceeded"
    assert all(event.severity == "info" for event in store.run(collector.run.run_id).events)


def test_tool_timeout_records_evidence(tmp_path):
    store = Store(tmp_path / "traces.db")
    store.save_task(Task("task", "task"))
    collector = Collector(store, ObserveConfig("task"))
    agent = observed_agent_class(DefaultAgent)(
        scripted([python_command("import time; time.sleep(2)"), "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"]),
        LocalEnvironment(timeout=1),
        collector=collector,
        system_template="system",
        instance_template="{{task}}",
        cost_limit=0,
    )
    assert agent.run("task")["exit_status"] == "Submitted"
    assert any(e.type == "tool_timeout" and "command" in e.evidence for e in store.run(collector.run.run_id).events)


@pytest.fixture(scope="module")
def demo_store(tmp_path_factory):
    store = Store(tmp_path_factory.mktemp("demo") / "traces.db")
    demo(store, DATA)
    return store


def test_end_to_end_isolation_evaluation_comparison_and_export(demo_store):
    store = demo_store
    experiment = store.experiments()[0]
    rows = compare(store, experiment.run_ids)
    assert [r["verdict"] for r in rows] == ["failed", "passed"]
    assert rows[0]["initial_state"] == rows[1]["initial_state"]
    assert rows[0]["base_revision"] == rows[1]["base_revision"]
    assert all(r["duration_ms"] > 0 and r["cost_usd"] is None for r in rows)
    bad, good = [store.run(rid) for rid in experiment.run_ids]
    assert bad.workspace != good.workspace and bad.diff is None and good.diff
    assert any(e.type == "suspected_no_progress" for e in bad.events)
    assert not any(e.type == "suspected_no_progress" for e in good.events)
    assert store.evaluation(bad.run_id).returncode == 1 and store.evaluation(good.run_id).returncode == 0
    assert all(Path(store.evaluation(r.run_id).evidence_path).is_file() for r in (bad, good))
    assert json.loads(export_rows(rows))[0]["run_id"] == bad.run_id
    assert list(csv.DictReader(io.StringIO(export_rows(rows, "csv"))))[1]["verdict"] == "passed"
    assert len(store.runs()) == 4 and len(store.tasks()) == 3
    with pytest.raises(ValueError, match="existing runs"):
        store.save_experiment(Experiment("invalid", ["absent"]))


def test_evaluation_unknown_changed_workspace_external_and_timeout(tmp_path):
    store = Store(tmp_path / "traces.db")
    repo = create_repo(tmp_path / "repo")
    store.save_task(Task("task", "task"))
    run = Run("run", "task", workspace=str(repo), metadata={"final_state": snapshot(repo)[1]})
    store.save_run(run)
    assert evaluate(store, "run").verdict == "not_evaluated"
    store.save_task(Task("task", "task", test_command=python_command("import time; time.sleep(2)")))
    assert evaluate(store, "run", timeout=1).verdict == "evaluation_error"
    (repo / "bug.py").write_text("changed", encoding="utf-8")
    assert "changed since" in evaluate(store, "run").test_summary
    assert store.evaluation("run").returncode is None
    evidence = tmp_path / "external.json"
    evidence.write_text('{"resolved": true}', encoding="utf-8")
    register_evaluation(
        store, Evaluation("run", evaluator="external/source", verdict="passed", evidence_path=str(evidence))
    )
    assert store.evaluation("run").evaluator == "external/source"
    assert Path(store.evaluation("run").evidence_path).read_bytes() == evidence.read_bytes()
    with pytest.raises(ValueError, match="evidence_path"):
        register_evaluation(store, Evaluation("run", verdict="passed"))


def test_committed_changes_are_in_diff_and_state_validation(tmp_path):
    repo = create_repo(tmp_path / "repo")
    base = git(repo, "rev-parse", "HEAD")
    initial = snapshot(repo)[1]
    (repo / "bug.py").write_text("value = 2\n", encoding="utf-8")
    git(repo, "add", ".")
    git(repo, "-c", "user.name=test", "-c", "user.email=test@localhost", "commit", "--quiet", "-m", "fix")
    assert snapshot(repo)[1] != initial and "+value = 2" in snapshot(repo, base)[0]
    store = Store(tmp_path / "traces.db")
    store.save_task(Task("task", "task", test_command="echo test"))
    store.save_run(Run("run", "task", workspace=str(repo), metadata={"final_state": initial}))
    assert evaluate(store, "run").verdict == "evaluation_error"
    (repo / "new_file.py").write_text("title = '中文文件'\n", encoding="utf-8")
    assert "+title = '中文文件'" in snapshot(repo, base)[0]
    assert "new_file.py" in snapshot(repo, base)[0] and snapshot(repo)[1] != initial


def test_dashboard_pages_playback_and_comparison(demo_store):
    from streamlit.testing.v1 import AppTest

    dashboard = Path(__file__).parents[2] / "src/minisweagent/observability/dashboard.py"
    app = AppTest.from_file(str(dashboard), default_timeout=30).run()
    next(w for w in app.text_input if w.label == "SQLite 数据库").set_value(str(demo_store.path)).run()
    assert not app.exception
    next(w for w in app.button if w.label == "下一步").click().run()
    assert next(w for w in app.radio if w.label == "选择步骤").value == 2 and not app.exception
    for page in ("监控", "失败分析", "任务与对比"):
        app.sidebar.radio[0].set_value(page).run()
        assert not app.exception
    next(w for w in app.multiselect if w.label.startswith("选择任意 Run")).set_value(
        demo_store.experiments()[0].run_ids
    ).run()
    assert not app.exception and any(w.label == "导出 CSV" for w in app.get("download_button"))
    configuration = demo_store.path.parent / "ui-config.yaml"
    configuration.write_text(
        json.dumps(
            {
                "agent": {"system_template": "system", "instance_template": "{{task}}", "cost_limit": 0},
                "model": {
                    "model_name": "scripted-ui",
                    "model_class": "minisweagent.models.test_models.DeterministicModel",
                    "cost_per_call": 0,
                    "outputs": [
                        {
                            "role": "assistant",
                            "content": "提交",
                            "extra": {"actions": [{"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"}]},
                        }
                    ],
                },
            }
        ),
        encoding="utf-8",
    )
    next(w for w in app.selectbox if w.label == "任务").set_value(
        demo_store.run(demo_store.experiments()[0].run_ids[0]).task_id
    )
    next(w for w in app.text_area if w.label.startswith("配置规格")).set_value(str(configuration))
    next(w for w in app.checkbox if w.label == "执行任务的真实验证命令").uncheck()
    before = len(demo_store.runs())
    next(w for w in app.button if w.label == "启动真实运行").click().run()
    assert not app.exception and len(demo_store.runs()) == before + 1
    assert demo_store.evaluation(demo_store.runs()[0].run_id).verdict == "not_evaluated"

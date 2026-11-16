import json
import sys
from copy import deepcopy
from pathlib import Path

import pytest
from litellm.exceptions import BadRequestError

from minisweagent.agents import get_agent
from minisweagent.agents.enhanced import EnhancedAgent
from minisweagent.environments.local import LocalEnvironment
from minisweagent.exceptions import FormatError, LimitsExceeded
from minisweagent.models.test_models import (
    DeterministicModel,
    DeterministicResponseAPIToolcallModel,
    DeterministicToolcallModel,
    make_output,
    make_response_api_output,
    make_toolcall_output,
)
from minisweagent.observability.instrumentation import Collector, ObserveConfig, observed_agent_class
from minisweagent.observability.metrics import run_metrics
from minisweagent.observability.schema import Task
from minisweagent.observability.storage import Store
from minisweagent.optimization.context_manager import ContextCapacityError, ContextConfig, ContextManager
from minisweagent.optimization.task_state import message_text
from minisweagent.run.benchmarks.swebench import process_instance
from minisweagent.run.benchmarks.utils.batch_progress import RunBatchProgressManager
from minisweagent.run.benchmarks.utils.common import get_progress_agent_class


@pytest.fixture(params=[DeterministicModel, DeterministicToolcallModel, DeterministicResponseAPIToolcallModel])
def model(request):
    return request.param(outputs=[])


def history(model):
    return [
        model.format_message(role="system", content="Keep constraints"),
        model.format_message(role="user", content="Fix issue"),
    ]


def add_turn(model, messages, commands, outputs, note="I suspect the parser; next verify the hypothesis"):
    actions = [{"command": command, "tool_call_id": f"call_{len(messages)}_{i}"} for i, command in enumerate(commands)]
    if isinstance(model, DeterministicResponseAPIToolcallModel):
        reply = make_response_api_output(note, actions)
    elif isinstance(model, DeterministicToolcallModel):
        reply = make_toolcall_output(
            note,
            [
                {
                    "id": a["tool_call_id"],
                    "type": "function",
                    "function": {"name": "bash", "arguments": json.dumps({"command": a["command"]})},
                }
                for a in actions
            ],
            actions,
        )
    else:
        reply = make_output(note, [{"command": command} for command in commands])
    messages.append(reply)
    messages.extend(model.format_observation_messages(reply, outputs))


def output(text, returncode=0, exception_info=""):
    return {"output": text, "returncode": returncode, "exception_info": exception_info}


def assert_pairs(messages):
    pending = set()
    for message in messages:
        if message.get("role") == "assistant":
            assert not pending
            pending.update(c["id"] for c in message.get("tool_calls", []))
        elif message.get("object") == "response":
            assert not pending
            pending.update(c["call_id"] for c in message["output"] if c["type"] == "function_call")
        result_id = message.get("tool_call_id") or (
            message.get("call_id") if message.get("type") == "function_call_output" else None
        )
        if result_id:
            assert result_id in pending
            pending.remove(result_id)
    assert not pending


def test_event_elision_memory_and_native_pairs(model):
    messages = history(model)
    add_turn(
        model,
        messages,
        ["cat src/parser.py", "pytest tests/test_parser.py"],
        [output("def parse():\n" + "old code\n" * 300), output("FAILED test_parse\n" * 200, 1)],
    )
    add_turn(
        model,
        messages,
        ["cat src/parser.py", "pytest tests/test_parser.py"],
        [output("def parse():\nnew code"), output("1 passed")],
    )
    add_turn(model, messages, ["echo checking"], [output("checked")])
    original = deepcopy(messages)
    manager = ContextManager(ContextConfig(context_window=200000, keep_recent_turns=1), model, "Fix issue")
    view = manager.build(messages)
    assert messages == original
    assert set(manager.elisions) == {3, 4}
    assert "superseded" in message_text(view[3])
    assert view[2] == messages[2]
    assert view[-3:-1] == messages[-2:]
    assert "<task_memory>" in message_text(view[-1])
    assert "src/parser.py" in manager.task_state.files
    assert "parse" in manager.task_state.symbols
    assert manager.task_state.tests["pytest tests/test_parser.py"].returncode == 0
    assert not manager.task_state.unresolved
    assert all(item.status == "unverified" for item in manager.task_state.hypotheses.values())
    assert manager.task_state.next_steps
    assert_pairs(view)
    assert manager.build(messages) == view
    assert len(manager.events) == 2


def test_different_windows_failures_and_user_constraints_are_protected(model):
    messages = history(model)
    add_turn(
        model,
        messages,
        ["sed -n '1,100p' src/parser.py", "cat missing.py"],
        [output("A" * 3000), output("No such file" * 300, 1)],
    )
    messages.append(model.format_message(role="user", content="Preserve backwards compatibility"))
    add_turn(
        model, messages, ["sed -n '101,200p' src/parser.py", "cat missing.py"], [output("B" * 3000), output("found it")]
    )
    add_turn(model, messages, ["echo next"], [output("done")])
    manager = ContextManager(ContextConfig(context_window=30000, keep_recent_turns=1), model)
    assert manager.build(messages) == messages
    assert not manager.elisions


def test_window_compaction_is_stable_and_preserves_complete_interactions(model):
    messages = history(model)
    for path in ("first", "second"):
        add_turn(
            model,
            messages,
            [f"cat src/{path}.py", "echo inspected"],
            [output("x" * 6000 + "\nValueError: invalid parser state\n" + "y" * 6000), output("done")],
        )
    add_turn(model, messages, ["echo next"], [output("next")])
    original = deepcopy(messages)
    manager = ContextManager(
        ContextConfig(context_window=10000, reserved_output_tokens=1000, keep_recent_turns=1, summary_chars=128), model
    )
    view = manager.build(messages)
    assert manager.checkpoints
    assert manager.requests[-1]["estimated_input_tokens"] + 1000 <= 10000
    assert messages == original
    assert view[:2] == messages[:2]
    assert view[-3:-1] == messages[-2:]
    assert "ValueError: invalid parser state" in "\n".join(message_text(m) for m in view)
    assert_pairs(view)
    checkpoints = deepcopy(manager.checkpoints)
    assert manager.build(messages) == view
    assert manager.checkpoints == checkpoints
    add_turn(model, messages, ["echo later"], [output("later")])
    manager.build(messages)
    assert manager.checkpoints[: len(checkpoints)] == checkpoints
    assert len({e["start"] for e in manager.events if e["type"] == "compaction"}) == len(manager.checkpoints)


def test_incomplete_multicall_and_thinking_are_never_split():
    from minisweagent.models.litellm_model import LitellmModel

    model = DeterministicToolcallModel(outputs=[])
    messages = history(model)
    add_turn(model, messages, ["cat src/a.py", "cat src/b.py"], [output("a" * 3000), output("b" * 3000)])
    messages[2]["content"] = [
        {"type": "thinking", "thinking": "private reasoning", "signature": "signed"},
        {"type": "text", "text": "Inspect files"},
    ]
    missing = messages.pop()
    add_turn(model, messages, ["echo next"], [output("next")])
    manager = ContextManager(ContextConfig(context_window=20000, keep_recent_turns=1), model)
    assert manager.build(messages) == messages
    assert not manager._interactions(messages)[0].complete
    assert not manager.checkpoints
    # Restore a complete tool exchange and check that the signed blocks survive an untouched view.
    messages.insert(4, missing)
    manager = ContextManager(ContextConfig(context_window=20000, keep_recent_turns=1), model)
    assert manager.build(messages)[2]["content"] == messages[2]["content"]
    assert_pairs(manager.build(messages))
    prepared = LitellmModel(model_name="anthropic/test", set_cache_control="default_end")._prepare_messages_for_api(
        manager.build(messages)
    )
    assert prepared[2]["content"][0] == {"type": "thinking", "thinking": "private reasoning", "signature": "signed"}
    assert prepared[-1]["cache_control"] == {"type": "ephemeral"}


def test_uncompressible_context_reports_capacity_without_losing_state(model):
    messages = history(model)
    add_turn(model, messages, ["pytest tests/test_parser.py"], [output("critical failure\n" * 1000, 1)])
    manager = ContextManager(
        ContextConfig(context_window=6000, reserved_output_tokens=1000, keep_recent_turns=1), model
    )
    with pytest.raises(ContextCapacityError, match="Full history and task memory are preserved"):
        manager.build(messages)
    assert manager.task_state.tests["pytest tests/test_parser.py"].returncode == 1
    assert manager.requests[-1]["source_indices"] == list(range(len(messages)))
    assert len(messages) == 4


def test_no_gain_keeps_raw_turn_and_does_not_repeatedly_summarize(model):
    messages = history(model)
    for _ in range(5):
        add_turn(model, messages, ["echo tiny"], [output("tiny")])
    manager = ContextManager(
        ContextConfig(
            context_window=10000, reserved_output_tokens=1000, compaction_threshold=0.01, keep_recent_turns=1
        ),
        model,
    )
    assert manager.build(messages) == messages
    assert not manager.checkpoints
    events = deepcopy(manager.events)
    assert manager.build(messages) == messages
    assert manager.events == events
    assert all(e["type"] == "compaction_skipped" for e in events)


def submit_command():
    return f"\"{sys.executable}\" -c \"print('COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT'); print('done')\""


def test_enhanced_run_reuse_costs_and_saved_memory(tmp_path):
    model = DeterministicModel(outputs=[make_output("next run", [{"command": submit_command()}]) for _ in range(2)])
    agent = get_agent(
        model,
        LocalEnvironment(),
        {
            "agent_class": "enhanced",
            "system_template": "Constraints",
            "instance_template": "{{task}}",
            "output_path": tmp_path / "enhanced.json",
        },
    )
    assert isinstance(agent, EnhancedAgent)
    assert agent.run("first issue")["exit_status"] == "Submitted"
    agent.context_manager.events.append({"type": "first_issue_only"})
    assert agent.run("second issue")["exit_status"] == "Submitted"
    data = json.loads((tmp_path / "enhanced.json").read_text(encoding="utf-8"))
    assert agent.n_calls == 1
    assert agent.cost == 1
    assert data["messages"] == agent.messages
    assert data["context_engineering"]["task_state"]["issue"] == "second issue"
    assert data["context_engineering"]["task_state"]["stage"] == "Submitted"
    assert not data["context_engineering"]["events"]
    assert "first issue" not in json.dumps(data)


def test_format_error_billing_and_existing_limits_are_reused(tmp_path):
    class BilledFormatErrorModel(DeterministicModel):
        def query(self, messages, **kwargs):
            raise FormatError({"role": "user", "content": "invalid response", "extra": {"cost": 2.0}})

    agent = EnhancedAgent(
        BilledFormatErrorModel(outputs=[]),
        LocalEnvironment(),
        system_template="S",
        instance_template="{{task}}",
        cost_limit=1,
        output_path=tmp_path / "error.json",
    )
    assert agent.run("format failure")["exit_status"] == "LimitsExceeded"
    assert agent.cost == 2
    assert agent.n_calls == 1
    assert agent.messages[-2]["content"] == "invalid response"
    assert json.loads((tmp_path / "error.json").read_text())["info"]["model_stats"]["instance_cost"] == 2


def test_invalid_command_observation_keeps_environment_error_and_original_action(tmp_path):
    agent = EnhancedAgent(
        DeterministicModel(outputs=[]),
        LocalEnvironment(),
        system_template="S",
        instance_template="{{task}}",
        output_path=tmp_path / "invalid.json",
    )
    agent.add_messages(*history(agent.model), make_output("Invalid command", [{"command": 37}]))
    assert agent.execute_actions(agent.messages[-1])[0]["extra"]["returncode"] == -1
    assert agent.get_model_messages() == agent.messages
    agent.save(agent.config.output_path)
    assert agent.messages[2]["extra"]["actions"][0]["command"] == 37
    assert agent.messages[3]["extra"]["returncode"] == -1
    assert agent.context_manager.task_state.unresolved["37"].source == 3
    assert json.loads((tmp_path / "invalid.json").read_text())["messages"][3]["extra"]["exception_info"]


def test_bad_request_recovers_raw_history_and_keeps_limit_checks():
    error = BadRequestError(message="invalid messages", model="deterministic", llm_provider="test")
    model = DeterministicModel(outputs=[make_output("", [{"raise": error}]), make_output("recovered", [])])
    agent = EnhancedAgent(
        model,
        LocalEnvironment(),
        system_template="S",
        instance_template="{{task}}",
        context={"context_window": 100000, "keep_recent_turns": 1},
    )
    messages = history(model)
    add_turn(model, messages, ["cat src/a.py"], [output("old" * 1000)])
    add_turn(model, messages, ["cat src/a.py"], [output("new")])
    agent.add_messages(*messages)
    assert agent.query()["content"] == "recovered"
    assert agent.context_manager.raw_history
    assert agent.context_manager.requests[-1]["source_indices"] == list(range(len(messages)))
    assert agent.n_calls == 2
    agent.config.step_limit = 2
    with pytest.raises(LimitsExceeded):
        agent.query()


def test_swebench_progress_wrapper_selects_enhanced_and_retains_raw_config(tmp_path):
    manager = RunBatchProgressManager(1)
    manager.on_instance_start("test")
    config = {"system_template": "Constraints", "instance_template": "{{task}}", "step_limit": 1}
    agent = get_progress_agent_class("enhanced")(
        DeterministicModel(outputs=[make_output("next", [{"command": "echo hello"}])]),
        LocalEnvironment(),
        progress_manager=manager,
        instance_id="test",
        **config,
    )
    assert isinstance(agent, EnhancedAgent)
    assert agent.run("SWE-bench task")["exit_status"] == "LimitsExceeded"
    assert len(agent.context_manager.requests) == 1
    assert agent.save(tmp_path / "bench.json")["context_engineering"]["task_state"]["issue"] == "SWE-bench task"
    assert config["step_limit"] == 1


def test_observed_enhanced_run_compacts_and_preserves_usage_and_actions(tmp_path):
    store = Store(tmp_path / "traces.db")
    store.save_task(Task("context", "Context integration"))
    collector = Collector(store, ObserveConfig("context"))
    commands = [f'"{sys.executable}" -c "print(\'x\' * 3500)"'] * 2 + [submit_command()]
    outputs = [make_output("Next verify output", [{"command": command}], cost=0.25) for command in commands]
    for reply in outputs:
        reply["extra"]["response"] = {"usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13}}
    agent = observed_agent_class(EnhancedAgent)(
        DeterministicModel(outputs=outputs, cost_per_call=0.25),
        LocalEnvironment(),
        collector=collector,
        system_template="S",
        instance_template="{{task}}",
        context={"context_window": 9000, "reserved_output_tokens": 1000, "keep_recent_turns": 1, "summary_chars": 128},
    )
    assert agent.run("Verify observed compaction")["exit_status"] == "Submitted"
    run = store.run(collector.run.run_id)
    data = json.loads(Path(run.trajectory_path).read_text(encoding="utf-8"))
    assert data["context_engineering"]["checkpoints"]
    assert len(data["messages"]) == 8
    assert len(data["messages"][3]["extra"]["raw_output"]) > 3500
    assert run.messages == data["messages"]
    assert run_metrics(run)["total_tokens"] == 39
    assert run_metrics(run)["cost_usd"] == 0.75
    assert run_metrics(run)["llm_calls"] == 3
    assert run_metrics(run)["tool_calls"] == 3


def test_metadata_reservation_and_accumulated_usage_do_not_trigger_compaction():
    from minisweagent.models.litellm_model import LitellmModel

    model = LitellmModel(model_name="openai/gpt-4o", model_kwargs={"max_tokens": 6000})
    manager = ContextManager(ContextConfig(), model)
    assert manager._limits()[1:] == (6000, "metadata")
    assert ContextManager(ContextConfig(context_window=64000), model)._limits() == (64000, 6000, "override")
    model = DeterministicModel(outputs=[])
    agent = EnhancedAgent(model, LocalEnvironment(), system_template="S", instance_template="{{task}}")
    agent.add_messages(*history(model))
    agent.cost = 1000000000
    agent.n_calls = 100000
    assert agent.get_model_messages() == agent.messages
    assert not agent.context_manager.checkpoints
    assert agent.context_manager.requests[-1]["window_source"] == "fallback"


def test_memory_distinguishes_edit_intent_observed_diff_and_command_errors(model):
    messages = history(model)
    add_turn(
        model,
        messages,
        ["cat missing.py", "rg absent src", "python -c 'path.write_text(value)'"],
        [output("File missing", 1), output("", 1), output("edit command completed")],
    )
    add_turn(model, messages, ["git diff --name-only"], [output("src/parser.py\ntests/test_parser.py\n")])
    manager = ContextManager(ContextConfig(context_window=100000), model)
    manager.build(messages)
    state = manager.task_state
    assert "cat missing.py" in state.unresolved
    assert "rg absent src" not in state.unresolved
    assert state.changes["src/parser.py"].status == "observed"
    assert state.changes["5"].status == "unverified"
    assert state.changes["5"].returncode == 0


@pytest.mark.parametrize(("agent_class", "is_enhanced"), [("default", False), ("enhanced", True)])
def test_swebench_process_instance_selects_agent_and_writes_predictions(tmp_path, agent_class, is_enhanced):
    config = {
        "agent": {"agent_class": agent_class, "system_template": "S", "instance_template": "{{task}}", "cost_limit": 0},
        "environment": {"environment_class": "local", "cwd": str(tmp_path)},
        "model": {
            "model_class": "minisweagent.models.test_models.DeterministicModel",
            "model_name": "deterministic",
            "outputs": [
                make_output("Inspect", [{"command": "echo first"}]),
                make_output("Finish", [{"command": submit_command()}]),
            ],
        },
    }
    original = deepcopy(config)
    process_instance(
        {"instance_id": "example__issue-1", "problem_statement": "Fix issue"},
        tmp_path,
        config,
        RunBatchProgressManager(1),
    )
    data = json.loads((tmp_path / "example__issue-1" / "example__issue-1.traj.json").read_text(encoding="utf-8"))
    assert data["info"]["exit_status"] == "Submitted"
    assert data["info"]["model_stats"]["api_calls"] == 2
    assert ("context_engineering" in data) == is_enhanced
    assert json.loads((tmp_path / "preds.json").read_text())["example__issue-1"]["model_patch"].strip() == "done"
    assert config == original

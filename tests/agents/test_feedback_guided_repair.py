import json
import os
import shlex
import shutil
import subprocess
import sys
from copy import deepcopy

import pytest

from minisweagent.agents.default import DefaultAgent
from minisweagent.agents.enhanced import EnhancedAgent
from minisweagent.environments.docker import DockerEnvironment
from minisweagent.environments.local import LocalEnvironment
from minisweagent.models.test_models import DeterministicModel, make_output
from minisweagent.observability.instrumentation import Collector, ObserveConfig, observed_agent_class
from minisweagent.observability.metrics import run_metrics
from minisweagent.observability.schema import Task
from minisweagent.observability.storage import Store
from minisweagent.optimization.context_manager import ContextCapacityError, ContextConfig, ContextManager
from minisweagent.optimization.feedback_manager import FeedbackConfig, FeedbackManager, parse_feedback
from minisweagent.optimization.task_state import message_text
from tests.agents.test_enhanced import add_turn, assert_pairs, history, model, output  # noqa: F401


def pytest_failure(
    node="tests/test_bug.py::test_value", value=1, temporary="/tmp/pytest-of-dev/pytest-10", address="0x1234"
):
    return (
        "________________ test_value ________________\n"
        f"{temporary}/test_value/probe.py:42: AssertionError\n"
        f"E       assert {value} == 2\n"
        f"E       AssertionError: value at {address} in {temporary}/test_value/probe.py\n"
        "================ short test summary info ================\n"
        f"FAILED {node} - assert {value} == 2\n"
        "================ 1 failed in 0.12s ================\n"
    )


PASS = "tests/test_bug.py::test_value PASSED [100%]\n================ 1 passed in 0.03s ================\n"


@pytest.mark.parametrize(
    ("command",),  # noqa: PT006
    [
        ("pytest -q tests/test_bug.py",),
        ("python3.11 -m pytest tests/test_bug.py",),
        ("cd /testbed && TOKEN=value uv run python -m pytest tests/test_bug.py",),
        ("poetry run pytest tests/test_bug.py || true",),
        ("coverage run -m pytest tests/test_bug.py",),
        ("python -m coverage run -m pytest tests/test_bug.py",),
        ("python manage.py test app",),
        ("python runtests.py tests.test_bug",),
        ("./run_tests.sh",),
        ("make test",),
    ],
)
def test_pytest_command_wrappers_evidence_and_masked_exit(command):
    parsed = parse_feedback(command, output(pytest_failure(), 0 if "true" in command else 1), FeedbackConfig())
    assert parsed.classification == "test" and parsed.outcome == "failed"
    assert parsed.failures[0].node == "tests/test_bug.py::test_value"
    assert parsed.failures[0].failure_type == "AssertionError"
    assert any("assert 1 == 2" in item for item in parsed.failures[0].evidence)
    assert parsed.failures[0].locations[0]["line"] == 42


@pytest.mark.parametrize(
    ("command", "text", "returncode", "classification", "outcome"),
    [
        ("rg absent src", "", 1, "command", "nonzero"),
        ("cat old-test.log", pytest_failure(), 1, "command", "nonzero"),
        ("echo pytest", PASS, 0, "command", "completed"),
        ("python -c 'print(\"pytest\")'", PASS, 0, "command", "completed"),
        ("pytest", "no tests ran in 0.01s", 5, "test", "inconclusive"),
        ("pytest", "1 skipped in 0.01s", 0, "test", "inconclusive"),
        ("pytest --help", "usage: pytest [options]", 0, "test", "inconclusive"),
        ("pytest", "", 1, "test", "inconclusive"),
        ("pytest", "KeyboardInterrupt\nno tests ran", 130, "test", "interrupted"),
        ("pytest", "docker: Error response from daemon: container stopped", 1, "system_error", "inconclusive"),
        ("python -m pytest", "python: No module named pytest", 1, "system_error", "inconclusive"),
        ("pytest", pytest_failure() + "\n1 failed\n", -9, "system_error", "interrupted"),
    ],
)
def test_execution_classification_is_not_just_the_exit_code(command, text, returncode, classification, outcome):
    parsed = parse_feedback(command, output(text, returncode), FeedbackConfig())
    assert parsed.classification == classification and parsed.outcome == outcome
    if classification != "test":
        assert not parsed.failures


def test_unittest_collection_reproduction_and_project_commands():
    unittest = (
        "test_value (tests.test_bug.Case.test_value) ... FAIL\n"
        "======================================================================\n"
        "FAIL: test_value (tests.test_bug.Case.test_value)\n"
        "----------------------------------------------------------------------\n"
        "Traceback (most recent call last):\n"
        '  File "/testbed/tests/test_bug.py", line 7, in test_value\n'
        "    self.assertEqual(1, 2)\nAssertionError: 1 != 2\n"
        "Ran 1 test in 0.001s\nFAILED (failures=1)\n"
    )
    parsed = parse_feedback("python -m unittest tests.test_bug -v", output(unittest, 1), FeedbackConfig())
    assert parsed.outcome == "failed" and parsed.complete
    assert parsed.failures[0].node == "test_value (tests.test_bug.Case.test_value)"
    assert parsed.failures[0].failure_type == "AssertionError" and parsed.counts["failed"] == 1
    assert (
        parse_feedback("python -m unittest", output("Ran 1 test in 0.01s\nOK (skipped=1)\n"), FeedbackConfig()).outcome
        == "inconclusive"
    )
    collection = "ImportError while importing test module\nE   ModuleNotFoundError: No module named 'dependency'\nERROR tests/test_bug.py\n1 error during collection\n"
    parsed = parse_feedback("pytest", output(collection, 2), FeedbackConfig())
    assert parsed.classification == "test" and parsed.outcome == "failed" and not parsed.complete
    assert parsed.failures[0].failure_type == "ModuleNotFoundError"
    reproduction = (
        'Traceback (most recent call last):\n  File "reproduce.py", line 9, in <module>\nAssertionError: bad result\n'
    )
    assert parse_feedback("python reproduce.py", output(reproduction, 1), FeedbackConfig()).outcome == "failed"
    assert parse_feedback("python checks.py", output(pytest_failure(), 1), FeedbackConfig()).classification == "command"
    assert (
        parse_feedback(
            "python checks.py",
            output(pytest_failure(), 1),
            FeedbackConfig(test_command_patterns=[r"python checks\.py"]),
        ).outcome
        == "failed"
    )


def test_signatures_ignore_volatile_details_but_preserve_distinct_assertions_and_nodes():
    first = parse_feedback("pytest", output(pytest_failure(), 1), FeedbackConfig()).failures[0]
    same = parse_feedback(
        "pytest -q",
        output(pytest_failure(temporary="/tmp/pytest-of-dev/pytest-99", address="0xFFFF"), 1),
        FeedbackConfig(),
    ).failures[0]
    assert first.signature == same.signature
    assert (
        first.signature
        != parse_feedback("pytest", output(pytest_failure(value=3), 1), FeedbackConfig()).failures[0].signature
    )
    assert (
        first.signature
        != parse_feedback("pytest", output(pytest_failure(node="tests/test_bug.py::test_other"), 1), FeedbackConfig())
        .failures[0]
        .signature
    )
    multiple = parse_feedback(
        "pytest",
        output("FAILED a.py::test_a - ValueError: first\nFAILED b.py::test_b - TypeError: second\n2 failed", 1),
        FeedbackConfig(),
    )
    assert [failure.failure_type for failure in multiple.failures] == ["ValueError", "TypeError"]
    assert multiple.failures[0].signature != multiple.failures[1].signature


def feedback_turn(manager, model, messages, command, text, returncode=0, note="Inspect the parser and verify"):
    add_turn(model, messages, [command], [manager.prepare({"command": command}, output(text, returncode))], note=note)


def test_repeated_failures_edits_reflection_dedup_response_and_scope_safe_resolution(model):
    feedback = FeedbackManager(FeedbackConfig(enabled=True, repeat_failure_threshold=2))
    context = ContextManager(ContextConfig(context_window=100000), model, "Fix bug", feedback)
    messages = history(model)
    feedback_turn(feedback, model, messages, "pytest -q tests/test_bug.py", pytest_failure(), 1)
    context.ingest(messages)
    add_turn(model, messages, ["sed -i 's/1/2/' bug.py"], [output("edited")], note="Try another hypothesis")
    context.ingest(messages)
    feedback_turn(feedback, model, messages, "pytest -v tests/test_bug.py", pytest_failure(), 1)
    view = context.build(messages)
    assert len(feedback.reflections) == 1 and feedback.streak == 2
    assert feedback.reflections[0]["changes"][0]["source"] == 5
    assert feedback.reflections[0]["changes"][0]["status"] == "unverified"
    assert "<reflection" in message_text(view[-1]) and "repo_search" in message_text(view[-1])
    assert_pairs(view)
    assert context.build(messages) == view
    feedback_turn(
        feedback,
        model,
        messages,
        "pytest -q tests/test_bug.py",
        pytest_failure(),
        1,
        note="Revised hypothesis: inspect the assertion",
    )
    context.build(messages)
    assert len(feedback.reflections) == 1 and feedback.pending is None
    assert feedback.reflections[0]["response"]["status"] == "unverified"
    assert "<reflection" not in message_text(context.build(messages)[-1])
    feedback_turn(feedback, model, messages, "pytest tests/other.py", "1 passed in 0.01s")
    context.ingest(messages)
    assert next(iter(context.task_state.failures.values()))["status"] == "active"
    feedback_turn(feedback, model, messages, "pytest tests/test_bug.py", PASS)
    context.ingest(messages)
    assert next(iter(context.task_state.failures.values()))["status"] == "resolved"
    assert next(iter(context.task_state.failures.values()))["occurrences"] == 3
    assert len(feedback.history) == 5


def test_failure_changes_partial_results_and_regression(model):
    manager = FeedbackManager(FeedbackConfig(enabled=True))
    context = ContextManager(ContextConfig(context_window=100000), model, feedback_manager=manager)
    messages = history(model)
    feedback_turn(manager, model, messages, "pytest", pytest_failure(), 1)
    context.ingest(messages)
    old = next(iter(manager.failures.values()))
    feedback_turn(manager, model, messages, "pytest", pytest_failure(node="tests/test_bug.py::test_other", value=3), 1)
    context.ingest(messages)
    assert old.status == "not_observed" and len(manager.failures) == 2
    feedback_turn(manager, model, messages, "pytest", "no tests ran", 5)
    context.ingest(messages)
    assert any(record.status == "active" for record in manager.failures.values())
    feedback_turn(manager, model, messages, "pytest", PASS)
    context.ingest(messages)
    assert all(record.status == "resolved" for record in manager.failures.values())
    feedback_turn(manager, model, messages, "pytest", pytest_failure(), 1, note="All tests now pass; the bug is fixed")
    context.ingest(messages)
    assert set(manager.reflections[-1]["reasons"]) == {
        "regression_after_passing_scope",
        "unverified_claim_contradicted",
    }
    feedback_turn(manager, model, messages, "pytest", PASS)
    context.ingest(messages)
    assert manager.pending is None


def test_environment_exception_does_not_create_or_resolve_failures(model):
    manager = FeedbackManager(FeedbackConfig(enabled=True))
    context = ContextManager(ContextConfig(context_window=100000), model, feedback_manager=manager)
    messages = history(model)
    feedback_turn(manager, model, messages, "pytest", pytest_failure(), 1)
    context.ingest(messages)
    interrupted = manager.prepare(
        {"command": "pytest"},
        {**output(pytest_failure(), -1, "execution timed out"), "extra": {"exception_type": "TimeoutExpired"}},
    )
    add_turn(model, messages, ["pytest"], [interrupted])
    view = context.build(messages)
    assert len(manager.failures) == 1 and next(iter(manager.failures.values())).occurrences == 1
    assert next(iter(manager.failures.values())).status == "active" and not manager.reflections
    assert "environment problem" in message_text(view[-1]) and "do not infer a source-code bug" in message_text(
        view[-1]
    )
    assert context.task_state.unresolved["pytest"].returncode == -1


def test_environment_error_defers_pending_reflection_until_testing_resumes(model):
    manager = FeedbackManager(FeedbackConfig(enabled=True, repeat_failure_threshold=2))
    context = ContextManager(ContextConfig(context_window=100000), model, feedback_manager=manager)
    messages = history(model)
    commands = ["pytest", "pytest", "docker inspect container"]
    add_turn(
        model,
        messages,
        commands,
        [
            manager.prepare({"command": command}, result)
            for command, result in zip(
                commands,
                [output(pytest_failure(), 1), output(pytest_failure(), 1), output("", -1, "Docker unavailable")],
            )
        ],
    )
    view = context.build(messages)
    assert manager.pending and len(manager.reflections) == 1
    assert "environment problem" in message_text(view[-1]) and "<reflection" not in message_text(view[-1])
    assert next(iter(manager.failures.values())).occurrences == 2
    assert_pairs(view)
    feedback_turn(manager, model, messages, "pytest", pytest_failure(), 1, note="Environment restored; rerun tests")
    view = context.build(messages)
    assert manager.pending and manager.reflections[0]["response"] is None
    assert len(manager.reflections) == 1 and "<reflection" in message_text(view[-1])
    feedback_turn(manager, model, messages, "pytest", PASS, note="Reassessed hypothesis and verified a correction")
    context.ingest(messages)
    assert manager.pending is None and manager.reflections[0]["response"]["status"] == "unverified"
    assert next(iter(manager.failures.values())).status == "resolved"


def shell_command(args):
    return subprocess.list2cmdline(args) if os.name == "nt" else shlex.join(args)


def test_real_timeout_and_multiple_native_results_keep_execution_evidence(model, tmp_path):
    (tmp_path / "reproduce_timeout.py").write_text(
        "import time\nprint('started', flush=True)\ntime.sleep(5)\n", encoding="utf-8"
    )
    command = shell_command([sys.executable, "-B", "reproduce_timeout.py"])
    result = LocalEnvironment(cwd=str(tmp_path), timeout=1).execute({"command": command})
    manager = FeedbackManager(FeedbackConfig(enabled=True))
    parsed = manager.prepare({"command": command}, result)
    assert parsed["extra"]["test_feedback"]["outcome"] == "interrupted"
    assert parsed["extra"]["test_feedback"]["classification"] == "system_error"
    assert parsed["extra"]["exception_type"] == "TimeoutExpired" and "started" in parsed["output"]
    messages = history(model)
    commands = ["pytest tests/a.py", "pytest tests/b.py"]
    add_turn(
        model,
        messages,
        commands,
        [
            manager.prepare({"command": c}, output(pytest_failure(node=c.split()[1] + "::test_value"), 1))
            for c in commands
        ],
    )
    context = ContextManager(ContextConfig(context_window=100000), model, feedback_manager=manager)
    view = context.build(messages)
    assert [run["source"] for run in manager.history] == [3, 4]
    assert len(context.task_state.failures) == 2
    assert messages[3:5] == view[3:5]
    assert_pairs(view)


def test_raw_hook_precedes_truncation_and_compaction_preserves_feedback(model):
    model.config.observation_template = (
        "<returncode>{{output.returncode}}</returncode>{{output.output[:100]}}...{{output.output[-100:]}}"
    )
    feedback = FeedbackManager(FeedbackConfig(enabled=True, repeat_failure_threshold=2))
    context = ContextManager(
        ContextConfig(context_window=18000, reserved_output_tokens=1000, keep_recent_turns=1),
        model,
        feedback_manager=feedback,
    )
    messages = history(model)
    long_log = "x" * 5000 + "\n" + pytest_failure() + "\n" + "y" * 5000
    for index in range(2):
        feedback_turn(
            feedback,
            model,
            messages,
            "pytest",
            long_log,
            1,
            note="Prior reasoning " * 1000 if index == 0 else "Reconsider the parser",
        )
        context.ingest(messages)
    add_turn(model, messages, ["echo inspected"], [output("done")])
    original = deepcopy(messages)
    view = context.build(messages)
    assert "assert 1 == 2" not in message_text(messages[3])
    assert "assert 1 == 2" in messages[3]["extra"]["raw_output"]
    assert "assert 1 == 2" in message_text(view[-1]) and messages == original
    assert context.task_state.failures and context.task_state.reflections
    assert context.checkpoints and context.requests[-1]["memory_message"]
    assert_pairs(view)
    assert context.requests[-1]["feedback_message"] == view[-1]
    assert context.requests[-1]["estimated_input_tokens"] + 1000 <= 18000


def test_feedback_guidance_is_included_in_window_capacity_checks(model):
    feedback = FeedbackManager(FeedbackConfig(enabled=True))
    context = ContextManager(
        ContextConfig(context_window=300, reserved_output_tokens=100, request_overhead_tokens=0),
        model,
        feedback_manager=feedback,
    )
    messages = history(model)
    feedback_turn(feedback, model, messages, "pytest", pytest_failure(), 1)
    with pytest.raises(ContextCapacityError):
        context.build(messages)
    assert context.requests[-1]["feedback_message"] and context.task_state.failures


@pytest.fixture
def failing_project(tmp_path):
    (tmp_path / "bug.py").write_text("value = 1\n", encoding="utf-8")
    (tmp_path / "test_bug.py").write_text(
        "import unittest\nfrom bug import value\nclass Case(unittest.TestCase):\n    def test_value(self):\n        self.assertEqual(value, 2)\n",
        encoding="utf-8",
    )
    return tmp_path


def test_real_unittest_loop_observability_state_reset_and_original_agent(failing_project):
    test_command = shell_command([sys.executable, "-B", "-m", "unittest", "test_bug", "-v"])
    edit = shell_command([sys.executable, "-c", "from pathlib import Path; Path('bug.py').write_text('value = 2\\n')"])
    submit = shell_command([sys.executable, "-c", "print('COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT')"])
    store = Store(failing_project / "traces.db")
    store.save_task(Task("feedback", "Repair with test feedback"))
    collector = Collector(store, ObserveConfig("feedback"))
    replies = [
        make_output("Inspect", [{"command": command}], cost=0.25)
        for command in (test_command, edit, test_command, submit, submit)
    ]
    agent = observed_agent_class(EnhancedAgent)(
        DeterministicModel(outputs=replies, cost_per_call=0.25),
        LocalEnvironment(cwd=str(failing_project)),
        collector=collector,
        system_template="S",
        instance_template="{{task}}",
        feedback={"enabled": True},
        output_path=failing_project / "run.json",
    )
    assert agent.run("Fix value")["exit_status"] == "Submitted"
    data = agent.serialize()
    assert [run["outcome"] for run in data["feedback_guided_repair"]["history"]] == ["failed", "passed"]
    assert next(iter(data["feedback_guided_repair"]["failures"].values()))["status"] == "resolved"
    assert len(data["messages"]) == 10 and data["messages"][3]["extra"]["returncode"] == 1
    assert data["context_engineering"]["requests"][1]["feedback_message"]
    run = store.run(collector.run.run_id)
    assert run_metrics(run)["llm_calls"] == 4 and run_metrics(run)["tool_calls"] == 4
    assert run_metrics(run)["cost_usd"] == 1.0 and run.messages == agent.messages
    assert (
        json.loads((failing_project / "run.json").read_text(encoding="utf-8"))["feedback_guided_repair"]
        == data["feedback_guided_repair"]
    )
    assert agent.run("Another issue")["exit_status"] == "Submitted"
    assert not agent.feedback_manager.history and not agent.context_manager.task_state.failures
    original = DefaultAgent(
        DeterministicModel(outputs=[make_output("done", [{"command": submit}])]),
        LocalEnvironment(cwd=str(failing_project)),
        system_template="S",
        instance_template="{{task}}",
    )
    assert original.run("Original agent")["exit_status"] == "Submitted"
    assert "feedback_guided_repair" not in original.serialize()


def test_real_swebench_container_repair_feedback():
    executable = shutil.which("docker")
    if not executable or subprocess.run([executable, "version"], capture_output=True, timeout=10).returncode:
        pytest.skip("Docker is not accessible")
    image = os.environ.get("MSWEA_FEEDBACK_TEST_IMAGE", "python:3.10-slim")
    if subprocess.run([executable, "image", "inspect", image], capture_output=True).returncode:
        pytest.skip("Set MSWEA_FEEDBACK_TEST_IMAGE to a cached SWE-bench image")
    env = DockerEnvironment(image=image, cwd="/testbed", executable=executable)
    source = (
        "import unittest\nclass Case(unittest.TestCase):\n    def test_value(self):\n        self.assertEqual(1, 2)\n"
    )
    assert (
        env.execute(
            {
                "command": shlex.join(
                    ["python", "-c", f"from pathlib import Path; Path('feedback_probe.py').write_text({source!r})"]
                )
            }
        )["returncode"]
        == 0
    )
    command = "python -B -m unittest feedback_probe -v"
    edit = shlex.join(
        [
            "python",
            "-c",
            "from pathlib import Path; p=Path('feedback_probe.py'); p.write_text(p.read_text().replace('assertEqual(1, 2)', 'assertEqual(2, 2)'))",
        ]
    )
    agent = EnhancedAgent(
        DeterministicModel(
            outputs=[
                make_output("Verify", [{"command": cmd}])
                for cmd in (command, edit, command, "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT")
            ]
        ),
        env,
        system_template="S",
        instance_template="{{task}}",
        feedback={"enabled": True},
        retrieval={"enabled": True},
    )
    assert agent.run("Repair feedback probe")["exit_status"] == "Submitted"
    assert agent.repo_search.command and "/testbed" not in agent.repo_search.command
    assert [run["outcome"] for run in agent.feedback_manager.history] == ["failed", "passed"]
    assert next(iter(agent.context_manager.task_state.failures.values()))["status"] == "resolved"
    assert agent.n_calls == 4 and len(agent.messages) == 10
    env.cleanup()

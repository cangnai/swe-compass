import json
import os
import shutil
import subprocess
import sys
from copy import deepcopy
from pathlib import Path

import pytest

from minisweagent.agents.enhanced import EnhancedAgent
from minisweagent.environments.docker import DockerEnvironment
from minisweagent.environments.local import LocalEnvironment
from minisweagent.models.test_models import DeterministicModel, make_output
from minisweagent.observability.instrumentation import Collector, ObserveConfig, observed_agent_class
from minisweagent.observability.metrics import run_metrics
from minisweagent.observability.schema import Task
from minisweagent.observability.storage import Store
from minisweagent.optimization import repo_retriever
from minisweagent.optimization.context_manager import ContextConfig, ContextManager
from minisweagent.optimization.repo_retriever import METADATA_PREFIX, RepoRetriever, format_results
from minisweagent.optimization.repo_search import RepoSearch, RetrievalConfig
from minisweagent.optimization.task_state import TaskState, command_kind, message_text
from tests.agents.test_enhanced import add_turn, assert_pairs, history, model, output  # noqa: F401


@pytest.fixture
def repository(tmp_path):
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src/parser.py").write_text(
        "class Parser:\n"
        "    @staticmethod\n"
        "    async def decode_payload(value):\n"
        "        def normalize(item):\n"
        "            return item.strip()\n"
        "        return normalize(value)\n",
        encoding="utf-8",
    )
    (tmp_path / "tests/test_parser.py").write_text(
        "from src.parser import Parser\n\ndef test_decode():\n    Parser.decode_payload('payload')\n", encoding="utf-8"
    )
    (tmp_path / "noise.txt").write_text("payload " * 1000, encoding="utf-8")
    (tmp_path / ".gitignore").write_text("ignored/\n*.generated.py\n", encoding="utf-8")
    for directory in ("ignored", "node_modules", ".venv", "build"):
        (tmp_path / directory).mkdir()
        (tmp_path / directory / "parser.py").write_text("def decode_payload(): pass", encoding="utf-8")
    return tmp_path


def test_hybrid_traceback_symbols_and_bounded_deduplicated_snippets(repository):
    retriever = RepoRetriever(repository)
    results = retriever.search(
        'Parser.decode_payload failed\nFile "/testbed/src/parser.py", line 5, in normalize', snippet_lines=4
    )
    assert results[0].path == "src/parser.py"
    assert results[0].symbol.name == "Parser.decode_payload.normalize"
    assert results[0].symbol.start == 4 and results[0].symbol.end == 5
    assert "traceback line" in results[0].reasons
    assert any("exact symbol:" in reason for result in results for reason in result.reasons)
    assert all(len(result.snippet.splitlines()) <= 4 for result in results)
    assert not any(
        any(part in result.path for part in ("ignored", "node_modules", ".venv", "build")) for result in results
    )
    for i, result in enumerate(results):
        assert all(
            result.path != other.path or result.end + 3 < other.start or other.end + 3 < result.start
            for other in results[i + 1 :]
        )
    assert "5:             return item.strip()" in format_results("decode_payload", retriever, results)
    assert RepoRetriever(repository).search("Parser.decode_payload")[0].symbol.name == "Parser.decode_payload"
    assert (
        RepoRetriever(repository).search("src/parser.py:5", snippet_lines=1)[0].snippet
        == "5:             return item.strip()"
    )
    for i in range(45):
        (repository / f"a{i}.py").write_text("Parser.decode_payload('payload')\n", encoding="utf-8")
    assert RepoRetriever(repository, candidate_limit=1).search("Parser.decode_payload")[0].path == "src/parser.py"


def test_test_path_links_to_source_definition_and_dotted_module(repository):
    results = RepoRetriever(repository).search("tests/test_parser.py fails", top_k=10)
    assert any(result.path == "src/parser.py" and result.symbol.name == "Parser.decode_payload" for result in results)
    assert any("test-referenced definition: Parser.decode_payload" in result.reasons for result in results)
    assert RepoRetriever(repository).search("src.parser decode_payload")[0].path == "src/parser.py"
    (repository / "src/unique_decoder.py").write_text("def convert(value): return value\n", encoding="utf-8")
    assert RepoRetriever(repository).search("unique_decoder crashes")[0].path == "src/unique_decoder.py"


def test_every_query_refreshes_edits_creations_deletions_and_syntax_fallback(repository):
    retriever = RepoRetriever(repository)
    before = retriever.search("Parser.decode_payload")[0]
    (repository / "src/parser.py").write_text("\n\n\n" + (repository / "src/parser.py").read_text(), encoding="utf-8")
    after = retriever.search("Parser.decode_payload")[0]
    assert after.symbol.start == before.symbol.start + 3 and after.revision != before.revision
    (repository / "fresh.py").write_text("def unique_refresh():\n    return 7\n", encoding="utf-8")
    assert retriever.search("unique_refresh")[0].symbol.name == "unique_refresh"
    (repository / "fresh.py").write_text("def unique_refresh():\n    return " + "1+" * 1300 + "1\n", encoding="utf-8")
    assert retriever.search("unique_refresh")[0].path == "fresh.py"
    (repository / "fresh.py").write_text("def unique_refresh(\n", encoding="utf-8")
    assert retriever.search("unique_refresh")[0].symbol is None
    assert "text fallback (Python AST unavailable)" in retriever.search("unique_refresh")[0].reasons
    (repository / "fresh.py").unlink()
    assert retriever.search("unique_refresh") == []


def test_non_python_binary_large_files_and_literal_shell_characters(repository):
    (repository / "source.js").write_text("function uniqueWidget() { return 1; }\n", encoding="utf-8")
    (repository / "binary.bin").write_bytes(b"uniqueWidget\x00secret")
    (repository / "large.txt").write_text("uniqueWidget" * 100, encoding="utf-8")
    results = RepoRetriever(repository, max_file_bytes=100).search("uniqueWidget; $(touch HACKED) `echo unsafe`")
    assert [result.path for result in results] == ["source.js"]
    assert results[0].symbol is None and "text fallback (non-Python)" in results[0].reasons
    assert not (repository / "HACKED").exists()
    assert RepoRetriever(repository).search("zzzzzz_no_matches") == []


@pytest.mark.parametrize("path_mode", ["git", "filesystem"])
def test_standalone_fallback_without_ripgrep(repository, tmp_path, path_mode):
    executable_dir = tmp_path / "tools"
    executable_dir.mkdir()
    if path_mode == "git":
        executable_dir = Path(shutil.which("git")).parent
    result = subprocess.run(
        [sys.executable, "-B", repo_retriever.__file__, "decode_payload", "--root", str(repository)],
        capture_output=True,
        text=True,
        env={**os.environ, "PATH": str(executable_dir)},
        check=True,
    )
    metadata = json.loads(result.stdout.split(METADATA_PREFIX)[-1])
    assert metadata["results"][0]["symbol"]["name"] == "Parser.decode_payload"
    assert not any(result["path"].startswith("ignored/") for result in metadata["results"])
    assert not (repository / "__pycache__").exists()


def test_local_deployment_quotes_queries_keeps_patch_clean_and_records_memory(repository):
    env = LocalEnvironment(cwd=str(repository))
    helper = RepoSearch(RetrievalConfig(enabled=True, python=sys.executable))
    before = env.execute({"command": "git status --porcelain"})
    helper.install(env)
    assert helper.command and helper.setup["returncode"] == 0
    assert env.execute({"command": "git status --porcelain"}) == before
    command = helper.command + ' "Parser.decode_payload"'
    result = env.execute({"command": command})
    assert result["returncode"] == 0 and "Python AST" in result["output"]
    state = TaskState()
    state.observe({"command": command}, {"extra": {"raw_output": result["output"], "returncode": 0}}, 3)
    assert state.files["src/parser.py"].status == "candidate"
    assert state.symbols["src/parser.py:Parser.decode_payload"].source == 3
    assert state.localizations["src/parser.py"]["results"][0]["revision"]
    assert not state.changes and not state.unresolved
    assert command_kind(command) == "read"
    assert str(repository) not in helper.command
    state.observe({"command": command}, {"extra": {"raw_output": METADATA_PREFIX + "{broken", "returncode": 0}}, 4)
    assert state.files["src/parser.py"].source == 3
    state.observe(
        {"command": command},
        {
            "extra": {
                "raw_output": format_results("Parser.decode_payload", RepoRetriever(repository), []),
                "returncode": 0,
            }
        },
        5,
    )
    assert not state.localizations and not state.files and not state.symbols


def test_retrieval_context_elision_and_native_protocol_pairs(repository, model):
    messages = history(model)
    retriever = RepoRetriever(repository)
    command = "python /tmp/tool/repo_search.py decode_payload"
    text = format_results("decode_payload", retriever, retriever.search("decode_payload"))
    add_turn(model, messages, [command], [output(text)])
    (repository / "src/parser.py").write_text("def decode_payload(): return 1\n", encoding="utf-8")
    add_turn(
        model,
        messages,
        [command],
        [output(format_results("decode_payload", retriever, retriever.search("decode_payload")))],
    )
    add_turn(model, messages, ["echo next"], [output("done")])
    original = deepcopy(messages)
    manager = ContextManager(ContextConfig(context_window=100000, keep_recent_turns=1, min_elision_chars=10), model)
    view = manager.build(messages)
    assert 3 in manager.elisions and messages == original
    assert "superseded" in message_text(view[3])
    assert manager.task_state.localizations["src/parser.py"]["source"] == 5
    assert "src/parser.py:Parser.decode_payload" not in manager.task_state.symbols
    assert "src/parser.py:decode_payload" in manager.task_state.symbols
    assert_pairs(view)


class SearchModel(DeterministicModel):
    def query(self, messages, **kwargs):
        if self.current_index == -1:
            command = next(
                line.split(" 'issue keywords", 1)[0].split(' "issue keywords', 1)[0]
                for line in message_text(messages[0]).splitlines()
                if "--max-file-bytes" in line
            )
            self.config.outputs[0] = make_output(
                "Locate code", [{"command": command + ' "Parser.decode_payload"'}], cost=0.25
            )
        return super().query(messages, **kwargs)


def test_observed_agent_run_reuses_bash_loop_preserves_setup_actions_and_stats(repository, tmp_path):
    store = Store(tmp_path / "trace.db")
    store.save_task(Task("retrieval", "Repository retrieval"))
    collector = Collector(store, ObserveConfig("retrieval"))
    agent = observed_agent_class(EnhancedAgent)(
        SearchModel(
            outputs=[
                make_output("", []),
                make_output("done", [{"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"}], cost=0.25),
            ]
        ),
        LocalEnvironment(cwd=str(repository)),
        collector=collector,
        system_template="S",
        instance_template="{{task}}",
        retrieval={"enabled": True, "python": sys.executable},
    )
    assert agent.run("Fix decode_payload")["exit_status"] == "Submitted"
    data = agent.serialize()
    assert data["repository_retrieval"]["setup"]["returncode"] == 0
    assert data["context_engineering"]["task_state"]["localizations"]["src/parser.py"]["source"] == 3
    assert len(agent.messages) == 6 and "decode_payload" in agent.messages[3]["extra"]["raw_output"]
    run = store.run(collector.run.run_id)
    assert run.messages == agent.messages
    assert run_metrics(run)["llm_calls"] == 2 and run_metrics(run)["tool_calls"] == 3
    assert run_metrics(run)["cost_usd"] == 0.5


def test_failed_setup_falls_back_without_breaking_agent_loop(repository):
    agent = EnhancedAgent(
        DeterministicModel(outputs=[make_output("done", [{"command": "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"}])]),
        LocalEnvironment(cwd=str(repository)),
        system_template="S",
        instance_template="{{task}}",
        retrieval={"enabled": True, "python": "missing-retrieval-python-executable"},
    )
    assert agent.run("Task")["exit_status"] == "Submitted"
    assert "setup failed" in message_text(agent.messages[0])
    assert agent.serialize()["repository_retrieval"]["setup"]["returncode"] != 0


def test_real_swebench_docker_deployment_and_refresh():
    executable = shutil.which("docker")
    if not executable or subprocess.run([executable, "version"], capture_output=True, timeout=10).returncode:
        pytest.skip("Docker is not accessible")
    image = os.environ.get("MSWEA_RETRIEVAL_TEST_IMAGE", "python:3.10-slim")
    if subprocess.run([executable, "image", "inspect", image], capture_output=True).returncode:
        pytest.skip("Set MSWEA_RETRIEVAL_TEST_IMAGE to a cached SWE-bench image")
    env = DockerEnvironment(image=image, cwd="/testbed", executable=executable)
    before = env.execute({"command": "git status --porcelain"})
    helper = RepoSearch(RetrievalConfig(enabled=True))
    helper.install(env)
    assert helper.command and "/testbed" not in helper.command
    assert env.execute({"command": "git status --porcelain"}) == before
    assert (
        env.execute({"command": "printf 'def unique_container_symbol(): return 7\n' > retrieval_probe.py"})[
            "returncode"
        ]
        == 0
    )
    first = env.execute({"command": helper.command + " unique_container_symbol"})
    assert first["returncode"] == 0 and "1: def unique_container_symbol(): return 7" in first["output"]
    assert (
        env.execute({"command": "printf '\n\ndef unique_container_symbol(): return 9\n' > retrieval_probe.py"})[
            "returncode"
        ]
        == 0
    )
    assert (
        "3: def unique_container_symbol(): return 9"
        in env.execute({"command": helper.command + " unique_container_symbol"})["output"]
    )
    assert "mini-repo-search" not in env.execute({"command": "git status --porcelain"})["output"]
    env.cleanup()

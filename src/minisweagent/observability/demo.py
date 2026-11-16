"""Offline integration demo: scripted responses, real agent actions and real pytest verdicts."""

import json
import sys
from pathlib import Path

from minisweagent.config import get_config_from_spec
from minisweagent.observability.experiments import compare, export_rows, run_task
from minisweagent.observability.schema import Experiment, Task, new_id
from minisweagent.observability.storage import Store
from minisweagent.observability.trajectory_parser import import_trajectory
from minisweagent.observability.workspace import git


def python_command(code: str) -> str:
    return f'"{sys.executable}" -c "{code}"'


def demo(store: Store, historical: Path | None = None) -> list[dict]:
    if historical and historical.is_dir():
        for path in sorted(historical.glob("*.traj.json")):
            import_trajectory(store, path)
    source = store.path.parent / "demo-sources" / new_id()
    source.mkdir(parents=True)
    (source / ".gitignore").write_text("__pycache__/\n.pytest_cache/\n", encoding="utf-8")
    (source / "pytest.ini").write_text("[pytest]\n", encoding="utf-8")
    (source / "calculator.py").write_text("def divide(a, b):\n    return a // b\n", encoding="utf-8")
    (source / "test_calculator.py").write_text(
        "import pytest\nfrom calculator import divide\n\n"
        "def test_fraction():\n    assert divide(5, 2) == 2.5\n\n"
        "def test_negative():\n    assert divide(-3, 2) == -1.5\n\n"
        "def test_zero():\n    with pytest.raises(ZeroDivisionError):\n        divide(1, 0)\n",
        encoding="utf-8",
    )
    git(source, "init", "--quiet")
    git(source, "add", ".")
    git(
        source,
        "-c",
        "user.name=mini-observe",
        "-c",
        "user.email=demo@localhost",
        "commit",
        "--quiet",
        "-m",
        "fix(env): reproduce fractional division bug",
    )
    task = Task(
        "division-" + new_id()[:8],
        "修复除法精度 Bug",
        "修复 calculator.divide：保留小数，验证负数与除零。",
        str(source.resolve()),
        git(source, "rev-parse", "HEAD"),
        f'"{sys.executable}" -m pytest -q',
    )
    store.save_task(task)
    test = task.test_command
    fix = python_command(
        "from pathlib import Path; p=Path('calculator.py'); p.write_text(p.read_text().replace('a // b', 'a / b'))"
    )
    commands = (
        [test, test, test, "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"],
        [test, fix, test, "echo COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT"],
    )
    runs = []
    for sequence in commands:
        config = get_config_from_spec("mini.yaml")
        config["agent"] = {k: v for k, v in config["agent"].items() if k not in {"mode", "confirm_exit", "agent_class"}}
        config["agent"].update(agent_class="default", cost_limit=0, step_limit=10)
        config["model"] = {
            "model_class": "minisweagent.models.test_models.DeterministicModel",
            "model_name": "offline-scripted-demo",
            "cost_per_call": 0,
            "outputs": [
                {
                    "role": "assistant",
                    "content": "离线脚本响应；命令由原 Agent 实际执行。",
                    "extra": {"actions": [{"command": command}]},
                }
                for command in sequence
            ],
        }
        runs.append(run_task(store, task.task_id, config))
    store.save_experiment(Experiment("自定义除法 Bug：两次独立执行", [r.run_id for r in runs]))
    rows = compare(store, [r.run_id for r in runs])
    for format in ("json", "csv"):
        (store.path.parent / f"demo-comparison.{format}").write_text(
            export_rows(rows, format), encoding="utf-8-sig" if format == "csv" else "utf-8"
        )
    (store.path.parent / "demo-manifest.json").write_text(
        json.dumps(
            {
                "task_id": task.task_id,
                "run_ids": [r.run_id for r in runs],
                "database": str(store.path),
                "model": "offline-scripted-demo: no API calls; timings and tests are real",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return rows

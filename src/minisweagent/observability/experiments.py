import csv
import hashlib
import io
import json
import os
import subprocess
import time
from dataclasses import asdict
from pathlib import Path

from minisweagent.agents import get_agent_class
from minisweagent.environments import get_environment
from minisweagent.environments.local import LocalEnvironment, _run
from minisweagent.models import get_model
from minisweagent.observability.instrumentation import Collector, ObserveConfig, observed_agent_class
from minisweagent.observability.metrics import run_metrics
from minisweagent.observability.schema import Evaluation, Run, new_id
from minisweagent.observability.storage import Store
from minisweagent.observability.workspace import git, isolated_checkout, snapshot


def run_task(store: Store, task_id: str, config: dict, *, isolate: bool = True, evaluate_after: bool = True) -> Run:
    task = store.task(task_id)
    repo = Path(task.repo_path).resolve()
    if not repo.is_dir():
        raise NotADirectoryError(repo)
    if isolate and not task.base_revision:
        raise ValueError("An isolated run needs a task base_revision (commit SHA)")
    workspace = (
        isolated_checkout(repo, task.base_revision, store.path.parent / "workspaces" / new_id()) if isolate else repo
    )
    collector = Collector(store, ObserveConfig(task_id, str(workspace)))
    collector.run.metadata.update(
        base_revision=git(workspace, "rev-parse", "HEAD"),
        isolated=isolate,
        requested_base_revision=task.base_revision,
        task_snapshot=asdict(task),
    )
    agent_config = dict(config.get("agent", {}))
    agent_type = get_agent_class(agent_config.pop("agent_class", "default"))
    environment = get_environment(config.get("environment", {}) | {"cwd": str(workspace)}, default_type="local")
    if not isinstance(environment, LocalEnvironment):
        raise ValueError(
            "This task runner evaluates local Git workspaces; use LocalEnvironment or an external evaluator"
        )
    agent = observed_agent_class(agent_type)(
        get_model(config=config.get("model", {})),
        environment,
        collector=collector,
        **agent_config,
    )
    agent.run(task.description or task.title)
    if evaluate_after:
        evaluate(store, collector.run.run_id)
    return store.run(collector.run.run_id)


def evaluate(store: Store, run_id: str, timeout: int = 120) -> Evaluation:
    run = store.run(run_id)
    task = store.task(run.task_id)
    evaluation = Evaluation(run_id)
    command = run.metadata.get("task_snapshot", {}).get("test_command", task.test_command)
    if not command:
        store.save_evaluation(evaluation)
        return evaluation
    evidence = store.artifacts / f"{run_id}.evaluation-{new_id()}.json"
    evaluation.evidence_path = str(evidence)
    evaluation.details = {"command": command, "cwd": run.workspace, "timeout": timeout}
    current_state = snapshot(run.workspace)[1]
    if not run.workspace or not Path(run.workspace).is_dir():
        evaluation.verdict = "evaluation_error"
        evaluation.test_summary = "No preserved run workspace; import an external evaluation instead"
    elif current_state != run.metadata.get("final_state") or current_state is None:
        evaluation.verdict = "evaluation_error"
        evaluation.test_summary = (
            "Workspace state is unavailable or changed since the run; refusing to evaluate another revision"
        )
    else:
        timer = time.perf_counter()
        try:
            result = _run(command, run.workspace, dict(os.environ), timeout)
            evaluation.returncode = result.returncode
            evaluation.verdict = "passed" if result.returncode == 0 else "failed"
            evaluation.test_summary = result.stdout[-4000:]
            evaluation.details["output"] = result.stdout
        except (subprocess.TimeoutExpired, OSError) as error:
            evaluation.verdict = "evaluation_error"
            evaluation.test_summary = f"{type(error).__name__}: {error}"
            output = getattr(error, "output", "") or ""
            evaluation.details["output"] = output.decode(errors="replace") if isinstance(output, bytes) else output
        evaluation.details["duration_ms"] = (time.perf_counter() - timer) * 1000
        evaluation.details["state_before"] = current_state
        evaluation.details["state_after"] = snapshot(run.workspace)[1]
    evidence.write_text(json.dumps(asdict(evaluation), ensure_ascii=False, indent=2), encoding="utf-8")
    store.save_evaluation(evaluation)
    return evaluation


def register_evaluation(store: Store, evaluation: Evaluation) -> None:
    store.run(evaluation.run_id)
    if evaluation.verdict not in {"passed", "failed", "not_evaluated", "evaluation_error"}:
        raise ValueError("Unknown evaluation verdict")
    if evaluation.verdict in {"passed", "failed"} and not evaluation.evidence_path:
        raise ValueError("External evaluation needs evidence_path")
    if evaluation.evidence_path:
        source = Path(evaluation.evidence_path)
        target = store.artifacts / f"{evaluation.run_id}.external-{new_id()}{source.suffix}"
        target.write_bytes(source.read_bytes())
        evaluation.evidence_path = str(target)
    store.save_evaluation(evaluation)


def compare(store: Store, run_ids: list[str]) -> list[dict]:
    result = []
    for rid in run_ids:
        run = store.run(rid)
        evaluation = store.evaluation(rid)
        config_json = json.dumps(run.config_snapshot, sort_keys=True, ensure_ascii=False)
        prompts = {k: v for k, v in run.config_snapshot.get("agent", {}).items() if k.endswith("_template")}
        result.append(
            run_metrics(run)
            | {
                "verdict": evaluation.verdict,
                "evaluator": evaluation.evaluator,
                "test_summary": evaluation.test_summary,
                "evidence_path": evaluation.evidence_path,
                "config_hash": hashlib.sha256(config_json.encode()).hexdigest()[:12],
                "prompt_hash": hashlib.sha256(
                    json.dumps(prompts, sort_keys=True, ensure_ascii=False).encode()
                ).hexdigest()[:12],
                "config_snapshot": run.config_snapshot,
                "base_revision": run.metadata.get("base_revision"),
                "initial_state": run.metadata.get("initial_state"),
                "source": run.source,
                "trajectory_path": run.trajectory_path,
            }
        )
    return result


def export_rows(rows: list[dict], format: str = "json") -> str:
    if format == "json":
        return json.dumps(rows, ensure_ascii=False, indent=2)
    if format != "csv":
        raise ValueError("Expected json or csv")
    output = io.StringIO(newline="")
    if rows:
        writer = csv.DictWriter(output, fieldnames=list(dict.fromkeys(k for row in rows for k in row)))
        writer.writeheader()
        for row in rows:
            writer.writerow(
                {k: json.dumps(v, ensure_ascii=False) if isinstance(v, dict | list) else v for k, v in row.items()}
            )
    return output.getvalue()

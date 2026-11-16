"""Opt-in local tracing and evaluation CLI. Existing mini entry points are unchanged."""

import json
import subprocess
import sys
from dataclasses import asdict
from pathlib import Path

import typer

from minisweagent.config import get_config_from_spec
from minisweagent.observability.experiments import compare, evaluate, export_rows, register_evaluation, run_task
from minisweagent.observability.schema import Evaluation, Task
from minisweagent.observability.storage import Store
from minisweagent.observability.trajectory_parser import import_trajectory
from minisweagent.utils.serialize import recursive_merge

app = typer.Typer(help="mini-SWE-agent traces / diagnostics / evaluations")
DEFAULT_DB = Path(".mini-observe/traces.db")


@app.command("import")
def import_traces(path: Path, db: Path = DEFAULT_DB, task_id: str = "") -> None:
    store = Store(db)
    for source in sorted(path.rglob("*.traj.json")) if path.is_dir() else [path]:
        typer.echo(import_trajectory(store, source, task_id).run_id)


@app.command("task")
def register_task(file: Path, db: Path = DEFAULT_DB) -> None:
    task = Task(**json.loads(file.read_text(encoding="utf-8-sig")))
    Store(db).save_task(task)
    typer.echo(task.task_id)


@app.command("run")
def execute(
    task_id: str,
    config: list[str] = typer.Option(["mini.yaml"], "-c", "--config"),
    db: Path = DEFAULT_DB,
    isolate: bool = True,
    evaluate_after: bool = True,
) -> None:
    run = run_task(
        Store(db),
        task_id,
        recursive_merge(*[get_config_from_spec(s) for s in config]),
        isolate=isolate,
        evaluate_after=evaluate_after,
    )
    typer.echo(run.run_id)


@app.command("evaluate")
def evaluate_run(run_id: str, db: Path = DEFAULT_DB, timeout: int = 120) -> None:
    typer.echo(json.dumps(asdict(evaluate(Store(db), run_id, timeout)), ensure_ascii=False, indent=2))


@app.command("evaluation-import")
def import_evaluation(file: Path, db: Path = DEFAULT_DB) -> None:
    register_evaluation(Store(db), Evaluation(**json.loads(file.read_text(encoding="utf-8-sig"))))


@app.command("compare")
def comparison(run_ids: list[str] = typer.Argument(None), db: Path = DEFAULT_DB, output: Path | None = None) -> None:
    store = Store(db)
    rows = compare(store, run_ids or [r.run_id for r in store.runs()])
    text = export_rows(rows, "csv" if output and output.suffix == ".csv" else "json")
    if output:
        output.write_text(text, encoding="utf-8-sig" if output.suffix == ".csv" else "utf-8")
    else:
        typer.echo(text)


@app.command("demo")
def demonstration(db: Path = Path(".mini-observe/demo/traces.db"), historical: Path = Path("tests/test_data")) -> None:
    from minisweagent.observability.demo import demo

    rows = demo(Store(db), historical)
    typer.echo(
        export_rows(
            [
                {
                    k: row[k]
                    for k in (
                        "run_id",
                        "task_id",
                        "exit_status",
                        "verdict",
                        "steps",
                        "duration_ms",
                        "llm_ms",
                        "tool_ms",
                        "total_tokens",
                        "cost_usd",
                        "diagnostics",
                    )
                }
                for row in rows
            ]
        )
    )


@app.command("serve")
def serve(db: Path = DEFAULT_DB, port: int = 8501) -> None:
    subprocess.run(
        [
            sys.executable,
            "-m",
            "streamlit",
            "run",
            str(Path(__file__).resolve().parents[1] / "observability" / "dashboard.py"),
            "--server.address=127.0.0.1",
            f"--server.port={port}",
            "--server.headless=true",
            "--browser.gatherUsageStats=false",
            "--",
            "--db",
            str(db.resolve()),
        ],
        check=True,
    )


if __name__ == "__main__":
    app()

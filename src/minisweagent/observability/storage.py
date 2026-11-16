import hashlib
import json
import sqlite3
from contextlib import contextmanager
from dataclasses import asdict
from pathlib import Path

from minisweagent.observability.schema import Evaluation, Experiment, Run, Task


class Store:
    def __init__(self, path: Path | str = ".mini-observe/traces.db"):
        self.path = Path(path).resolve()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.artifacts = self.path.parent / "artifacts"
        self.artifacts.mkdir(exist_ok=True)
        with self.connect() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS tasks (task_id TEXT PRIMARY KEY, body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS runs (
                    run_id TEXT PRIMARY KEY, task_id TEXT NOT NULL REFERENCES tasks(task_id), body TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS runs_task ON runs(task_id);
                CREATE TABLE IF NOT EXISTS spans (
                    span_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id), body TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS spans_run ON spans(run_id);
                CREATE TABLE IF NOT EXISTS events (
                    event_id TEXT PRIMARY KEY, run_id TEXT NOT NULL REFERENCES runs(run_id), body TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS events_run ON events(run_id);
                CREATE TABLE IF NOT EXISTS evaluations (
                    run_id TEXT PRIMARY KEY REFERENCES runs(run_id), body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS experiments (experiment_id TEXT PRIMARY KEY, body TEXT NOT NULL);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.execute("PRAGMA foreign_keys=ON")
        db.execute("BEGIN")
        try:
            with db:
                yield db
        finally:
            db.close()

    def save_task(self, task: Task) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO tasks VALUES (?, ?) ON CONFLICT(task_id) DO UPDATE SET body=excluded.body",
                (task.task_id, json.dumps(asdict(task), ensure_ascii=False)),
            )

    def tasks(self) -> list[Task]:
        with self.connect() as db:
            return [Task(**json.loads(row[0])) for row in db.execute("SELECT body FROM tasks ORDER BY task_id")]

    def task(self, task_id: str) -> Task:
        return next(t for t in self.tasks() if t.task_id == task_id)

    def save_run(self, run: Run) -> None:
        body = run.to_dict()
        body.pop("spans")
        body.pop("events")
        with self.connect() as db:
            db.execute(
                "INSERT INTO runs VALUES (?, ?, ?) ON CONFLICT(run_id) DO UPDATE SET task_id=excluded.task_id, body=excluded.body",
                (run.run_id, run.task_id, json.dumps(body, ensure_ascii=False)),
            )
            for table, items, key in (("spans", run.spans, "span_id"), ("events", run.events, "event_id")):
                db.execute(f"DELETE FROM {table} WHERE run_id=?", (run.run_id,))
                db.executemany(
                    f"INSERT INTO {table} VALUES (?, ?, ?)",
                    [(getattr(item, key), run.run_id, json.dumps(asdict(item), ensure_ascii=False)) for item in items],
                )

    def runs(self, task_id: str = "", model: str = "", status: str = "", search: str = "") -> list[Run]:
        from minisweagent.observability.diagnostics import diagnose

        with self.connect() as db:
            rows = list(db.execute("SELECT run_id, body FROM runs ORDER BY rowid DESC"))
            span_rows = list(db.execute("SELECT run_id, body FROM spans ORDER BY rowid"))
            event_rows = list(db.execute("SELECT run_id, body FROM events ORDER BY rowid"))
        spans: dict[str, list] = {}
        events: dict[str, list] = {}
        for target, values in ((spans, span_rows), (events, event_rows)):
            for run_id, body in values:
                target.setdefault(run_id, []).append(json.loads(body))
        runs = [
            Run.from_dict(json.loads(body) | {"spans": spans.get(rid, []), "events": events.get(rid, [])})
            for rid, body in rows
        ]
        for run in runs:
            run.events = diagnose(run)
        return [
            r
            for r in runs
            if (not task_id or r.task_id == task_id)
            and (not model or r.model == model)
            and (not status or r.exit_status == status)
            and (not search or search.lower() in json.dumps(r.to_dict(), ensure_ascii=False).lower())
        ]

    def run(self, run_id: str) -> Run:
        return next(r for r in self.runs() if r.run_id == run_id)

    def save_evaluation(self, evaluation: Evaluation) -> None:
        with self.connect() as db:
            db.execute(
                "INSERT INTO evaluations VALUES (?, ?) ON CONFLICT(run_id) DO UPDATE SET body=excluded.body",
                (evaluation.run_id, json.dumps(asdict(evaluation), ensure_ascii=False)),
            )

    def evaluation(self, run_id: str) -> Evaluation:
        with self.connect() as db:
            row = db.execute("SELECT body FROM evaluations WHERE run_id=?", (run_id,)).fetchone()
        return Evaluation(**json.loads(row[0])) if row else Evaluation(run_id)

    def save_experiment(self, experiment: Experiment) -> None:
        if not experiment.run_ids or set(experiment.run_ids) - {r.run_id for r in self.runs()}:
            raise ValueError("Select existing runs for the experiment")
        with self.connect() as db:
            db.execute(
                "INSERT INTO experiments VALUES (?, ?)",
                (experiment.experiment_id, json.dumps(asdict(experiment), ensure_ascii=False)),
            )

    def experiments(self) -> list[Experiment]:
        with self.connect() as db:
            return [Experiment(**json.loads(row[0])) for row in db.execute("SELECT body FROM experiments")]

    def preserve(self, path: Path) -> Path:
        data = path.read_bytes()
        target = self.artifacts / f"{hashlib.sha256(data).hexdigest()}.traj.json"
        if not target.exists():
            target.write_bytes(data)
        return target

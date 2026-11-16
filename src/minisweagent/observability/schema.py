import time
from dataclasses import asdict, dataclass, field
from uuid import uuid4


def new_id() -> str:
    return uuid4().hex


@dataclass
class Task:
    task_id: str
    title: str
    description: str = ""
    repo_path: str = ""
    base_revision: str = ""
    test_command: str = ""
    source: str = "custom"
    external_id: str = ""


@dataclass
class Span:
    run_id: str
    kind: str
    step_index: int = 0
    span_id: str = field(default_factory=new_id)
    parent_span_id: str | None = None
    start_time: float | None = None
    end_time: float | None = None
    duration_ms: float | None = None
    status: str = "unknown"
    attributes: dict = field(default_factory=dict)


@dataclass
class Event:
    run_id: str
    type: str
    severity: str
    step_index: int
    rule: str
    evidence: dict
    event_id: str = field(default_factory=new_id)


@dataclass
class Evaluation:
    run_id: str
    evaluator: str = "command"
    verdict: str = "not_evaluated"
    test_summary: str = ""
    evidence_path: str = ""
    returncode: int | None = None
    evaluated_at: float = field(default_factory=time.time)
    details: dict = field(default_factory=dict)


@dataclass
class Experiment:
    name: str
    run_ids: list[str]
    experiment_id: str = field(default_factory=new_id)
    created_at: float = field(default_factory=time.time)


@dataclass
class Run:
    run_id: str
    task_id: str
    model: str = "unknown"
    config_snapshot: dict = field(default_factory=dict)
    start_time: float | None = None
    end_time: float | None = None
    duration_ms: float | None = None
    exit_status: str = "unknown"
    submission: str = ""
    trajectory_path: str = ""
    source: str = "historical"
    workspace: str = ""
    diff: str | None = None
    messages: list[dict] = field(default_factory=list)
    spans: list[Span] = field(default_factory=list)
    events: list[Event] = field(default_factory=list)
    metadata: dict = field(default_factory=dict)

    @property
    def trace_id(self) -> str:
        return self.run_id

    def to_dict(self) -> dict:
        return asdict(self) | {"trace_id": self.trace_id}

    @classmethod
    def from_dict(cls, data: dict) -> "Run":
        return cls(
            **{k: v for k, v in data.items() if k not in {"trace_id", "spans", "events"}},
            spans=[Span(**s) for s in data.get("spans", [])],
            events=[Event(**e) for e in data.get("events", [])],
        )

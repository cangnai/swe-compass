import time
from dataclasses import dataclass
from pathlib import Path

from minisweagent.environments.local import LocalEnvironment
from minisweagent.exceptions import FormatError, InterruptAgentFlow, Submitted
from minisweagent.observability.diagnostics import diagnose
from minisweagent.observability.execution_status import tool_status
from minisweagent.observability.schema import Run, Span, new_id
from minisweagent.observability.storage import Store
from minisweagent.observability.trajectory_parser import llm_attributes
from minisweagent.observability.workspace import snapshot


def safe_config(value):
    if isinstance(value, dict):
        return {
            k: "<redacted>"
            if any(word in k.lower() for word in ("api_key", "password", "secret", "authorization", "access_token"))
            else safe_config(v)
            for k, v in value.items()
        }
    if isinstance(value, list):
        return [safe_config(v) for v in value]
    return value


@dataclass
class ObserveConfig:
    task_id: str
    workspace: str = ""


class Scope:
    def __init__(self, collector: "Collector", kind: str, attributes: dict | None = None):
        self.collector = collector
        self.span = Span(
            collector.run.run_id,
            kind,
            collector.step_index,
            parent_span_id=collector.parent,
            start_time=time.time(),
            status="running",
            attributes=attributes or {},
        )
        self.previous_parent = collector.parent

    def __enter__(self) -> Span:
        self.collector.run.spans.append(self.span)
        self.collector.parent = self.span.span_id
        self.collector.store.save_run(self.collector.run)
        self.span.start_time = time.time()
        self.timer = time.perf_counter()
        return self.span

    def __exit__(self, error_type, error, tb) -> bool:
        self.collector.parent = self.previous_parent
        self.span.end_time = time.time()
        self.span.duration_ms = (time.perf_counter() - self.timer) * 1000
        if self.span.kind == "tool":
            self.span.attributes["state_hash"] = snapshot(self.collector.config.workspace)[1]
        if error_type:
            self.span.status = (
                "submitted"
                if isinstance(error, Submitted)
                else "interrupted"
                if isinstance(error, InterruptAgentFlow)
                else "error"
            )
            if not isinstance(error, Submitted):
                self.span.attributes.update(error_type=error_type.__name__, error=str(error))
            if isinstance(error, FormatError) and error.messages:
                self.span.attributes.update(llm_attributes(error.messages[0], self.collector.run.model))
                self.span.attributes["error_messages"] = list(error.messages)
        elif self.span.status == "running":
            self.span.status = "ok"
        self.collector.run.events = diagnose(self.collector.run)
        self.collector.store.save_run(self.collector.run)
        return False


class Collector:
    def __init__(self, store: Store, config: ObserveConfig):
        self.store = store
        self.config = config
        self.run = Run(new_id(), config.task_id, source="runtime", workspace=config.workspace, exit_status="running")
        self.step_index = 0
        self.parent: str | None = None
        self.message_start = 0

    def scope(self, kind: str, attributes: dict | None = None) -> Scope:
        return Scope(self, kind, attributes)

    def checkpoint(self, agent) -> None:
        self.run.messages = list(agent.messages)
        for span in reversed(self.run.spans):
            if span.kind == "step":
                span.attributes["message_indices"] = list(range(self.message_start, len(agent.messages)))
                break
        self.run.events = diagnose(self.run)
        self.store.save_run(self.run)


class ObservedModel:
    def __init__(self, model, collector: Collector):
        self.wrapped = model
        self.collector = collector

    def __getattr__(self, name):
        return getattr(self.wrapped, name)

    def query(self, *args, **kwargs) -> dict:
        with self.collector.scope("llm") as span:
            result = self.wrapped.query(*args, **kwargs)
            span.attributes.update(llm_attributes(result, self.collector.run.model))
            return result


class ObservedEnvironment:
    def __init__(self, env, collector: Collector):
        self.wrapped = env
        self.collector = collector

    def __getattr__(self, name):
        return getattr(self.wrapped, name)

    def execute(self, action: dict, *args, **kwargs) -> dict:
        cwd = (
            kwargs.get("cwd")
            or (args[0] if args else "")
            or getattr(self.wrapped.config, "cwd", "")
            or (str(Path.cwd()) if isinstance(self.wrapped, LocalEnvironment) else None)
        )
        state = snapshot(self.collector.config.workspace)[1]
        with self.collector.scope("tool", {"tool_name": "shell", **action, "cwd": cwd, "state_before": state}) as span:
            result = self.wrapped.execute(action, *args, **kwargs)
            span.attributes.update(result)
            span.status = tool_status(result)
            return result


class ObservedAgentMixin:
    def __init__(self, model, env, *, collector: Collector, **kwargs):
        self.collector = collector
        super().__init__(ObservedModel(model, collector), ObservedEnvironment(env, collector), **kwargs)

    def step(self, *args, **kwargs):
        self.collector.step_index += 1
        self.collector.message_start = len(self.messages)
        with self.collector.scope("step"):
            return super().step(*args, **kwargs)

    def save(self, *args, **kwargs):
        result = super().save(*args, **kwargs)
        self.collector.checkpoint(self)
        return result

    def run(self, *args, **kwargs):
        run = self.collector.run
        run.config_snapshot = safe_config(self.serialize()["info"]["config"])
        run.model = run.config_snapshot.get("model", {}).get("model_name", "unknown")
        run.start_time = time.time()
        run.metadata["initial_state"] = snapshot(self.collector.config.workspace)[1]
        self.collector.store.save_run(run)
        timer = time.perf_counter()
        try:
            with self.collector.scope("run"):
                return super().run(*args, **kwargs)
        finally:
            run.end_time = time.time()
            run.duration_ms = (time.perf_counter() - timer) * 1000
            data = self.serialize()
            run.exit_status = data["info"].get("exit_status") or "Interrupted"
            run.submission = data["info"].get("submission", "")
            run.diff, run.metadata["final_state"] = snapshot(
                self.collector.config.workspace, run.metadata.get("base_revision", "HEAD")
            )
            raw = self.collector.store.artifacts / f"{run.run_id}.traj.json"
            super().save(raw)
            run.trajectory_path = str(raw)
            self.collector.checkpoint(self)


def observed_agent_class(base: type) -> type:
    return type(f"Observed{base.__name__}", (ObservedAgentMixin, base), {})

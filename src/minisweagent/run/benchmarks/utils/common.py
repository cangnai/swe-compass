"""Shared agent utilities for benchmark runners."""

from minisweagent.agents import get_agent_class
from minisweagent.agents.default import DefaultAgent
from minisweagent.run.benchmarks.utils.batch_progress import RunBatchProgressManager


class ProgressTrackingMixin:
    """Agent that reports per-step progress via :class:`RunBatchProgressManager`."""

    def __init__(self, *args, progress_manager: RunBatchProgressManager, instance_id: str = "", **kwargs):
        super().__init__(*args, **kwargs)
        self.progress_manager = progress_manager
        self.instance_id = instance_id

    def step(self) -> list[dict]:
        self.progress_manager.update_instance_status(self.instance_id, f"Step {self.n_calls + 1:3d} (${self.cost:.2f})")
        return super().step()


class ProgressTrackingAgent(ProgressTrackingMixin, DefaultAgent):
    pass


def get_progress_agent_class(spec: str = "default") -> type:
    agent_class = get_agent_class(spec)
    if agent_class is DefaultAgent:
        return ProgressTrackingAgent
    return type(f"ProgressTracking{agent_class.__name__}", (ProgressTrackingMixin, agent_class), {})

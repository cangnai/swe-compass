"""DefaultAgent with issue-local context, repository retrieval and repair feedback."""

import time

from litellm.exceptions import BadRequestError
from pydantic import Field

from minisweagent.agents.default import AgentConfig, DefaultAgent
from minisweagent.optimization.context_manager import ContextConfig, ContextManager
from minisweagent.optimization.feedback_manager import FeedbackConfig, FeedbackManager
from minisweagent.optimization.repo_search import RepoSearch, RetrievalConfig


class EnhancedAgentConfig(AgentConfig):
    cost_limit: float = 0
    context: ContextConfig = Field(default_factory=ContextConfig)
    retrieval: RetrievalConfig = Field(default_factory=RetrievalConfig)
    feedback: FeedbackConfig = Field(default_factory=FeedbackConfig)


class EnhancedAgent(DefaultAgent):
    def __init__(self, *args, config_class: type = EnhancedAgentConfig, **kwargs):
        super().__init__(*args, config_class=config_class, **kwargs)
        self.feedback_manager = FeedbackManager(self.config.feedback)
        self.context_manager = ContextManager(self.config.context, self.model, feedback_manager=self.feedback_manager)
        self.repo_search = RepoSearch(self.config.retrieval)

    def run(self, task: str = "", **kwargs) -> dict:
        self.feedback_manager = FeedbackManager(self.config.feedback)
        self.context_manager = ContextManager(self.config.context, self.model, task, self.feedback_manager)
        self.cost = 0.0
        self.n_calls = 0
        self.n_consecutive_format_errors = 0
        self._start_time = time.time()
        self.extra_template_vars = {}
        self.repo_search = RepoSearch(self.config.retrieval)
        self.repo_search.install(self.env)
        return super().run(task, **kwargs)

    def _render_template(self, template: str) -> str:
        rendered = super()._render_template(template)
        return rendered + self.repo_search.prompt() if template == self.config.system_template else rendered

    def add_messages(self, *messages: dict) -> list[dict]:
        added = super().add_messages(*messages)
        self.context_manager.ingest(self.messages)
        return added

    def get_model_messages(self) -> list[dict]:
        return self.context_manager.build(self.messages)

    def process_action_output(self, action: dict, output: dict) -> dict:
        return self.feedback_manager.prepare(action, super().process_action_output(action, output))

    def query(self) -> dict:
        try:
            return super().query()
        except BadRequestError as error:
            if not self.context_manager.recover_raw_history(self.messages, str(error)):
                raise
            return super().query()

    def serialize(self, *extra_dicts) -> dict:
        return super().serialize(
            {
                "context_engineering": self.context_manager.serialize(),
                "repository_retrieval": self.repo_search.serialize(),
                "feedback_guided_repair": self.feedback_manager.serialize(),
            },
            *extra_dicts,
        )

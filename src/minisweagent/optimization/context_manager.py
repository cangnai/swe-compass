"""Stable history views, event-based elision and extractive context checkpoints."""

import json
from copy import deepcopy
from dataclasses import asdict, dataclass

from jinja2 import StrictUndefined, Template

from minisweagent import Model
from minisweagent.optimization.feedback_manager import FeedbackManager
from minisweagent.optimization.task_state import TaskState, command_kind, excerpt, message_text


@dataclass
class ContextConfig:
    context_window: int | None = None
    fallback_context_window: int = 32768
    reserved_output_tokens: int = 4096
    request_overhead_tokens: int = 1024
    compaction_threshold: float = 0.85
    keep_recent_turns: int = 4
    min_elision_chars: int = 1000
    summary_chars: int = 1200

    def __post_init__(self):
        if self.context_window is not None and self.context_window <= 0 or self.fallback_context_window <= 0:
            raise ValueError("Context windows must be positive")
        if not 0 < self.compaction_threshold <= 1 or self.keep_recent_turns < 1:
            raise ValueError("Invalid compaction threshold or recent-turn protection")
        if min(self.reserved_output_tokens, self.request_overhead_tokens, self.min_elision_chars) < 0:
            raise ValueError("Context sizes cannot be negative")
        if self.summary_chars < 128:
            raise ValueError("summary_chars must be at least 128")


@dataclass
class Interaction:
    start: int
    end: int
    complete: bool
    protected: bool


class ContextCapacityError(ValueError):
    """The protected context cannot fit; full history remains in the trajectory."""


_CHECKPOINT_TEMPLATE = """<context_checkpoint source_start="{{ start }}" source_end="{{ end }}">
Extractive working memory for earlier interactions. Source indices refer to the full trajectory.
Commands and return codes are observed evidence. Agent notes and edit intentions are unverified.
Excerpts are marked; re-read repository files or rerun commands when more detail is needed.
{{ memory }}
{{ summaries }}
</context_checkpoint>"""


def _calls(message: dict) -> set[str]:
    if message.get("object") == "response":
        return {item["call_id"] for item in message.get("output", []) if item.get("type") == "function_call"}
    return {call["id"] for call in message.get("tool_calls", []) or []}


def _result_id(message: dict) -> str | None:
    return message.get("tool_call_id") or (
        message.get("call_id") if message.get("type") == "function_call_output" else None
    )


def _assistant(message: dict) -> bool:
    return message.get("role") == "assistant" or message.get("object") == "response"


def _has_media(message: dict) -> bool:
    return (
        any(
            isinstance(block, dict)
            and block.get("type") in {"image", "image_url", "input_image", "audio", "input_audio"}
            for block in message.get("content", [])
        )
        if isinstance(message.get("content"), list)
        else False
    )


class ContextManager:
    def __init__(
        self, config: ContextConfig, model: Model, task: str = "", feedback_manager: FeedbackManager | None = None
    ):
        self.config = config
        self.model = model
        self.task_state = TaskState(issue=task)
        self.feedback_manager = feedback_manager
        self.processed = 0
        self.actions: dict[str, dict] = {}
        self.text_actions: list[dict] = []
        self.observations: dict[int, dict] = {}
        self.elisions: dict[int, dict] = {}
        self.checkpoints: list[dict] = []
        self.requests: list[dict] = []
        self.events: list[dict] = []
        self.attempted: set[int] = set()
        self.raw_history = False

    def ingest(self, messages: list[dict]) -> None:
        for i in range(self.processed, len(messages)):
            message = messages[i]
            if message.get("role") == "system":
                self.task_state.constraints.append(message_text(message))
            elif (
                i > 1
                and message.get("role") == "user"
                and "returncode" not in message.get("extra", {})
                and message.get("extra", {}).get("interrupt_type") != "FormatError"
            ):
                self.task_state.constraints.append(message_text(message))
            if _assistant(message):
                self.task_state.note(message, i)
                if self.feedback_manager:
                    self.feedback_manager.note(message_text(message), i, self.task_state)
                self.text_actions = []
                for action in message.get("extra", {}).get("actions", []):
                    action = {**action, "command": str(action.get("command", ""))}
                    if action.get("tool_call_id"):
                        self.actions[action["tool_call_id"]] = action
                    else:
                        self.text_actions.append(action)
            if "returncode" in message.get("extra", {}):
                action = self.actions.get(_result_id(message), {})
                if not action and self.text_actions:
                    action = self.text_actions.pop(0)
                self.observations[i] = action
                self.task_state.observe(action, message, i)
                if self.feedback_manager:
                    self.feedback_manager.observe(action, message, i, self.task_state)
            if message.get("role") == "exit":
                self.task_state.stage = message.get("extra", {}).get("exit_status", "finished")
        self.processed = len(messages)

    def _interactions(self, messages: list[dict]) -> list[Interaction]:
        starts = [i for i, message in enumerate(messages) if i >= 2 and _assistant(message)]
        result = []
        for start, end in zip(starts, starts[1:] + [len(messages)]):
            pending = _calls(messages[start])
            n_outputs = 0
            protected = False
            protected |= _has_media(messages[start])
            for i in range(start + 1, end):
                message = messages[i]
                protected |= _has_media(message)
                if result_id := _result_id(message):
                    pending.discard(result_id)
                if i in self.observations:
                    n_outputs += 1
                elif message.get("role") in {"user", "system", "exit", "developer"}:
                    protected = True
            actions = messages[start].get("extra", {}).get("actions", [])
            protected |= any(
                "COMPLETE_TASK_AND_SUBMIT_FINAL_OUTPUT" in str(action.get("command", "")) for action in actions
            )
            result.append(Interaction(start, end, not pending and n_outputs >= len(actions), protected))
        return result

    def _elide(self, messages: list[dict], turns: list[Interaction]) -> None:
        eligible = {
            i
            for turn in turns[: -self.config.keep_recent_turns]
            if turn.complete and not turn.protected
            for i in range(turn.start, turn.end)
        }
        latest: dict[str, int] = {}
        for i, action in reversed(list(self.observations.items())):
            command = action.get("command", "")
            extra = messages[i].get("extra", {})
            feedback = extra.get("test_feedback", {})
            if feedback and (
                feedback.get("classification") != "test" or feedback.get("outcome") not in {"passed", "failed"}
            ):
                continue
            if command_kind(command) == "other" or extra.get("exception_info"):
                continue
            if i in eligible and command in latest and i not in self.elisions:
                replacement = messages[latest[command]]
                # An ordinary read failure may be an unresolved diagnostic, not stale file content.
                if (
                    command_kind(command) == "test"
                    or extra.get("returncode") == replacement["extra"].get("returncode") == 0
                ):
                    text = message_text(messages[i])
                    if len(text) >= self.config.min_elision_chars:
                        self.elisions[i] = {"replacement": latest[command], "command": command}
                        self.events.append({"type": "elision", "source": i, **self.elisions[i]})
            latest.setdefault(command, i)

    def _view(self, messages: list[dict]) -> tuple[list[dict], list[int | None]]:
        view = []
        indices: list[int | None] = []
        checkpoints = {c["start"]: c for c in self.checkpoints}
        i = 0
        while i < len(messages):
            if not self.raw_history and i in checkpoints:
                view.append(deepcopy(checkpoints[i]["message"]))
                indices.append(None)
                i = checkpoints[i]["end"]
                continue
            message = deepcopy(messages[i])
            if not self.raw_history and i in self.elisions:
                event = self.elisions[i]
                text = (
                    f"[Earlier result cleared; superseded by source message {event['replacement']}. "
                    f"Command: {event['command']}; original returncode: {message.get('extra', {}).get('returncode')}. "
                    f"Full output preserved at source message {i}.]"
                )
                key = "output" if message.get("type") == "function_call_output" else "content"
                message[key] = text
            view.append(message)
            indices.append(i)
            i += 1
        if not self.raw_history and (self.checkpoints or self.elisions):
            memory = self.task_state.serialize()
            memory.pop("issue")
            memory.pop("constraints")
            if self.feedback_manager and self.feedback_manager.config.enabled:
                memory = self.feedback_manager.compact_memory(memory)
            view.append(
                self.model.format_message(
                    role="user",
                    content=Template("<task_memory>\n{{ memory }}\n</task_memory>", undefined=StrictUndefined).render(
                        memory=json.dumps(memory, ensure_ascii=False)
                    ),
                    extra={"context_memory": True},
                )
            )
            indices.append(None)
        if guidance := self._feedback_message():
            view.append(guidance)
            indices.append(None)
        return view, indices

    def _feedback_message(self) -> dict | None:
        if self.feedback_manager and (text := self.feedback_manager.context()):
            return self.model.format_message(role="user", content=text, extra={"feedback_context": True})
        return None

    def _limits(self) -> tuple[int, int, str]:
        from litellm import model_cost

        config = getattr(self.model, "config", None)
        name = getattr(config, "model_name", "")
        metadata = model_cost.get(name, model_cost.get(name.split("/", 1)[-1], {}))
        window = self.config.context_window or metadata.get("max_input_tokens") or self.config.fallback_context_window
        kwargs = getattr(config, "model_kwargs", {})
        reserve = max(
            self.config.reserved_output_tokens,
            *(kwargs.get(key) or 0 for key in ("max_tokens", "max_completion_tokens", "max_output_tokens")),
        )
        return (
            int(window),
            reserve,
            "override"
            if self.config.context_window
            else "metadata"
            if metadata.get("max_input_tokens")
            else "fallback",
        )

    def _size(self, messages: list[dict]) -> int:
        # UTF-8 bytes are a conservative text-token upper bound; exclude trajectory-only payloads.
        return self.config.request_overhead_tokens + sum(
            len(json.dumps({k: v for k, v in message.items() if k != "extra"}, ensure_ascii=False).encode("utf-8"))
            for message in messages
        )

    def _checkpoint(self, messages: list[dict], turn: Interaction) -> dict:
        memory = TaskState(issue=self.task_state.issue)
        summaries = []
        for i in range(turn.start, turn.end):
            message = messages[i]
            if _assistant(message):
                memory.note(message, i)
                if text := message_text(message):
                    summaries.append(f"Source {i}, agent note (unverified): {excerpt(text, self.config.summary_chars)}")
            if i in self.observations:
                action = self.observations[i]
                memory.observe(action, message, i)
                if i not in self.elisions:
                    summaries.append(
                        f"Source {i}, command: {excerpt(action.get('command', ''), self.config.summary_chars)}\n"
                        f"returncode={message.get('extra', {}).get('returncode')}; "
                        f"exception={message.get('extra', {}).get('exception_info')}\n"
                        f"{excerpt(message_text(message), self.config.summary_chars)}"
                    )
        state = memory.serialize()
        if self.feedback_manager and self.feedback_manager.config.enabled:
            state = self.feedback_manager.compact_memory(state)
        # Agent notes above already preserve hypotheses; keep the structured snapshot compact.
        state.pop("hypotheses")
        state.pop("issue")
        content = Template(_CHECKPOINT_TEMPLATE, undefined=StrictUndefined).render(
            start=turn.start, end=turn.end, memory=json.dumps(state, ensure_ascii=False), summaries="\n".join(summaries)
        )
        return {
            "start": turn.start,
            "end": turn.end,
            "message": self.model.format_message(role="user", content=content),
        }

    def build(self, messages: list[dict]) -> list[dict]:
        self.ingest(messages)
        turns = self._interactions(messages)
        if not self.raw_history:
            self._elide(messages, turns)
        view, indices = self._view(messages)
        before = self._size(view)
        window, reserve, source = self._limits()
        target = int((window - reserve) * self.config.compaction_threshold)
        for turn in turns[: -self.config.keep_recent_turns]:
            if self.raw_history or self._size(view) <= target:
                break
            if not turn.complete or turn.protected or turn.start in self.attempted:
                continue
            self.attempted.add(turn.start)
            try:
                checkpoint = self._checkpoint(messages, turn)
            except (TypeError, ValueError) as error:
                self.events.append(
                    {"type": "compaction_failed", "start": turn.start, "end": turn.end, "error": str(error)}
                )
                continue
            if not isinstance(checkpoint["message"], dict) or not message_text(checkpoint["message"]).strip():
                self.events.append(
                    {"type": "compaction_failed", "start": turn.start, "end": turn.end, "error": "empty summary"}
                )
                continue
            original = self._size(view)
            self.checkpoints.append(checkpoint)
            self.checkpoints.sort(key=lambda c: c["start"])
            candidate, candidate_indices = self._view(messages)
            if self._size(candidate) < original:
                view, indices = candidate, candidate_indices
                self.events.append({"type": "compaction", "start": turn.start, "end": turn.end})
            else:
                self.checkpoints.remove(checkpoint)
                self.events.append(
                    {"type": "compaction_skipped", "start": turn.start, "end": turn.end, "reason": "no reduction"}
                )
        estimated = self._size(view)
        if estimated + reserve > window and self.recover_raw_history(messages, "Processed view exceeds context window"):
            view, indices = self._view(messages)
            estimated = self._size(view)
        self.requests.append(
            {
                "source_indices": indices,
                "estimated_input_tokens": estimated,
                "before_compaction": before,
                "context_window": window,
                "window_source": source,
                "reserved_output_tokens": reserve,
                "checkpoint_ranges": [] if self.raw_history else [[c["start"], c["end"]] for c in self.checkpoints],
                "elided_indices": [] if self.raw_history else [i for i in indices if i in self.elisions],
                "raw_history": self.raw_history,
                "memory_message": next((m for m in reversed(view) if m.get("extra", {}).get("context_memory")), None),
                "feedback_message": next(
                    (m for m in reversed(view) if m.get("extra", {}).get("feedback_context")), None
                ),
            }
        )
        if estimated + reserve > window:
            raise ContextCapacityError(
                f"Protected context needs approximately {estimated} input + {reserve} output tokens, window={window}. "
                "Full history and task memory are preserved; increase context_window or reduce recent-turn protection."
            )
        return view

    def recover_raw_history(self, messages: list[dict], error: str) -> bool:
        window, reserve, _ = self._limits()
        guidance = self._feedback_message()
        if (
            self.raw_history
            or not (self.checkpoints or self.elisions)
            or self._size(messages + ([guidance] if guidance else [])) + reserve > window
        ):
            return False
        self.raw_history = True
        self.events.append({"type": "raw_history_fallback", "error": error})
        return True

    def serialize(self) -> dict:
        return {
            "config": asdict(self.config),
            "task_state": self.task_state.serialize(),
            "checkpoints": self.checkpoints,
            "elisions": self.elisions,
            "requests": self.requests,
            "events": self.events,
            "raw_history_fallback": self.raw_history,
        }

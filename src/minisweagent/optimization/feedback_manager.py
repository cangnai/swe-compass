"""Issue-local test evidence, failure history and event-triggered reflection."""

import hashlib
import json
import re
import shlex
from dataclasses import asdict, dataclass, field
from pathlib import PurePosixPath
from typing import TYPE_CHECKING

from jinja2 import StrictUndefined, Template

if TYPE_CHECKING:
    from minisweagent.optimization.task_state import TaskState


@dataclass
class FeedbackConfig:
    enabled: bool = False
    repeat_failure_threshold: int = 3
    test_command_patterns: list[str] = field(default_factory=list)
    max_evidence_chars: int = 1600
    max_failures: int = 20
    max_attempts: int = 5

    def __post_init__(self) -> None:
        if self.repeat_failure_threshold < 2 or self.max_evidence_chars < 256:
            raise ValueError("Feedback needs a repetition threshold >= 2 and evidence budget >= 256")
        if min(self.max_failures, self.max_attempts) < 1:
            raise ValueError("Feedback history limits must be positive")
        for pattern in self.test_command_patterns:
            re.compile(pattern)


def _tokens(command: str) -> list[str]:
    command = re.sub(r"\b[A-Za-z]:\\[^\s\"']+", lambda match: match[0].replace("\\", "/"), command)
    try:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()")
        lexer.whitespace_split = True
        lexer.commenters = ""
        return list(lexer)
    except ValueError:
        return []


def _runner(parts: list[str]) -> bool:
    while parts:
        name = PurePosixPath(parts[0].replace("\\", "/")).name.lower().removesuffix(".exe")
        if re.match(r"^\w+=", parts[0]) or name == "env":
            parts = parts[1:]
        elif name == "timeout" and len(parts) > 2:
            parts = parts[2:]
        elif name in {"uv", "poetry", "pipenv"} and parts[1:2] == ["run"]:
            parts = parts[2:]
        elif name == "coverage" and parts[1:2] == ["run"]:
            parts = ["python", *parts[2:]]
        else:
            break
    if not parts:
        return False
    if name in {"pytest", "py.test", "unittest", "tox", "nox"}:
        return True
    if re.fullmatch(r"(?:python\d*(?:\.\d+)?|py)", name):
        if "-c" in parts:
            return False
        if "-m" in parts:
            index = parts.index("-m") + 1
            if parts[index : index + 1] == ["coverage"]:
                return _runner(parts[index:])
            return parts[index : index + 1] in [["pytest"], ["unittest"], ["tox"], ["nox"]]
        script = next((part for part in parts[1:] if part.endswith(".py")), "")
        script = PurePosixPath(script.replace("\\", "/")).name
        return bool(
            script == "manage.py"
            and "test" in parts
            or script == "setup.py"
            and "test" in parts
            or re.fullmatch(r"(?:runtests|run_tests|test\w*|repro\w*)\.py", script)
        )
    if name in {"bash", "sh"} and len(parts) > 1:
        name = PurePosixPath(parts[1]).name
    return bool(
        name in {"make", "npm", "yarn"}
        and any(re.fullmatch(r"(?:test|check)(?:[-_:].*)?", p) for p in parts[1:])
        or re.fullmatch(r"(?:run[_-]?tests?|runtests|test)(?:[_.-].*)?", name)
    )


def count_test_invocations(command: str, patterns: list[str] | None = None) -> int:
    tokens = _tokens(command)
    segments: list[list[str]] = [[]]
    for token in tokens:
        if token in {";", "&&", "||", "|", "&", "(", ")"}:
            segments.append([])
        else:
            segments[-1].append(token)
    return sum(_runner(parts) for parts in segments) or int(any(re.search(p, command) for p in patterns or []))


def _scope(command: str) -> str:
    return (
        shlex.join(
            [
                token
                for token in _tokens(command)
                if token not in {"-q", "-v", "-vv", "--quiet", "--verbose", "-s"}
                and not token.startswith(("--tb=", "--color="))
            ]
        )
        or command.strip()
    )


def _normalize(text: str) -> str:
    text = text.replace("\\", "/")
    text = re.sub(r"(?:[A-Za-z]:)?[^\s'\"]*/pytest-of-[^/\s]+/pytest-\d+", "<pytest_tmp>", text)
    text = re.sub(r"/(?:tmp|var/tmp)/tmp[\w.-]+", "/tmp/<tmp>", text)
    text = re.sub(r"\b0x[\da-fA-F]+\b", "<address>", text)
    text = re.sub(r"\b\d{4}-\d\d-\d\d[T ][\d:.+-]+Z?\b", "<timestamp>", text)
    text = re.sub(r"\bline \d+\b|(?<=\.py):\d+", "<line>", text)
    return re.sub(r"\s+", " ", re.sub(r"^\s*E\s+", "", text)).strip()


_ERROR = r"(?:[\w.]+(?:Error|Exception)|Failed|KeyboardInterrupt|SystemExit)"
_DIAGNOSTIC = re.compile(rf"^\s*(?:E\s+)?(?:assert\b|{_ERROR}(?::|$))")


def _evidence(text: str, limit: int) -> list[str]:
    lines = text.splitlines()
    selected = [line.strip() for line in lines if _DIAGNOSTIC.search(line)]
    selected += [
        line.strip() for line in lines if re.search(r'Traceback|File ["\']|\.py:\d+|^[<>+-] |^Expected|^Actual', line)
    ]
    result = []
    for line in dict.fromkeys(selected):
        remaining = limit - sum(len(item) + 1 for item in result)
        if remaining <= 0:
            break
        result.append(line[:remaining])
    return result


def _locations(text: str) -> list[dict]:
    locations = []
    for match in re.finditer(r"""File ["']([^"']+)["'], line (\d+)|([\w./\\-]+\.py):(\d+)""", text):
        location = {"path": match[1] or match[3], "line": int(match[2] or match[4])}
        if location not in locations:
            locations.append(location)
    return locations[-10:]


def _block(text: str, node: str) -> str:
    headings = list(re.finditer(r"^_{3,}\s+(.+?)\s+_{3,}\s*$|^(?:FAIL|ERROR): (.+)$", text, re.MULTILINE))
    leaf = node.split("::")[-1].split(" (")[0]
    for index, heading in enumerate(headings):
        if leaf in heading.group():
            return text[heading.end() : headings[index + 1].start() if index + 1 < len(headings) else len(text)]
    return text


@dataclass
class Failure:
    signature: str
    node: str
    failure_type: str
    evidence: list[str]
    locations: list[dict]


@dataclass
class TestFeedback:
    command: str
    scope: str
    classification: str
    outcome: str
    returncode: int | None
    complete: bool = False
    failures: list[Failure] = field(default_factory=list)
    passed_tests: list[str] = field(default_factory=list)
    counts: dict[str, int] = field(default_factory=dict)
    evidence: list[str] = field(default_factory=list)
    exception_info: str = ""
    truncated: bool = False


def parse_feedback(command: str, output: dict, config: FeedbackConfig) -> TestFeedback:
    text = re.sub(r"\x1b\[[0-?]*[ -/]*[@-~]", "", str(output.get("output", ""))).replace("\r\n", "\n")
    invocations = count_test_invocations(command, config.test_command_patterns)
    feedback = TestFeedback(
        command, _scope(command), "test" if invocations else "command", "inconclusive", output.get("returncode")
    )
    exception = output.get("exception_info") or output.get("extra", {}).get("exception") or ""
    exception_type = output.get("extra", {}).get("exception_type", "")
    infrastructure = re.search(
        r"Cannot connect to the Docker daemon|error during connect:|Error response from daemon:|OCI runtime exec failed|"
        r"failed to connect to (?:the )?(?:Docker|docker) API|command not found|is not recognized as an internal|"
        r"No module named ['\"]?(?:pytest|unittest)['\"]?\s*$",
        text,
        re.MULTILINE,
    )
    report_seen = bool(re.search(r"\b\d+ (?:passed|failed|errors?)\b|^Ran \d+ tests?", text, re.MULTILINE))
    signaled = isinstance(feedback.returncode, int) and feedback.returncode < 0
    if (
        exception
        or exception_type
        or infrastructure
        and not report_seen
        or signaled
        or feedback.returncode in {126, 127, 137, 143}
    ):
        feedback.classification = "system_error"
        feedback.exception_info = str(
            exception or exception_type or (infrastructure[0] if infrastructure else "Process terminated")
        )
        feedback.outcome = (
            "interrupted"
            if re.search(r"timeout|timed out|Timeout|terminated|killed", feedback.exception_info)
            or signaled
            or feedback.returncode in {137, 143}
            else "inconclusive"
        )
        feedback.evidence = _evidence(text, config.max_evidence_chars)
        return feedback
    if not invocations:
        feedback.outcome = (
            "completed"
            if feedback.returncode == 0
            else "nonzero"
            if feedback.returncode is not None
            else "inconclusive"
        )
        return feedback
    feedback.counts = {
        status: int(count)
        for count, status in re.findall(r"\b(\d+) (passed|failed|errors?|skipped|xfailed|xpassed)\b", text)
    }
    ran = re.search(r"^Ran (\d+) tests? in ", text, re.MULTILINE)
    if ran:
        feedback.counts["ran"] = int(ran[1])
        feedback.counts.update(
            {
                {
                    "failures": "failed",
                    "errors": "errors",
                    "skipped": "skipped",
                    "expected failures": "xfailed",
                    "unexpected successes": "xpassed",
                }[status]: int(count)
                for status, count in re.findall(
                    r"(failures|errors|skipped|expected failures|unexpected successes)=(\d+)", text
                )
            }
        )
        feedback.counts["passed"] = max(0, int(ran[1]) - sum(v for k, v in feedback.counts.items() if k != "ran"))
    feedback.passed_tests = list(dict.fromkeys(re.findall(r"^(\S+::[^\n]+?)\s+PASSED\b", text, re.MULTILINE)))
    feedback.passed_tests += re.findall(r"^(test\S+ \([^\n]+\))\s+\.\.\. ok\s*$", text, re.MULTILINE)
    failures: list[tuple[str, str]] = []
    for match in re.finditer(r"^(?:FAILED|ERROR)\s+(.+)$|^(?:FAIL|ERROR): (.+)$", text, re.MULTILINE):
        node, _, reason = (match[1] or match[2]).partition(" - ")
        failures.append((node.removeprefix("collecting ").strip(), reason.strip()))
    failed_summary = bool(re.search(r"^FAILED\s*\(|\b[1-9]\d* (?:failed|errors?)\b", text, re.MULTILINE))
    if not failures and failed_summary:
        failures.append(("<unattributed>", ""))
    if not failures and "Traceback (most recent call last):" in text and re.search(rf"^{_ERROR}:", text, re.MULTILINE):
        failures.append((next((p for p in _tokens(command) if p.endswith(".py")), "<test process>"), ""))
    feedback.truncated = len(failures) > config.max_failures
    for node, reason in list(dict.fromkeys(failures))[: config.max_failures]:
        block = _block(text, node)
        if block == text and len(failures) > 1:
            block = reason
        evidence = _evidence(block, config.max_evidence_chars)
        if reason:
            reason = reason[: config.max_evidence_chars // 2]
            evidence = [reason, *_evidence(block, config.max_evidence_chars - len(reason) - 1)]
        types = re.findall(rf"^\s*(?:E\s+)?({_ERROR})(?::|$)", reason, re.MULTILINE)
        if not types:
            types = re.findall(rf"^\s*(?:E\s+)?({_ERROR})(?::|$)", block, re.MULTILINE)
        failure_type = (
            types[-1] if types else "AssertionError" if any("assert " in item for item in evidence) else "TestFailure"
        )
        stable = list(dict.fromkeys(_normalize(line) for line in evidence if _DIAGNOSTIC.search(line)))
        signature = hashlib.sha256(
            json.dumps([_normalize(node), failure_type, stable or [_normalize(reason)]], sort_keys=True).encode()
        ).hexdigest()[:20]
        feedback.failures.append(Failure(signature, node, failure_type, evidence, _locations(block)))
    interrupted = bool(re.search(r"KeyboardInterrupt|^!+ Interrupted:", text, re.MULTILINE)) or feedback.returncode in {
        130,
        -2,
    }
    if interrupted and not re.search(r"error[s]? during collection", text):
        feedback.outcome = "interrupted"
    elif feedback.failures:
        feedback.outcome = "failed"
    elif (
        feedback.returncode == 0
        and feedback.counts.get("passed", 0)
        and (not ran or re.search(r"^OK(?:\s|$)", text, re.MULTILINE))
    ):
        feedback.outcome = "passed"
    feedback.complete = bool(
        invocations == 1
        and feedback.outcome in {"passed", "failed"}
        and not feedback.truncated
        and (ran or feedback.counts)
        and not re.search(r"during collection|KeyboardInterrupt|INTERNALERROR|stopping after", text, re.IGNORECASE)
        and not any(token in {"-x", "-f", "--failfast", "--lf", "--last-failed"} for token in _tokens(command))
        and not any(t.startswith("--maxfail") for t in _tokens(command))
    )
    feedback.evidence = _evidence(text, config.max_evidence_chars)
    return feedback


@dataclass
class FailureRecord:
    failure: Failure
    first_source: int
    latest_source: int
    occurrences: int = 0
    status: str = "active"
    scopes: list[str] = field(default_factory=list)
    attempts: list[dict] = field(default_factory=list)
    last_change_source: int | None = None


_GUIDANCE = """<test_feedback>
Observed execution feedback (source indices refer to the full trajectory):
{{ feedback }}
{% if system_error %}This is an execution/environment problem. Check the runner or environment; do not infer a source-code bug from it.
{% else %}Use the failed nodes, assertions and locations as evidence, not confirmed root causes. After edits choose focused tests or a reproduction command, then relevant regression tests; a full suite is not required after every edit. A passing run covers only its executed scope and does not end the task automatically.
{% endif %}{% if reflection %}<reflection event="{{ reflection.id }}">
{{ reflection.summary }}
Reassess the unverified hypothesis against this evidence. Inspect the relevant source or use repo_search with the observed test names, exception and locations; choose a different validation when repeated attempts provide no new information. Explain the revised hypothesis and next validation in your normal response.
</reflection>{% endif %}
</test_feedback>"""


class FeedbackManager:
    def __init__(self, config: FeedbackConfig):
        self.config = config
        self.history: list[dict] = []
        self.failures: dict[str, FailureRecord] = {}
        self.previous: dict[str, dict] = {}
        self.latest: dict = {}
        self.reflections: list[dict] = []
        self.pending: dict | None = None
        self.last_group: tuple[str, ...] = ()
        self.streak = 0
        self.notified: set[tuple[str, tuple[str, ...]]] = set()

    def prepare(self, action: dict, output: dict) -> dict:
        if not self.config.enabled:
            return output
        feedback = parse_feedback(str(action.get("command", "")), output, self.config)
        if feedback.classification == "command":
            return output
        return {**output, "extra": {**output.get("extra", {}), "test_feedback": asdict(feedback)}}

    def note(self, text: str, source: int, task: "TaskState") -> None:
        if self.pending and self.latest.get("classification") != "system_error":
            self.pending["response"] = {
                "source": source,
                "text": text[: self.config.max_evidence_chars],
                "status": "unverified",
            }
            self.pending = None
            task.reflections = self.reflections[-self.config.max_attempts :]

    def observe(self, action: dict, message: dict, source: int, task: "TaskState") -> None:
        feedback = message.get("extra", {}).get("test_feedback")
        if not self.config.enabled or not feedback:
            return
        run = {**feedback, "source": source, "action": dict(action)}
        self.history.append(run)
        self.latest = run
        if run["classification"] != "test" or run["outcome"] not in {"passed", "failed"}:
            return
        group = tuple(sorted(f["signature"] for f in run["failures"]))
        previous = self.previous.get(run["scope"], {})
        explicit_passes = set(run["passed_tests"])
        for signature, record in self.failures.items():
            if signature in group or record.status == "resolved":
                continue
            if record.failure.node in explicit_passes or run["complete"] and run["scope"] in record.scopes:
                record.status = (
                    "resolved"
                    if record.failure.node in explicit_passes
                    or run["outcome"] == "passed"
                    and not any(run["counts"].get(k, 0) for k in ("skipped", "xfailed"))
                    else "not_observed"
                )
                record.last_change_source = source
        changes = [
            {"source": item.source, "command": item.command[:600], "status": item.status, "returncode": item.returncode}
            for item in task.changes.values()
            if previous.get("source", -1) < item.source < source
        ]
        changes = list({item["source"]: item for item in changes}.values())[-self.config.max_attempts :]
        for failure in run["failures"]:
            signature = failure["signature"]
            if signature not in self.failures:
                self.failures[signature] = FailureRecord(Failure(**failure), source, source)
            record = self.failures[signature]
            record.failure = Failure(**failure)
            record.latest_source = source
            record.occurrences += 1
            record.status = "active"
            if run["scope"] not in record.scopes:
                record.scopes.append(run["scope"])
            record.attempts = (record.attempts + [{"source": source, "command": run["command"], "changes": changes}])[
                -self.config.max_attempts :
            ]
        self.streak = self.streak + 1 if group and group == self.last_group else int(bool(group))
        if group != self.last_group:
            self.notified.clear()
        self.last_group = group
        if self.pending and not any(self.failures[s].status == "active" for s in self.pending["failure_signatures"]):
            self.pending["superseded_by"] = source
            self.pending = None
        reasons = []
        if group and previous.get("outcome") == "passed":
            reasons.append("regression_after_passing_scope")
        claim = task.hypotheses.get("current")
        if (
            group
            and claim
            and not re.search(r"\b(?:not|never|isn't|wasn't)\b|尚未|未修复|没有", claim.text, re.IGNORECASE)
            and re.search(
                r"\b(?:all (?:the )?tests? (?:now )?pass(?:ed)?|(?:bug|issue) (?:is |was )?fixed)\b|已修复|测试(?:全部)?通过",
                claim.text,
                re.IGNORECASE,
            )
        ):
            reasons.append("unverified_claim_contradicted")
        if group and self.streak >= self.config.repeat_failure_threshold:
            reasons.append("repeated_failure_without_progress")
        new_reasons = [reason for reason in reasons if (reason, group) not in self.notified]
        if new_reasons:
            event = {
                "id": len(self.reflections) + 1,
                "source": source,
                "reasons": new_reasons,
                "failure_signatures": list(group),
                "hypothesis": asdict(claim) if claim else None,
                "changes": changes,
                "streak": self.streak,
                "response": None,
                "summary": json.dumps(
                    {
                        "trigger": new_reasons,
                        "consecutive_failed_runs": self.streak,
                        "previous_outcome": previous.get("outcome"),
                        "hypothesis_unverified": claim.text[:600] if claim else "",
                        "edit_intentions_not_verified_patches": changes,
                        "failed_nodes": [f["node"] for f in run["failures"]],
                    },
                    ensure_ascii=False,
                ),
            }
            self.reflections.append(event)
            self.pending = event
            self.notified.update((reason, group) for reason in new_reasons)
        task.failures = {signature: asdict(record) for signature, record in self.failures.items()}
        task.reflections = self.reflections[-self.config.max_attempts :]
        self.previous[run["scope"]] = run

    def context(self) -> str:
        if not self.latest:
            return ""
        feedback = self._compact_feedback(self.latest)
        return Template(_GUIDANCE, undefined=StrictUndefined).render(
            feedback=json.dumps(feedback, ensure_ascii=False),
            system_error=self.latest["classification"] == "system_error",
            reflection=self.pending if self.latest["classification"] != "system_error" else None,
        )

    def _compact_feedback(self, feedback: dict, evidence_chars: int | None = None) -> dict:
        result = {k: v for k, v in feedback.items() if k not in {"action", "scope", "evidence", "failures"}}
        budget = (evidence_chars or self.config.max_evidence_chars) // max(1, len(feedback.get("failures", [])))
        result["failures"] = [
            {**failure, "evidence": ["\n".join(failure["evidence"])[:budget]], "locations": failure["locations"][-2:]}
            for failure in feedback.get("failures", [])
        ]
        if not result["failures"]:
            result["evidence"] = feedback.get("evidence", [])
        return result

    def compact_memory(self, memory: dict) -> dict:
        memory["test_feedback"] = {
            command: {k: v for k, v in run.items() if k not in {"failures", "evidence", "scope"}}
            for command, run in memory["test_feedback"].items()
        }
        records = sorted(
            memory["failures"].items(), key=lambda item: (item[1]["status"] != "active", -item[1]["latest_source"])
        )
        memory["failure_records_omitted"] = max(0, len(records) - self.config.max_failures)
        budget = max(1, self.config.max_evidence_chars // max(1, min(len(records), self.config.max_failures)))
        memory["failures"] = {
            signature: {
                **{k: v for k, v in record.items() if k not in {"failure", "attempts", "scopes"}},
                "failure": self._compact_feedback({"failures": [record["failure"]]}, budget)["failures"][0],
                "attempt_sources": [attempt["source"] for attempt in record["attempts"]],
            }
            for signature, record in records[: self.config.max_failures]
        }
        memory["reflections"] = [
            {"id": event["id"], "source": event["source"], "reasons": event["reasons"], "response": event["response"]}
            for event in memory["reflections"]
        ]
        return memory

    def serialize(self) -> dict:
        return {
            "config": asdict(self.config),
            "history": self.history,
            "failures": {signature: asdict(record) for signature, record in self.failures.items()},
            "reflections": self.reflections,
            "pending_reflection": self.pending["id"] if self.pending else None,
        }

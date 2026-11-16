"""Issue-local working memory with references to the authoritative trajectory."""

import json
import re
from dataclasses import asdict, dataclass, field

from minisweagent.optimization.repo_retriever import METADATA_PREFIX


def is_repo_search(command: str) -> bool:
    return bool(re.search(r"""(?:^repo_search\s|[/\\]repo_search\.py["']?(?:\s|$))""", command))


def message_text(message: dict) -> str:
    content = message.get("content", message.get("output", ""))
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "\n".join(
            message_text(item) if item.get("type") == "message" else item.get("text", "")
            for item in content
            if isinstance(item, dict)
        )
    return ""


def excerpt(text: str, limit: int = 1200) -> str:
    if len(text) <= limit:
        return text
    diagnostics = "\n".join(
        line for line in text.splitlines() if re.search(r"Traceback|Error|Exception|FAILED|assert |^E\s", line)
    )
    return f"{text[: limit // 2]}\n[excerpt; full text in source messages]\n{diagnostics}\n{text[-limit // 2 :]}"


def command_kind(command: str) -> str:
    """Only simple, repeatable commands qualify for automatic supersession."""
    if re.search(r"[\n;&|<>`]|\$\(", command):
        return "other"
    if re.match(r"^(?:python(?:3)? -m )?(?:pytest|unittest)\b|^(?:tox|nox)\b", command):
        return "test"
    if re.match(r"^(?:cat|head|tail|sed -n|rg|grep|find|ls)\s", command):
        return "read"
    if is_repo_search(command):
        return "read"
    return "other"


@dataclass
class MemoryItem:
    text: str
    source: int
    status: str = "observed"
    command: str = ""
    returncode: int | None = None


@dataclass
class TaskState:
    issue: str = ""
    constraints: list[str] = field(default_factory=list)
    stage: str = "working"
    files: dict[str, MemoryItem] = field(default_factory=dict)
    symbols: dict[str, MemoryItem] = field(default_factory=dict)
    localizations: dict[str, dict] = field(default_factory=dict)
    changes: dict[str, MemoryItem] = field(default_factory=dict)
    tests: dict[str, MemoryItem] = field(default_factory=dict)
    test_feedback: dict[str, dict] = field(default_factory=dict)
    failures: dict[str, dict] = field(default_factory=dict)
    reflections: list[dict] = field(default_factory=list)
    unresolved: dict[str, MemoryItem] = field(default_factory=dict)
    hypotheses: dict[str, MemoryItem] = field(default_factory=dict)
    next_steps: list[MemoryItem] = field(default_factory=list)

    def note(self, message: dict, source: int) -> None:
        if text := message_text(message):
            self.hypotheses = {"current": MemoryItem(excerpt(text), source, "unverified")}
            self.next_steps = [
                MemoryItem(line, source, "unverified")
                for line in text.splitlines()
                if re.search(r"\b(?:next|plan|will|should|need to)\b|下一步|计划|接下来", line, re.IGNORECASE)
            ]

    def observe(self, action: dict, message: dict, source: int) -> None:
        command = str(action.get("command", ""))
        extra = message.get("extra", {})
        output = extra.get("raw_output", message_text(message))
        item = MemoryItem(excerpt(str(output)), source, command=command, returncode=extra.get("returncode"))
        kind = command_kind(command)
        feedback = extra.get("test_feedback")
        if feedback:
            self.test_feedback[command] = {**feedback, "source": source}
        if kind == "test" or feedback and feedback.get("classification") == "test":
            self.tests[command] = item
            self.stage = "testing"
        no_matches = item.returncode == 1 and re.match(r"^(?:rg|grep)\s", command)
        if extra.get("exception_info") or item.returncode not in (0, None) and not no_matches:
            self.unresolved[command] = item
        elif item.returncode == 0:
            self.unresolved.pop(command, None)
        retrieval = is_repo_search(command)
        if retrieval and item.returncode == 0:
            self._localize(str(output), command, source)
        if kind == "read" and item.returncode == 0 and not retrieval:
            for path in re.findall(r"[\w./-]+\.[a-zA-Z][\w]*", command + "\n" + str(output)):
                self.files[path] = MemoryItem("Path observed; relevance not verified", source, command=command)
            for symbol in re.findall(r"\b(?:def|class|function)\s+(\w+)", str(output)):
                self.symbols[symbol] = MemoryItem("Definition observed; cause not verified", source, command=command)
        if not retrieval and re.search(
            r"\b(?:apply_patch|patch|sed -i|git (?:apply|restore|checkout)|tee)\b|(?:>|write_text\(|write_bytes\(|\.write\()",
            command,
        ):
            self.changes[str(source)] = MemoryItem(
                excerpt(command), source, "unverified", command=command, returncode=item.returncode
            )
            self.stage = "editing"
        if re.fullmatch(r"git diff --name-only", command.strip()) and item.returncode == 0:
            for path in str(output).splitlines():
                self.changes[path] = MemoryItem("Changed path reported by git diff", source, command=command)

    def _localize(self, output: str, command: str, source: int) -> None:
        for line in reversed(output.splitlines()):
            if not line.startswith(METADATA_PREFIX):
                continue
            try:
                metadata = json.loads(line.removeprefix(METADATA_PREFIX))
            except json.JSONDecodeError:
                return
            if not isinstance(metadata, dict) or metadata.get("version") != 1:
                return
            results = metadata.get("results")
            if not isinstance(results, list) or not all(isinstance(metadata.get(k), str) for k in ("root", "query")):
                return
            for path, previous in list(self.localizations.items()):
                if previous["root"] != metadata["root"] or previous["query"] != metadata["query"]:
                    continue
                del self.localizations[path]
                self.symbols = {name: item for name, item in self.symbols.items() if not name.startswith(path + ":")}
                if self.files[path].status == "candidate" and self.files[path].source == previous["source"]:
                    del self.files[path]
            for result in results:
                if not isinstance(result, dict) or not isinstance(result.get("path"), str):
                    continue
                path = result["path"]
                if not isinstance(result.get("reasons"), list) or not all(
                    isinstance(r, str) for r in result["reasons"]
                ):
                    continue
                evidence = f"Retrieval candidate; cause unverified. {path}:{result.get('start')}-{result.get('end')}; "
                evidence += "; ".join(result["reasons"])
                self.files[path] = MemoryItem(evidence, source, "candidate", command, 0)
                if self.localizations.get(path, {}).get("source") != source:
                    self.localizations[path] = {
                        "source": source,
                        "command": command,
                        "status": "candidate",
                        "root": metadata.get("root"),
                        "query": metadata.get("query"),
                        "results": [],
                    }
                    self.symbols = {
                        name: item for name, item in self.symbols.items() if not name.startswith(path + ":")
                    }
                self.localizations[path]["results"].append(result)
                symbol = result.get("symbol")
                if isinstance(symbol, dict) and isinstance(symbol.get("name"), str):
                    self.symbols[f"{path}:{symbol['name']}"] = MemoryItem(evidence, source, "candidate", command, 0)
            return

    def serialize(self) -> dict:
        return asdict(self)

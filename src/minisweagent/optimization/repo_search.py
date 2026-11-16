"""Deploy the standalone retriever through the existing execution environment."""

import base64
import json
import os
import shlex
import subprocess
import zlib
from dataclasses import asdict, dataclass
from pathlib import Path

from jinja2 import StrictUndefined, Template

from minisweagent import Environment
from minisweagent.environments.local import LocalEnvironment
from minisweagent.optimization import repo_retriever


@dataclass
class RetrievalConfig:
    enabled: bool = False
    python: str = "python"
    top_k: int = 5
    snippet_lines: int = 24
    candidate_limit: int = 40
    max_file_bytes: int = 1_000_000

    def __post_init__(self):
        if not self.python or not 1 <= self.top_k <= 50 or not 1 <= self.snippet_lines <= 100:
            raise ValueError("Invalid retrieval interpreter or output limits")
        if self.candidate_limit < 1 or self.max_file_bytes < 1:
            raise ValueError("Retrieval candidate and file size limits must be positive")


_PROMPT = """
<repository_retrieval>
Optional repo_search helper for the current task repository (ordinary Bash remains available):
{{ command }} {{ query_example }}
Use --root PATH for another working directory, --top-k N or --snippet-lines N for more detail.
Quote queries as shell arguments; never paste unquoted issue text into shell commands.
Returns ranked candidate files, Python AST symbols, line ranges, evidence and bounded code snippets.
Other languages and invalid Python use text search. Results are clues, not verified bug causes.
Each invocation rereads the current files; rerun after edits to refresh locations.
If no match is found, refine the query or continue with rg/grep and ordinary Bash.
</repository_retrieval>"""


class RepoSearch:
    def __init__(self, config: RetrievalConfig):
        self.config = config
        self.command = ""
        self.query_example = ""
        self.setup: dict = {}

    def install(self, env: Environment) -> None:
        if not self.config.enabled:
            return
        wrapped = env
        while hasattr(wrapped, "wrapped"):
            wrapped = wrapped.wrapped
        quote = subprocess.list2cmdline if os.name == "nt" and isinstance(wrapped, LocalEnvironment) else shlex.join
        self.query_example = quote(["issue keywords, API names, paths or traceback"])
        payload = base64.b64encode(zlib.compress(Path(repo_retriever.__file__).read_bytes())).decode()
        bootstrap = (
            "import base64,json,tempfile,zlib; from pathlib import Path; "
            "cwd=Path.cwd().resolve(); "
            "root=next((p for p in (cwd,*cwd.parents) if (p/'.git').exists()),cwd); "
            "base=Path(tempfile.gettempdir()).resolve(); "
            "assert base != root and root not in base.parents, 'Tool directory must be outside the repository'; "
            "path=Path(tempfile.mkdtemp(prefix='mini-repo-search-',dir=str(base)))/'repo_search.py'; "
            f"path.write_bytes(zlib.decompress(base64.b64decode('{payload}'))); "
            "print('REPO_SEARCH_INSTALLED '+json.dumps(str(path)))"
        )
        self.setup = env.execute({"command": quote([self.config.python, "-c", bootstrap])})
        if self.setup.get("returncode") != 0 or self.setup.get("exception_info"):
            return
        for line in self.setup.get("output", "").splitlines():
            if line.startswith("REPO_SEARCH_INSTALLED "):
                path = json.loads(line.removeprefix("REPO_SEARCH_INSTALLED "))
                self.command = quote(
                    [
                        self.config.python,
                        "-B",
                        path,
                        "--top-k",
                        str(self.config.top_k),
                        "--snippet-lines",
                        str(self.config.snippet_lines),
                        "--candidate-limit",
                        str(self.config.candidate_limit),
                        "--max-file-bytes",
                        str(self.config.max_file_bytes),
                    ]
                )

    def prompt(self) -> str:
        if self.command:
            return Template(_PROMPT, undefined=StrictUndefined).render(
                command=self.command, query_example=self.query_example
            )
        if self.config.enabled:
            return "\nrepo_search setup failed; continue locating code with ordinary Bash (rg/grep/find)."
        return ""

    def serialize(self) -> dict:
        return {"config": asdict(self.config), "command": self.command, "setup": self.setup}

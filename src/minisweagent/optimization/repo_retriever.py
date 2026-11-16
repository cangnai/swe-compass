"""Standalone, standard-library-only repository localization, also deployed into task environments."""

from __future__ import annotations

import ast
import fnmatch
import hashlib
import json
import re
import shutil
import subprocess
from dataclasses import asdict, dataclass
from pathlib import Path

METADATA_PREFIX = "REPO_SEARCH_METADATA "
EXCLUDED = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".tox",
    ".mypy_cache",
    ".pytest_cache",
    "build",
    "dist",
    "site-packages",
    ".next",
}
STOP_WORDS = set(
    "a an and are as at be been but by can could def class else for from has have if in is it its "
    "no not of on or return self should that the their then there these this to was we when where which "
    "while will with would you error exception traceback file line test tests py true false none".split()
)


@dataclass
class Symbol:
    name: str
    kind: str
    start: int
    end: int


@dataclass
class SearchResult:
    path: str
    start: int
    end: int
    symbol: Symbol | None
    score: float
    reasons: list[str]
    snippet: str
    revision: str


def query_terms(query: str) -> list[str]:
    words = re.findall(r"[^\W\d]\w*(?:\.\w+)*", query, re.UNICODE)
    code = [word for word in words if "_" in word or "." in word or any(c.isupper() for c in word[1:])]
    parts = re.sub(r"([a-z])([A-Z])", r"\1 \2", " ".join(words)).replace("_", " ").replace(".", " ")
    return list(
        dict.fromkeys(
            word.lower() for word in code + words + parts.split() if len(word) > 1 and word.lower() not in STOP_WORDS
        )
    )[:32]


def symbols(source: str, path: str) -> tuple[list[Symbol], str]:
    if Path(path).suffix != ".py":
        return [], "text fallback (non-Python)"
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, RecursionError):
        return [], "text fallback (Python AST unavailable)"
    result = []
    pending = [(tree, [])]
    while pending:
        node, parents = pending.pop()
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):  # noqa: UP038
            parents = parents + [node.name]
            start = min([node.lineno] + [d.lineno for d in node.decorator_list])
            result.append(Symbol(".".join(parents), type(node).__name__, start, node.end_lineno or node.lineno))
        pending.extend((child, parents) for child in ast.iter_child_nodes(node))
    return sorted(result, key=lambda symbol: symbol.start), "Python AST"


class RepoRetriever:
    def __init__(self, root: Path, *, max_file_bytes: int = 1_000_000, candidate_limit: int = 40):
        self.root = root.resolve(strict=True)
        if not self.root.is_dir() or max_file_bytes < 1 or candidate_limit < 1:
            raise ValueError("Expected a repository directory and positive retrieval limits")
        self.max_file_bytes = max_file_bytes
        self.candidate_limit = candidate_limit
        self.warnings: list[str] = []

    def _run(self, args: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(args, cwd=self.root, capture_output=True)

    def _walk(self, directory: Path, rules: list[tuple[Path, str]]) -> list[str]:
        rules = list(rules)
        for name in (".gitignore", ".ignore", ".rgignore"):
            if (directory / name).is_file():
                rules.extend(
                    (directory, line.strip())
                    for line in (directory / name).read_text(errors="replace").splitlines()
                    if line.strip() and not line.startswith("#")
                )
        paths = []
        for path in sorted(directory.iterdir()):
            if path.is_symlink() or path.name in EXCLUDED:
                continue
            ignored = False
            for base, pattern in rules:
                negate = pattern.startswith("!")
                pattern = pattern.lstrip("!")
                if pattern.endswith("/") and not path.is_dir():
                    continue
                pattern = pattern.rstrip("/")
                value = path.relative_to(base).as_posix() if "/" in pattern else path.name
                if fnmatch.fnmatchcase(value, pattern.lstrip("/")):
                    ignored = not negate
            if not ignored:
                paths.extend(self._walk(path, rules) if path.is_dir() else [path.relative_to(self.root).as_posix()])
        return paths

    def _files(self) -> list[str]:
        if shutil.which("rg"):
            result = self._run(["rg", "--files", "--hidden", "-0", *self._globs(), "."])
            if result.returncode in (0, 1):
                return self._safe_paths(result.stdout)
            self.warnings.append("ripgrep inventory failed; using Git/filesystem inventory")
        if shutil.which("git"):
            result = self._run(["git", "ls-files", "-z", "--cached", "--others", "--exclude-standard"])
            if result.returncode == 0:
                return self._safe_paths(result.stdout)
        self.warnings.append("Filesystem inventory uses basic ignore patterns; Git/ripgrep is recommended")
        return self._walk(self.root, [])

    def _safe_paths(self, output: bytes) -> list[str]:
        paths = set()
        for value in output.decode("utf-8", errors="replace").split("\0"):
            path = self.root / value
            if not value or path.is_symlink() or any(p in EXCLUDED for p in Path(value).parts):
                continue
            if path.is_file() and self.root in path.resolve().parents:
                paths.add(path.relative_to(self.root).as_posix())
        return sorted(paths)

    def _globs(self) -> list[str]:
        return [arg for name in sorted(EXCLUDED) for arg in ("-g", f"!**/{name}/**")]

    def _matching_files(self, terms: list[str], files: list[str]) -> set[str]:
        if not terms or not shutil.which("rg"):
            return set(files)
        result = self._run(
            [
                "rg",
                "-l",
                "-0",
                "-i",
                "-F",
                "--hidden",
                "--max-filesize",
                str(self.max_file_bytes),
                *self._globs(),
                *[arg for term in terms for arg in ("-e", term)],
                "--",
                ".",
            ]
        )
        if result.returncode in (0, 1):
            return set(self._safe_paths(result.stdout))
        self.warnings.append("ripgrep text search failed; using direct text search")
        return set(files)

    def _read(self, path: str) -> str | None:
        try:
            if (self.root / path).stat().st_size > self.max_file_bytes:
                return None
            data = (self.root / path).read_bytes()
        except OSError:
            self.warnings.append(f"Skipped unreadable or removed file: {path}")
            return None
        return None if b"\0" in data else data.decode("utf-8", errors="replace")

    def search(self, query: str, *, top_k: int = 5, snippet_lines: int = 24) -> list[SearchResult]:
        if not query.strip() or not 1 <= top_k <= 50 or not 1 <= snippet_lines <= 100:
            raise ValueError("Provide a nonempty query, top_k in 1..50 and snippet_lines in 1..100")
        self.warnings = []
        terms = query_terms(query)
        normalized = query.replace("\\", "/").lower()
        traces = []
        for match in re.finditer(r"""File ["']([^"']+)["'], line (\d+)|([\w./-]+\.[\w]+):(\d+)""", query):
            traces.append(((match[1] or match[3]).replace("\\", "/").lower(), int(match[2] or match[4])))
        files = self._files()
        clues: dict[str, list[str]] = {}
        for path in files:
            lower = path.lower()
            module = Path(lower).with_suffix("").as_posix().replace("/", ".")
            reasons = []
            if lower in normalized or re.search(r"(?<![\w.])" + re.escape(module) + r"(?![\w.])", normalized):
                reasons.append("path/module match")
            elif Path(lower).name in normalized:
                reasons.append("filename match")
            if any(t == lower or t.endswith("/" + lower) for t, _ in traces):
                reasons.append("traceback location")
            if not reasons and any(term in lower for term in terms):
                reasons.append("path keyword match")
            if reasons:
                clues[path] = reasons
        references: set[str] = set()
        for path in clues:
            if "test" in Path(path).name and (source := self._read(path)):
                references.update(
                    name.lower()
                    for name in re.findall(r"\b([A-Za-z_]\w*)\s*\(", source)
                    if name.lower() not in STOP_WORDS and not name.startswith("test_")
                )
        references = set(sorted(references)[:32])
        matching = self._matching_files(terms + sorted(references), files)
        candidates = []
        for path in sorted(matching | clues.keys()):
            if (source := self._read(path)) is None:
                continue
            lower = source.lower()
            hits = [term for term in terms if term in lower]
            refs = sorted(name for name in references if re.search(r"\b" + re.escape(name) + r"\b", lower))
            path_terms = [term for term in terms if term in path.lower()]
            reasons = list(clues.get(path, []))
            score = 20 * ("traceback location" in reasons) + 12 * ("path/module match" in reasons)
            score += 8 * ("filename match" in reasons) + min(len(path_terms), 4) + min(len(hits), 6)
            if hits:
                reasons.append("keywords: " + ", ".join(hits[:8]))
            if path_terms:
                reasons.append("path keywords: " + ", ".join(path_terms[:4]))
            if refs:
                score += 4
                reasons.append("test references: " + ", ".join(refs[:6]))
            names = re.findall(r"^\s*(?:async\s+)?(?:def|class)\s+(\w+)", source, re.MULTILINE)
            definitions = [name for name in names if name.lower() in terms]
            if definitions:
                score += 8
                reasons.append("definition keywords: " + ", ".join(definitions[:6]))
            if score:
                candidates.append((score, path, reasons))
        candidates.sort(key=lambda c: (-c[0], c[1]))
        if len(candidates) > self.candidate_limit:
            self.warnings.append(
                f"Symbol localization limited to {self.candidate_limit} candidate files; refine the query if needed"
            )
        results = []
        for file_score, path, reasons in candidates[: self.candidate_limit]:
            if (source := self._read(path)) is None:
                continue
            definitions, parser = symbols(source, path)
            lines = source.splitlines()
            anchors: dict[int, list[str]] = {}
            for i, line in enumerate(lines, 1):
                hits = [term for term in terms if term in line.lower()]
                if hits:
                    anchors[i] = ["text: " + ", ".join(hits[:4])]
            for trace, line in traces:
                if (trace == path.lower() or trace.endswith("/" + path.lower())) and 1 <= line <= len(lines):
                    anchors.setdefault(line, []).append("traceback line")
            for symbol in definitions:
                name = symbol.name.lower()
                if name in terms or name.rsplit(".", 1)[-1] in terms:
                    anchors.setdefault(symbol.start, []).append("exact symbol: " + symbol.name)
                if name.rsplit(".", 1)[-1] in references:
                    anchors.setdefault(symbol.start, []).append("test-referenced definition: " + symbol.name)
            if not anchors and lines:
                anchors[1] = ["file context"]
            ranked = []
            for line, evidence in anchors.items():
                enclosing = [symbol for symbol in definitions if symbol.start <= line <= symbol.end]
                symbol = min(enclosing, key=lambda s: s.end - s.start) if enclosing else None
                evidence = list(evidence)
                evidence.extend(
                    "exact symbol: " + enclosing_symbol.name
                    for enclosing_symbol in enclosing
                    if enclosing_symbol.name.lower() in terms
                    or enclosing_symbol.name.rsplit(".", 1)[-1].lower() in terms
                )
                score = file_score + 20 * ("traceback line" in evidence)
                score += 12 * any(e.startswith("exact symbol:") for e in evidence)
                score += 6 * bool(symbol and "." in symbol.name and symbol.name.lower() in terms)
                score += 5 * any(e.startswith("test-referenced") for e in evidence)
                ranked.append((score, line, evidence, symbol))
            chosen = []
            revision = hashlib.sha256(source.encode()).hexdigest()
            for score, line, evidence, symbol in sorted(ranked, key=lambda item: (-item[0], item[1])):
                start = max(1, line - min(3, (snippet_lines - 1) // 2))
                if symbol and line - symbol.start < snippet_lines:
                    start = symbol.start
                end = min(len(lines), start + snippet_lines - 1)
                if any(start <= old.end + 3 and old.start <= end + 3 for old in chosen):
                    continue
                chosen.append(
                    SearchResult(
                        path,
                        start,
                        end,
                        symbol,
                        score,
                        list(dict.fromkeys(reasons + evidence + [parser])),
                        "\n".join(f"{i}: {lines[i - 1][:400]}" for i in range(start, end + 1)),
                        revision,
                    )
                )
                if len(chosen) >= top_k:
                    break
            results.extend(chosen)
        return sorted(results, key=lambda r: (-r.score, r.path, r.start))[:top_k]


def format_results(query: str, retriever: RepoRetriever, results: list[SearchResult]) -> str:
    lines = ["repo_search: candidate locations; relevance and bug cause are unverified."]
    for result in results:
        location = f"{result.path}:{result.start}-{result.end}"
        if result.symbol:
            location += f" | {result.symbol.name} ({result.symbol.start}-{result.symbol.end})"
        lines.extend(
            [f"\n{location} | score={result.score:g}", "Reasons: " + "; ".join(result.reasons), result.snippet]
        )
    if not results:
        lines.append("No matches. Refine the query or use ordinary Bash search.")
    lines.extend("Warning: " + warning for warning in retriever.warnings)
    metadata = {
        "version": 1,
        "root": str(retriever.root),
        "query": query,
        "results": [{k: v for k, v in asdict(result).items() if k != "snippet"} for result in results],
    }
    lines.append(METADATA_PREFIX + json.dumps(metadata, ensure_ascii=True))
    return "\n".join(lines)


if __name__ == "__main__":
    # The task image need not have mini-SWE-agent, Typer, or any third-party package installed.
    import argparse

    parser = argparse.ArgumentParser(description="Hierarchical repository search with Python AST and text fallback")
    parser.add_argument("query")
    parser.add_argument("--root", type=Path, default=Path.cwd())
    parser.add_argument("--top-k", type=int, default=5)
    parser.add_argument("--snippet-lines", type=int, default=24)
    parser.add_argument("--candidate-limit", type=int, default=40)
    parser.add_argument("--max-file-bytes", type=int, default=1_000_000)
    args = parser.parse_args()
    retriever = RepoRetriever(args.root, max_file_bytes=args.max_file_bytes, candidate_limit=args.candidate_limit)
    print(
        format_results(
            args.query, retriever, retriever.search(args.query, top_k=args.top_k, snippet_lines=args.snippet_lines)
        )
    )

"""Search a local checkout without running an agent: python -m minisweagent.run.utilities.repo_search."""

from pathlib import Path

import typer

from minisweagent.optimization.repo_retriever import RepoRetriever, format_results


def main(
    query: str,
    root: Path = typer.Option(Path.cwd(), "--root", exists=True, file_okay=False),
    top_k: int = typer.Option(5, min=1, max=50),
    snippet_lines: int = typer.Option(24, min=1, max=100),
) -> None:
    retriever = RepoRetriever(root)
    print(format_results(query, retriever, retriever.search(query, top_k=top_k, snippet_lines=snippet_lines)))


if __name__ == "__main__":
    typer.run(main)

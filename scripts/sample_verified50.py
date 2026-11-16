"""Create the fixed SWE-bench Verified subset without running an agent or evaluator."""

import hashlib
import json
import platform
import random
from collections import Counter
from dataclasses import dataclass
from pathlib import Path

import typer
from jinja2 import StrictUndefined, Template

ROOT = Path(__file__).resolve().parents[1]
QUOTAS = {"<15 min fix": 19, "15 min - 1 hour": 26, "1-4 hours": 4, ">4 hours": 1}
OUTPUTS = (
    "swebench_verified_50.jsonl",
    "verified50_ids.txt",
    "verified50_sampling_report.md",
    "verified50_sampling_meta.json",
)


@dataclass
class SamplingConfig:
    input_path: Path = ROOT / "dataset/swebench_verified_500.jsonl"
    output_dir: Path = ROOT / "dataset"
    seed: int = 42


@dataclass
class Edge:
    target: int
    reverse: int
    capacity: int
    cost: int


def allocate(rows: list[dict], quotas: dict[str, int]) -> Counter:
    """Minimize repo squared error first, then joint squared error, with exact difficulty quotas."""
    counts = Counter((row["difficulty"], row["repo"]) for row in rows)
    difficulties = Counter(row["difficulty"] for row in rows)
    if set(difficulties) - quotas.keys() or any(q < 0 or q > difficulties[d] for d, q in quotas.items()):
        raise ValueError("Unknown difficulty labels or insufficient records for the difficulty quotas")
    repos = sorted({row["repo"] for row in rows})
    size, population = sum(quotas.values()), len(rows)
    if not population:
        raise ValueError("The input dataset is empty")
    difficulty_nodes = {d: i + 1 for i, d in enumerate(quotas)}
    repo_nodes = {r: len(quotas) + i + 1 for i, r in enumerate(repos)}
    sink = len(quotas) + len(repos) + 1
    graph: list[list[Edge]] = [[] for _ in range(sink + 1)]

    def add_edge(start: int, end: int, capacity: int, cost: int) -> Edge:
        forward = Edge(end, len(graph[end]), capacity, cost)
        graph[start].append(forward)
        graph[end].append(Edge(start, len(graph[start]) - 1, 0, -cost))
        return forward

    # Joint squared error is at most 2 * (population * size) ** 2; repo error takes priority.
    priority = 2 * (population * size) ** 2 + 1
    cells: dict[tuple[str, str], list[Edge]] = {}
    for difficulty, quota in quotas.items():
        add_edge(0, difficulty_nodes[difficulty], quota, 0)
        for repo in repos:
            count = counts[difficulty, repo]
            cells[difficulty, repo] = [
                add_edge(
                    difficulty_nodes[difficulty],
                    repo_nodes[repo],
                    1,
                    population * (2 * k - 1) - 2 * size * count,
                )
                for k in range(1, min(count, quota) + 1)
            ]
    for repo, count in sorted(Counter(row["repo"] for row in rows).items()):
        for k in range(1, min(count, size) + 1):
            add_edge(repo_nodes[repo], sink, 1, priority * (population * (2 * k - 1) - 2 * size * count))

    for _ in range(size):
        distances: list[int | None] = [None] * len(graph)
        previous: list[tuple[int, int] | None] = [None] * len(graph)
        distances[0] = 0
        # Bellman-Ford handles negative marginal costs and residual edges without extra dependencies.
        for _ in range(len(graph) - 1):
            changed = False
            for node, edges in enumerate(graph):
                if distances[node] is None:
                    continue
                for index, edge in enumerate(edges):
                    candidate = distances[node] + edge.cost
                    if edge.capacity and (distances[edge.target] is None or candidate < distances[edge.target]):
                        distances[edge.target] = candidate
                        previous[edge.target] = (node, index)
                        changed = True
            if not changed:
                break
        if previous[sink] is None:
            raise ValueError("Cannot satisfy the difficulty quotas")
        node = sink
        while node:
            parent, index = previous[node]
            edge = graph[parent][index]
            edge.capacity -= 1
            graph[node][edge.reverse].capacity += 1
            node = parent
    return Counter({cell: sum(1 - edge.capacity for edge in edges) for cell, edges in cells.items()})


def select(rows: list[dict], allocation: Counter, seed: int) -> list[dict]:
    rng = random.Random(seed)
    groups: dict[tuple[str, str], list[dict]] = {}
    for row in sorted(rows, key=lambda row: row["instance_id"]):
        groups.setdefault((row["difficulty"], row["repo"]), []).append(row)
    return sorted(
        [row for cell, count in allocation.items() if count for row in rng.sample(groups[cell], count)],
        key=lambda row: row["instance_id"],
    )


def distribution(rows: list[dict]) -> dict:
    return {
        "difficulty": dict(Counter(row["difficulty"] for row in rows)),
        "repo": dict(sorted(Counter(row["repo"] for row in rows).items())),
        "joint": {
            d: dict(sorted(Counter(row["repo"] for row in rows if row["difficulty"] == d).items())) for d in QUOTAS
        },
    }


def generate(config: SamplingConfig) -> dict:
    source = config.input_path.read_bytes()
    lines = source.splitlines()
    rows = [json.loads(line) for line in lines]
    if len(rows) != 500:
        raise ValueError(f"Expected 500 input records, got {len(rows)}")
    for row in rows:
        if any(not isinstance(row.get(key), str) or not row[key] for key in ("instance_id", "repo", "difficulty")):
            raise ValueError("Every record needs nonempty instance_id, repo and difficulty strings")
        if not {"patch", "test_patch"} <= row.keys():
            raise ValueError(f"Missing reference patch fields: {row['instance_id']}")
    originals = {row["instance_id"]: line for row, line in zip(rows, lines)}
    if len(originals) != len(rows):
        raise ValueError("Duplicate instance_id in the input dataset")
    if config.input_path.resolve() in {(config.output_dir / name).resolve() for name in OUTPUTS}:
        raise ValueError("The input path must not overlap any output file")
    allocation = allocate(rows, QUOTAS)
    selected = select(rows, allocation, config.seed)
    ids = [row["instance_id"] for row in selected]
    sample = b"\n".join(originals[iid] for iid in ids) + b"\n"
    checks = {
        "exactly_50_unique_ids": len(ids) == len(set(ids)) == 50,
        "exact_difficulty_quotas": Counter(row["difficulty"] for row in selected) == QUOTAS,
        "all_records_from_source_with_all_fields": [json.loads(line) for line in sample.splitlines()] == selected,
        "original_json_lines_preserved": all(line in lines for line in sample.splitlines()),
        "repeat_selection_identical": selected
        == select(list(reversed(rows)), allocate(list(reversed(rows)), QUOTAS), config.seed),
        "input_unchanged": config.input_path.read_bytes() == source,
    }
    if not all(checks.values()):
        raise ValueError(f"Sampling validation failed: {checks}")
    ids_text = "\n".join(ids) + "\n"
    ids_path = config.output_dir / OUTPUTS[1]
    if ids_path.exists() and ids_path.read_text(encoding="utf-8") != ids_text:
        raise ValueError(f"Refusing to replace the fixed sample: {ids_path}")
    source_stats, sample_stats = distribution(rows), distribution(selected)
    repo_counts = source_stats["repo"]
    rounded = {repo: count // 10 for repo, count in repo_counts.items()}
    for repo in sorted(repo_counts, key=lambda repo: (-(repo_counts[repo] % 10), repo))[: 50 - sum(rounded.values())]:
        rounded[repo] += 1

    def repo_error(counts: dict[str, int]) -> int:
        return sum((10 * counts.get(repo, 0) - count) ** 2 for repo, count in repo_counts.items())

    meta = {
        "method": "difficulty_constrained_min_cost_flow_v1",
        "objective": ["minimize_repo_squared_count_error", "minimize_joint_squared_count_error"],
        "expected_counts": "source_count * sample_size / source_size",
        "tie_break": "difficulty quota order, then repo ascending; first shortest path on equal cost",
        "seed": config.seed,
        "sort_key": "instance_id",
        "rng": "random.Random.sample within each difficulty/repo cell",
        "python_version": platform.python_version(),
        "input": {
            "file": config.input_path.name,
            "sha256": hashlib.sha256(source).hexdigest(),
            "bytes": len(source),
            "records": len(rows),
        },
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "template_sha256": hashlib.sha256(Path(__file__).with_suffix(".md.j2").read_bytes()).hexdigest(),
        "sample_size": len(selected),
        "difficulty_quotas": QUOTAS,
        "source_distribution": source_stats,
        "sample_distribution": sample_stats,
        "repo_balance": {
            "squared_count_error": repo_error(sample_stats["repo"]) / 100,
            "unconstrained_rounding_lower_bound": repo_error(rounded) / 100,
            "attains_unconstrained_lower_bound": repo_error(sample_stats["repo"]) == repo_error(rounded),
            "max_absolute_count_deviation": max(
                abs(10 * sample_stats["repo"].get(repo, 0) - count) for repo, count in repo_counts.items()
            )
            / 10,
        },
        "instance_ids": ids,
        "validation": checks,
        "outputs": {
            OUTPUTS[0]: hashlib.sha256(sample).hexdigest(),
            OUTPUTS[1]: hashlib.sha256(ids_text.encode()).hexdigest(),
        },
    }
    report = (
        Template(Path(__file__).with_suffix(".md.j2").read_text(encoding="utf-8"), undefined=StrictUndefined).render(
            meta=meta,
            source=source_stats,
            sample=sample_stats,
            quotas=QUOTAS,
            repos=sorted(source_stats["repo"]),
        )
        + "\n"
    )
    config.output_dir.mkdir(parents=True, exist_ok=True)
    (config.output_dir / OUTPUTS[0]).write_bytes(sample)
    ids_path.write_bytes(ids_text.encode("utf-8"))
    (config.output_dir / OUTPUTS[2]).write_bytes(report.encode("utf-8"))
    (config.output_dir / OUTPUTS[3]).write_bytes(
        (json.dumps(meta, ensure_ascii=False, indent=2) + "\n").encode("utf-8")
    )
    if config.input_path.read_bytes() != source:
        raise ValueError("The source dataset changed during sampling")
    return meta


def main(
    input_path: Path = typer.Option(SamplingConfig.input_path, "--input", exists=True, dir_okay=False),
    output_dir: Path = typer.Option(SamplingConfig.output_dir, "--output-dir"),
    seed: int = typer.Option(42, "--seed"),
) -> None:
    meta = generate(SamplingConfig(input_path, output_dir, seed))
    typer.echo(f"Saved {meta['sample_size']} fixed tasks to {output_dir}; all validations passed.")


if __name__ == "__main__":
    typer.run(main)

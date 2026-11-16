"""Extend the fixed Verified 50 to 100 tasks, preserving all original sampling artifacts."""

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
DIFFICULTIES = ("<15 min fix", "15 min - 1 hour", "1-4 hours", ">4 hours")
OUTPUTS = (
    "swebench_verified_100.jsonl",
    "verified100_ids.txt",
    "verified100_sampling_report.md",
    "verified100_sampling_meta.json",
)


@dataclass
class SamplingConfig:
    input_path: Path = ROOT / "dataset/swebench_verified_500.jsonl"
    retained_path: Path = ROOT / "dataset/swebench_verified_50.jsonl"
    retained_ids: Path = ROOT / "dataset/verified50_ids.txt"
    output_dir: Path = ROOT / "dataset"
    seed: int = 42


def proportional_quotas(counts: Counter, size: int) -> dict[str, int]:
    population = sum(counts.values())
    quotas = {key: count * size // population for key, count in counts.items()}
    for key in sorted(counts, key=lambda key: (-(counts[key] * size % population), key))[: size - sum(quotas.values())]:
        quotas[key] += 1
    return quotas


@dataclass
class Edge:
    target: int
    reverse: int
    capacity: int
    cost: int


def allocate(rows: list[dict], quotas: dict[str, int], retained: list[dict]) -> Counter:
    """Minimize repo squared error first, then joint squared error, with exact difficulty quotas."""
    counts = Counter((row["difficulty"], row["repo"]) for row in rows)
    difficulties = Counter(row["difficulty"] for row in rows)
    if set(difficulties) - quotas.keys() or any(q < 0 or q > difficulties[d] for d, q in quotas.items()):
        raise ValueError("Unknown difficulty labels or insufficient records for the difficulty quotas")
    fixed = Counter((row["difficulty"], row["repo"]) for row in retained)
    fixed_difficulty = Counter(row["difficulty"] for row in retained)
    fixed_repo = Counter(row["repo"] for row in retained)
    if any(fixed_difficulty[d] > q for d, q in quotas.items()) or fixed - counts:
        raise ValueError("Retained records exceed the quotas or source cell capacity")
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
        add_edge(0, difficulty_nodes[difficulty], quota - fixed_difficulty[difficulty], 0)
        for repo in repos:
            count = counts[difficulty, repo]
            cells[difficulty, repo] = [
                add_edge(
                    difficulty_nodes[difficulty],
                    repo_nodes[repo],
                    1,
                    population * (2 * k - 1) - 2 * size * count,
                )
                for k in range(fixed[difficulty, repo] + 1, min(count, quota) + 1)
            ]
    for repo, count in sorted(Counter(row["repo"] for row in rows).items()):
        for k in range(fixed_repo[repo] + 1, min(count, size) + 1):
            add_edge(repo_nodes[repo], sink, 1, priority * (population * (2 * k - 1) - 2 * size * count))

    for _ in range(size - len(retained)):
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
    return Counter({cell: fixed[cell] + sum(1 - edge.capacity for edge in edges) for cell, edges in cells.items()})


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
            d: dict(sorted(Counter(row["repo"] for row in rows if row["difficulty"] == d).items()))
            for d in DIFFICULTIES
        },
    }


def extend(rows: list[dict], retained: list[dict], quotas: dict[str, int], seed: int) -> list[dict]:
    allocation = allocate(rows, quotas, retained)
    fixed = Counter((row["difficulty"], row["repo"]) for row in retained)
    retained_ids = {row["instance_id"] for row in retained}
    return sorted(
        retained
        + select(
            [row for row in rows if row["instance_id"] not in retained_ids],
            Counter({cell: count - fixed[cell] for cell, count in allocation.items()}),
            seed,
        ),
        key=lambda row: row["instance_id"],
    )


def generate(config: SamplingConfig) -> dict:
    protected = {
        config.input_path,
        config.retained_path,
        config.retained_ids,
        *ROOT.joinpath("dataset").glob("verified50_*"),
        *ROOT.joinpath("dataset").glob("swebench_verified_50.*"),
        *ROOT.joinpath("scripts").glob("sample_verified50.*"),
    }
    snapshots = {path.resolve(): path.read_bytes() for path in protected}
    if snapshots.keys() & {(config.output_dir / name).resolve() for name in OUTPUTS}:
        raise ValueError("Output paths must not overlap protected inputs")
    source = snapshots[config.input_path.resolve()]
    lines = source.splitlines()
    rows = [json.loads(line) for line in lines]
    retained = [json.loads(line) for line in snapshots[config.retained_path.resolve()].splitlines()]
    ids50 = snapshots[config.retained_ids.resolve()].decode("utf-8").splitlines()
    if len(rows) != 500:
        raise ValueError(f"Expected 500 input records, got {len(rows)}")
    for row in rows:
        if any(not isinstance(row.get(key), str) or not row[key] for key in ("instance_id", "repo", "difficulty")):
            raise ValueError("Every record needs nonempty instance_id, repo and difficulty strings")
        if not {"patch", "test_patch"} <= row.keys():
            raise ValueError(f"Missing reference patch fields: {row['instance_id']}")
    originals = {row["instance_id"]: row for row in rows}
    raw_lines = {row["instance_id"]: line for row, line in zip(rows, lines)}
    if len(originals) != 500:
        raise ValueError("Duplicate instance_id in the input dataset")
    if len(retained) != len(ids50) or len(ids50) != len(set(ids50)) or len(ids50) != 50:
        raise ValueError("Expected exactly 50 unique retained tasks and IDs")
    if sorted(ids50) != sorted(row["instance_id"] for row in retained):
        raise ValueError("Retained ID list does not match the retained JSONL")
    if any(originals.get(row["instance_id"]) != row for row in retained):
        raise ValueError("Retained records must match the source exactly, including every field")
    difficulty = Counter(row["difficulty"] for row in rows)
    if set(difficulty) != set(DIFFICULTIES):
        raise ValueError("Unexpected source difficulty labels")
    quotas = proportional_quotas(Counter({d: difficulty[d] for d in DIFFICULTIES}), 100)
    selected = extend(rows, retained, quotas, config.seed)
    ids = [row["instance_id"] for row in selected]
    added = [row for row in selected if row["instance_id"] not in set(ids50)]
    sample = b"\n".join(raw_lines[iid] for iid in ids) + b"\n"
    checks = {
        "exactly_100_unique_ids": len(ids) == len(set(ids)) == 100,
        "all_original_50_retained": set(ids50) <= set(ids),
        "exactly_50_new_tasks": len(added) == 50,
        "exact_difficulty_quotas": Counter(row["difficulty"] for row in selected) == quotas,
        "all_records_from_source_with_all_fields": all(row == originals[row["instance_id"]] for row in selected),
        "original_json_lines_preserved": all(line in lines for line in sample.splitlines()),
        "repeat_selection_identical": selected
        == extend(list(reversed(rows)), list(reversed(retained)), quotas, config.seed),
        "protected_inputs_unchanged": all(path.read_bytes() == data for path, data in snapshots.items()),
    }
    if not all(checks.values()):
        raise ValueError(f"Sampling validation failed: {checks}")
    ids_text = "\n".join(ids) + "\n"
    ids_path = config.output_dir / OUTPUTS[1]
    if ids_path.exists() and ids_path.read_text(encoding="utf-8") != ids_text:
        raise ValueError(f"Refusing to replace the fixed sample: {ids_path}")
    source_stats, sample_stats = distribution(rows), distribution(selected)
    repo_counts = source_stats["repo"]
    rounded = proportional_quotas(Counter(repo_counts), 100)

    def repo_error(counts: dict[str, int]) -> float:
        return sum((5 * counts.get(repo, 0) - count) ** 2 for repo, count in repo_counts.items()) / 25

    meta = {
        "method": "retained_difficulty_constrained_min_cost_flow_v1",
        "sample_size": 100,
        "retained_size": len(retained),
        "added_size": len(added),
        "seed": config.seed,
        "sort_key": "instance_id",
        "rng": "one random.Random(seed), sequential sample calls within sorted difficulty/repo cells",
        "python_version": platform.python_version(),
        "quota_method": "largest remainder on source difficulty counts; ties by label ascending",
        "difficulty_quotas": quotas,
        "expected_counts": "source_count * 100 / 500 (full source, not remaining pool)",
        "objective": [
            "retain_all_original_50",
            "exact_difficulty_quotas",
            "minimize_repo_squared_count_error",
            "minimize_joint_squared_count_error",
        ],
        "tie_break": "difficulty order, then repo ascending; first shortest path on equal cost",
        "input": {
            "file": config.input_path.name,
            "records": len(rows),
            "bytes": len(source),
            "sha256": hashlib.sha256(source).hexdigest(),
        },
        "retained_inputs": [
            {"file": path.name, "sha256": hashlib.sha256(snapshots[path.resolve()]).hexdigest()}
            for path in (config.retained_path, config.retained_ids)
        ],
        "protected_file_sha256": {
            path.as_posix(): hashlib.sha256(data).hexdigest() for path, data in sorted(snapshots.items())
        },
        "script_sha256": hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        "template_sha256": hashlib.sha256(Path(__file__).with_suffix(".md.j2").read_bytes()).hexdigest(),
        "source_distribution": source_stats,
        "retained_distribution": distribution(retained),
        "added_distribution": distribution(added),
        "sample_distribution": sample_stats,
        "repo_balance": {
            "squared_count_error": repo_error(sample_stats["repo"]),
            "unconstrained_rounding_lower_bound": repo_error(rounded),
            "attains_unconstrained_lower_bound": repo_error(sample_stats["repo"]) == repo_error(rounded),
            "max_absolute_count_deviation": max(
                abs(5 * sample_stats["repo"].get(repo, 0) - count) for repo, count in repo_counts.items()
            )
            / 5,
        },
        "instance_ids": ids,
        "retained_instance_ids": sorted(ids50),
        "added_instance_ids": [row["instance_id"] for row in added],
        "validation": checks,
        "outputs": {
            OUTPUTS[0]: hashlib.sha256(sample).hexdigest(),
            OUTPUTS[1]: hashlib.sha256(ids_text.encode()).hexdigest(),
        },
    }
    report = (
        Template(Path(__file__).with_suffix(".md.j2").read_text(encoding="utf-8"), undefined=StrictUndefined).render(
            meta=meta,
            difficulties=DIFFICULTIES,
            repos=sorted(repo_counts),
            source=source_stats,
            retained=meta["retained_distribution"],
            added=meta["added_distribution"],
            sample=sample_stats,
        )
        + "\n"
    )
    config.output_dir.mkdir(parents=True, exist_ok=True)
    for name, data in zip(
        OUTPUTS,
        (
            sample,
            ids_text.encode("utf-8"),
            report.encode("utf-8"),
            (json.dumps(meta, ensure_ascii=False, indent=2) + "\n").encode("utf-8"),
        ),
    ):
        (config.output_dir / name).write_bytes(data)
    if any(path.read_bytes() != data for path, data in snapshots.items()):
        raise ValueError("Protected inputs changed during sampling")
    return meta


def main(
    input_path: Path = typer.Option(SamplingConfig.input_path, "--input", exists=True, dir_okay=False),
    retained_path: Path = typer.Option(SamplingConfig.retained_path, "--retained-jsonl", exists=True, dir_okay=False),
    retained_ids: Path = typer.Option(SamplingConfig.retained_ids, "--retained-ids", exists=True, dir_okay=False),
    output_dir: Path = typer.Option(SamplingConfig.output_dir, "--output-dir"),
    seed: int = typer.Option(42, "--seed"),
) -> None:
    meta = generate(SamplingConfig(input_path, retained_path, retained_ids, output_dir, seed))
    typer.echo(
        f"Saved {meta['sample_size']} tasks ({meta['retained_size']} retained + {meta['added_size']} new) to {output_dir}; all validations passed."
    )


if __name__ == "__main__":
    typer.run(main)

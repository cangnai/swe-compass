import hashlib
import itertools
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from scripts.sample_verified100 import OUTPUTS, ROOT, SamplingConfig, allocate, generate, proportional_quotas


@pytest.mark.parametrize(
    ("capacities", "quotas", "fixed"),
    [
        (((3, 1, 0), (1, 2, 3)), (2, 3), (1, 0, 0, 0, 0, 1)),
        (((5, 0, 0), (0, 1, 1)), (1, 2), (1, 0, 0, 0, 0, 0)),
        (((1, 1, 1), (1, 1, 1)), (1, 2), (0, 1, 0, 0, 0, 1)),
        (((1, 1, 1), (1, 1, 1)), (1, 2), (0, 1, 0, 0, 1, 1)),
    ],
)
def test_retained_allocation_matches_exhaustive_optimum(capacities: tuple, quotas: tuple, fixed: tuple) -> None:
    cells = list(itertools.product(range(2), range(3)))
    counts = [capacities[d][r] for d, r in cells]
    rows = [{"difficulty": str(d), "repo": str(r)} for (d, r), count in zip(cells, counts) for _ in range(count)]
    retained = [{"difficulty": str(d), "repo": str(r)} for (d, r), count in zip(cells, fixed) for _ in range(count)]

    def score(values: tuple) -> tuple[int, int]:
        return (
            sum(
                (
                    len(rows) * sum(values[d * 3 + r] for d in range(2))
                    - sum(quotas) * sum(capacities[d][r] for d in range(2))
                )
                ** 2
                for r in range(3)
            ),
            sum((len(rows) * value - sum(quotas) * count) ** 2 for value, count in zip(values, counts)),
        )

    allocation = allocate(rows, {str(d): q for d, q in enumerate(quotas)}, retained)
    chosen = tuple(allocation[str(d), str(r)] for d, r in cells)
    assert all(sum(chosen[d * 3 : d * 3 + 3]) == quota for d, quota in enumerate(quotas))
    assert all(lower <= value <= upper for lower, value, upper in zip(fixed, chosen, counts))
    assert score(chosen) == min(
        score(values)
        for values in itertools.product(*(range(lower, upper + 1) for lower, upper in zip(fixed, counts)))
        if all(sum(values[d * 3 : d * 3 + 3]) == quota for d, quota in enumerate(quotas))
    )


@pytest.mark.skipif(
    not all(p.is_file() for p in (SamplingConfig.input_path, SamplingConfig.retained_path, SamplingConfig.retained_ids)),
    reason="Prepare the local SWE-bench source and retained 50-task sample first",
)
def test_cli_nested_sample_integrity_reproduction_and_lock(tmp_path: Path) -> None:
    protected = [
        SamplingConfig.input_path,
        SamplingConfig.retained_path,
        SamplingConfig.retained_ids,
        *ROOT.joinpath("dataset").glob("verified50_*"),
        *ROOT.joinpath("scripts").glob("sample_verified50.*"),
    ]
    originals = {path: path.read_bytes() for path in protected}
    command = [sys.executable, str(ROOT / "scripts/sample_verified100.py"), "--output-dir", str(tmp_path)]
    subprocess.run(command, check=True, capture_output=True, text=True)
    first = {name: (tmp_path / name).read_bytes() for name in OUTPUTS}
    subprocess.run(command, check=True, capture_output=True, text=True)
    assert first == {name: (tmp_path / name).read_bytes() for name in OUTPUTS}
    sample = [json.loads(line) for line in first[OUTPUTS[0]].splitlines()]
    ids = first[OUTPUTS[1]].decode().splitlines()
    meta = json.loads(first[OUTPUTS[3]])
    ids50 = set(SamplingConfig.retained_ids.read_text(encoding="utf-8").splitlines())
    assert len(sample) == len(set(ids)) == 100
    assert ids50 <= set(ids) and len(set(ids) - ids50) == 50
    assert ids == sorted(row["instance_id"] for row in sample) == meta["instance_ids"]
    assert set(meta["added_instance_ids"]) == set(ids) - ids50
    assert Counter(row["difficulty"] for row in sample) == {
        "<15 min fix": 39,
        "15 min - 1 hour": 52,
        "1-4 hours": 8,
        ">4 hours": 1,
    }
    assert proportional_quotas(Counter(meta["source_distribution"]["difficulty"]), 100) == meta["difficulty_quotas"]
    assert all(line in originals[SamplingConfig.input_path].splitlines() for line in first[OUTPUTS[0]].splitlines())
    assert all(meta["validation"].values())
    assert meta["repo_balance"]["attains_unconstrained_lower_bound"]
    assert meta["repo_balance"]["squared_count_error"] == 1.2
    assert all(meta["outputs"][name] == hashlib.sha256(first[name]).hexdigest() for name in OUTPUTS[:2])
    assert originals == {path: path.read_bytes() for path in protected}
    (tmp_path / OUTPUTS[1]).write_text("previous-fixed-task\n", encoding="utf-8")
    locked = {name: (tmp_path / name).read_bytes() for name in OUTPUTS}
    with pytest.raises(ValueError, match="Refusing to replace the fixed sample"):
        generate(SamplingConfig(output_dir=tmp_path))
    assert locked == {name: (tmp_path / name).read_bytes() for name in OUTPUTS}


@pytest.mark.parametrize(
    ("problem", "message"),
    [
        ("id_mismatch", "Retained ID list does not match"),
        ("duplicate", "50 unique retained tasks"),
        ("changed_patch", "match the source exactly"),
        ("over_quota", "Retained records exceed the quotas"),
    ],
)
@pytest.mark.skipif(
    not all(p.is_file() for p in (SamplingConfig.input_path, SamplingConfig.retained_path, SamplingConfig.retained_ids)),
    reason="Prepare the local SWE-bench source and retained 50-task sample first",
)
def test_invalid_retained_sample_fails_without_outputs(tmp_path: Path, problem: str, message: str) -> None:
    rows = [json.loads(line) for line in SamplingConfig.retained_path.read_bytes().splitlines()]
    ids = [row["instance_id"] for row in rows]
    if problem == "id_mismatch":
        ids[0] = "unknown-task"
    elif problem == "duplicate":
        ids[0] = ids[1]
    elif problem == "changed_patch":
        rows[0]["patch"] = "changed"
    else:
        rows = [
            row
            for row in map(json.loads, SamplingConfig.input_path.read_bytes().splitlines())
            if row["difficulty"] == "<15 min fix"
        ][:50]
        ids = [row["instance_id"] for row in rows]
    retained_path, retained_ids = tmp_path / "retained.jsonl", tmp_path / "retained_ids.txt"
    retained_path.write_text("\n".join(json.dumps(row) for row in rows), encoding="utf-8")
    retained_ids.write_text("\n".join(ids), encoding="utf-8")
    with pytest.raises(ValueError, match=message):
        generate(SamplingConfig(retained_path=retained_path, retained_ids=retained_ids, output_dir=tmp_path / "output"))
    assert not (tmp_path / "output").exists()

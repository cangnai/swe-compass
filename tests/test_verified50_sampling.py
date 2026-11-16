import hashlib
import itertools
import json
import subprocess
import sys
from collections import Counter
from pathlib import Path

import pytest

from scripts.sample_verified50 import OUTPUTS, QUOTAS, ROOT, SamplingConfig, allocate, generate


@pytest.mark.parametrize(
    ("capacities", "quotas"),
    [
        (((3, 1, 0), (1, 2, 3)), (2, 3)),
        (((5, 0, 0), (0, 1, 1)), (1, 2)),
        (((1, 1, 1), (1, 1, 1)), (1, 2)),
        (((0, 2, 0), (2, 0, 1)), (0, 2)),
    ],
)
def test_allocation_matches_exhaustive_optimum(capacities: tuple, quotas: tuple) -> None:
    cells = list(itertools.product(range(2), range(3)))
    counts = [capacities[d][r] for d, r in cells]
    rows = [{"difficulty": str(d), "repo": str(r)} for (d, r), count in zip(cells, counts) for _ in range(count)]

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

    allocation = allocate(rows, {str(d): q for d, q in enumerate(quotas)})
    chosen = tuple(allocation[str(d), str(r)] for d, r in cells)
    assert all(sum(chosen[d * 3 : d * 3 + 3]) == quota for d, quota in enumerate(quotas))
    assert all(0 <= value <= count for value, count in zip(chosen, counts))
    assert score(chosen) == min(
        score(values)
        for values in itertools.product(*(range(count + 1) for count in counts))
        if all(sum(values[d * 3 : d * 3 + 3]) == quota for d, quota in enumerate(quotas))
    )
    assert allocation == allocate(list(reversed(rows)), {str(d): q for d, q in enumerate(quotas)})


@pytest.mark.skipif(not SamplingConfig.input_path.is_file(), reason="Prepare the local SWE-bench source JSONL first")
def test_cli_preserves_records_reproduces_outputs_and_locks_ids(tmp_path: Path) -> None:
    source = SamplingConfig.input_path.read_bytes()
    command = [sys.executable, str(ROOT / "scripts/sample_verified50.py"), "--output-dir", str(tmp_path)]
    subprocess.run(command, check=True, capture_output=True, text=True)
    first_run = {name: (tmp_path / name).read_bytes() for name in OUTPUTS}
    subprocess.run(command, check=True, capture_output=True, text=True)
    assert first_run == {name: (tmp_path / name).read_bytes() for name in OUTPUTS}
    rows = [json.loads(line) for line in first_run[OUTPUTS[0]].splitlines()]
    ids = first_run[OUTPUTS[1]].decode().splitlines()
    meta = json.loads(first_run[OUTPUTS[3]])
    assert len(rows) == len(set(ids)) == 50
    assert ids == sorted(row["instance_id"] for row in rows) == meta["instance_ids"]
    assert Counter(row["difficulty"] for row in rows) == QUOTAS
    assert all(line in source.splitlines() for line in first_run[OUTPUTS[0]].splitlines())
    assert all({"patch", "test_patch"} <= row.keys() for row in rows)
    assert meta["input"]["sha256"] == hashlib.sha256(source).hexdigest()
    assert all(meta["validation"].values())
    assert meta["repo_balance"]["attains_unconstrained_lower_bound"]
    assert meta["repo_balance"]["squared_count_error"] == 1
    assert all(meta["outputs"][name] == hashlib.sha256(first_run[name]).hexdigest() for name in OUTPUTS[:2])
    assert SamplingConfig.input_path.read_bytes() == source
    (tmp_path / OUTPUTS[1]).write_text("previous-fixed-task\n", encoding="utf-8")
    locked_outputs = {name: (tmp_path / name).read_bytes() for name in OUTPUTS}
    with pytest.raises(ValueError, match="Refusing to replace the fixed sample"):
        generate(SamplingConfig(output_dir=tmp_path))
    assert locked_outputs == {name: (tmp_path / name).read_bytes() for name in OUTPUTS}


@pytest.mark.parametrize(
    ("problem", "message"),
    [
        ("duplicate", "Duplicate instance_id"),
        ("missing_patch", "Missing reference patch fields"),
        ("unknown_difficulty", "Unknown difficulty labels"),
        ("insufficient_difficulty", "insufficient records"),
    ],
)
@pytest.mark.skipif(not SamplingConfig.input_path.is_file(), reason="Prepare the local SWE-bench source JSONL first")
def test_bad_input_fails_before_writing(tmp_path: Path, problem: str, message: str) -> None:
    rows = [json.loads(line) for line in SamplingConfig.input_path.read_bytes().splitlines()]
    if problem == "duplicate":
        rows[1]["instance_id"] = rows[0]["instance_id"]
    elif problem == "missing_patch":
        del rows[0]["patch"]
    elif problem == "unknown_difficulty":
        rows[0]["difficulty"] = "unknown"
    else:
        for row in rows:
            if row["difficulty"] == ">4 hours":
                row["difficulty"] = "<15 min fix"
    source = tmp_path / "input.jsonl"
    source.write_text("\n".join(json.dumps(row) for row in rows) + "\n", encoding="utf-8")
    original = source.read_bytes()
    with pytest.raises(ValueError, match=message):
        generate(SamplingConfig(input_path=source, output_dir=tmp_path / "output"))
    assert not (tmp_path / "output").exists()
    assert source.read_bytes() == original

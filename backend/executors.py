from __future__ import annotations

import json
import os
from pathlib import Path

from backend import scoring
from backend.config import artifact_dir, data_dir, data_version, rules_file, time_boundary


def resolve_input(value: str) -> Path:
    root = data_dir().resolve()
    candidate = Path(value).expanduser()
    candidate = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"input_path must be inside DATA_DIR ({root})")
    if not candidate.is_file():
        raise FileNotFoundError(f"input file not found: {candidate}")
    return candidate


def _result(before: dict, after: dict, rules: dict) -> dict:
    return {
        "dimensions": scoring.dimensions_from(before, after, rules),
        "rows_before": before["total"],
        "rows_after": after["total"],
        "dispositions": scoring.dispositions_from(before, after),
        "explanation": scoring.explain(before, after),
        "rule_version": rules["version"],
        "data_version": data_version(),
        "output_data_version": f"{data_version()}-clean",
        "t1": time_boundary("T1"),
        "t2": time_boundary("T2"),
    }


def run_local(task_id: str, input_value: str, rules: dict, progress=None) -> tuple[dict, list]:
    source = resolve_input(input_value)
    mark = progress or (lambda _stage: None)

    mark("score_before")
    metrics = scoring.new_metrics()
    rows: dict[tuple[int, int], dict] = {}
    with source.open(encoding="iso-8859-1") as stream:
        for line in stream:
            info = scoring.add_line(metrics, line, rules)
            row = info["row"]
            if row is None:
                continue
            key = row["user"], row["movie"]
            if key not in rows or row["timestamp"] >= rows[key]["timestamp"]:
                rows[key] = row
    before = scoring.finalize(metrics)

    mark("clean")
    output_dir = artifact_dir() / task_id
    output_dir.mkdir(parents=True, exist_ok=True)
    cleaned = output_dir / "ratings-cleaned.dat"
    delimiter = rules["format"]["delimiter"]
    with cleaned.open("w", encoding="iso-8859-1", newline="\n") as stream:
        for user, movie in sorted(rows):
            row = rows[(user, movie)]
            stream.write(
                delimiter.join((str(user), str(movie), f"{row['rating']:g}", str(row["timestamp"]))) + "\n"
            )

    mark("score_after")
    after_metrics = scoring.new_metrics()
    with cleaned.open(encoding="iso-8859-1") as stream:
        for line in stream:
            scoring.add_line(after_metrics, line, rules)
    after = scoring.finalize(after_metrics)

    result = _result(before, after, rules)
    result_file = output_dir / "report.json"
    result_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    artifacts = [
        {"name": cleaned.name, "path": str(cleaned), "size": cleaned.stat().st_size},
        {"name": result_file.name, "path": str(result_file), "size": result_file.stat().st_size},
    ]
    return result, artifacts


def run_hadoop(task_id: str, input_value: str, rules: dict, progress=None) -> tuple[dict, list]:
    # Imported lazily so importing the executors never requires Hadoop tooling.
    from backend.hadoop_runner import run_hadoop as _run_hadoop

    return _run_hadoop(task_id, input_value, rules, progress=progress)

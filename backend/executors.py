from __future__ import annotations

import json
from pathlib import Path

from backend import scoring
from backend.config import artifact_dir, data_dir, data_version, time_boundary


def resolve_input(value: str) -> Path:
    root = data_dir().resolve()
    candidate = Path(value).expanduser()
    candidate = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"input_path must be inside DATA_DIR ({root})")
    if not candidate.is_file():
        raise FileNotFoundError(f"input file not found: {candidate}")
    return candidate


def result_payload(before: dict, after: dict, rules: dict) -> dict:
    return {
        "dimensions": scoring.dimensions_from(before, after, rules),
        "rows_before": before["total"],
        "rows_after": after["total"],
        "dispositions": scoring.dispositions_from(before, after),
        "explanation": scoring.explain(before, after),
        "repairs": scoring.repair_summary(before),
        "quarantine": scoring.quarantine_summary(before),
        "method": scoring.METHOD,
        "limitations": scoring.LIMITATIONS,
        "unverified": scoring.UNVERIFIED,
        "rule_version": rules["version"],
        "data_version": data_version(),
        "output_data_version": f"{data_version()}-clean",
        "t1": time_boundary("T1"),
        "t2": time_boundary("T2"),
    }


def write_quarantine(path: Path, rows: list[tuple[str, str]]) -> None:
    with path.open("w", encoding="iso-8859-1", newline="\n") as stream:
        for reason, raw in rows:
            stream.write(f"{reason}\t{raw}\n")


def run_local(task_id: str, input_value: str, rules: dict, progress=None) -> tuple[dict, list]:
    source = resolve_input(input_value)
    mark = progress or (lambda _stage: None)

    mark("score_before")
    metrics = scoring.new_metrics()
    rows: dict[tuple[int, int], dict] = {}
    rejected: list[tuple[str, str]] = []
    with source.open(encoding="iso-8859-1") as stream:
        for line in stream:
            info = scoring.add_line(metrics, line, rules)
            if info["bad"] is not None:
                rejected.append((info["bad"], info["raw"]))
                continue
            row = info["row"]
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

    quarantine_file = output_dir / "ratings-quarantine.tsv"
    write_quarantine(quarantine_file, rejected)

    mark("score_after")
    after_metrics = scoring.new_metrics()
    with cleaned.open(encoding="iso-8859-1") as stream:
        for line in stream:
            scoring.add_line(after_metrics, line, rules)
    after = scoring.finalize(after_metrics)

    result = result_payload(before, after, rules)
    report = output_dir / "report.json"
    report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    artifacts = [
        {"name": cleaned.name, "path": str(cleaned), "size": cleaned.stat().st_size},
        {"name": quarantine_file.name, "path": str(quarantine_file), "size": quarantine_file.stat().st_size},
        {"name": report.name, "path": str(report), "size": report.stat().st_size},
    ]
    return result, artifacts


def run_hadoop(task_id: str, input_value: str, rules: dict, progress=None) -> tuple[dict, list]:
    # Imported lazily so importing the executors never requires Hadoop tooling.
    from backend.hadoop_runner import run_hadoop as _run_hadoop

    return _run_hadoop(task_id, input_value, rules, progress=progress)

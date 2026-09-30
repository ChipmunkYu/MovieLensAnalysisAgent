"""Hadoop-backed MovieLens cleaning and five-dimension quality scoring.

This module is deliberately orchestration-only: it never falls back to local
processing when Hadoop is unavailable. Hadoop stages must write JSON artifacts
with the schema documented by ``QualityReport`` before a successful report is
returned.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
import subprocess
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Sequence

DIMENSIONS = ("Accurate", "Complete", "Unique", "Up-to-date", "Consistent")
RULE_VERSION = "ml1m-cleaning-v1.0"
SCORING_VERSION = "ml1m-quality-v1.0"


@dataclass(frozen=True)
class PipelineConfig:
    input_dir: Path
    output_dir: Path
    hadoop_command: str = "hadoop"
    pre_command: tuple[str, ...] = ()
    clean_command: tuple[str, ...] = ()
    post_command: tuple[str, ...] = ()
    dataset_version: str | None = None
    rule_version: str = RULE_VERSION
    scoring_version: str = SCORING_VERSION


def _dataset_version(input_dir: Path) -> str:
    digest = hashlib.sha256()
    for name in ("ratings.dat", "users.dat", "movies.dat"):
        path = input_dir / name
        if not path.is_file():
            raise FileNotFoundError(f"Missing source file: {path}")
        digest.update(name.encode("ascii"))
        digest.update(hashlib.sha256(path.read_bytes()).digest())
    return f"ml1m-{digest.hexdigest()[:16]}"


def _score(numerator: int, denominator: int) -> float:
    return round(100.0 * numerator / denominator, 6) if denominator else 0.0


def score_quality(statistics: dict[str, int]) -> dict[str, float]:
    """Apply the same fixed formulas to pre- and post-cleaning statistics.

    Required fields are counts over the same declared data scope:
    ``total_records``, ``accurate_records``, ``complete_records``,
    ``duplicate_records``, ``up_to_date_records`` and
    ``consistent_records``.
    """

    total = statistics["total_records"]
    return {
        "Accurate": _score(statistics["accurate_records"], total),
        "Complete": _score(statistics["complete_records"], total),
        "Unique": _score(max(total - statistics["duplicate_records"], 0), total),
        "Up-to-date": _score(statistics["up_to_date_records"], total),
        "Consistent": _score(statistics["consistent_records"], total),
    }


def _failure(
    task_id: str,
    dataset_version: str | None,
    rule_version: str,
    scoring_version: str,
    stage: str,
    error: str,
) -> dict[str, Any]:
    return {
        "task_id": task_id,
        "status": "failed",
        "failed_stage": stage,
        "error": error,
        "dataset_version": dataset_version,
        "rule_version": rule_version,
        "scoring_version": scoring_version,
        "T1": None,
        "T2": None,
        "quality": None,
        "data_volume": None,
        "disposition": None,
    }


def _run_stage(command: Sequence[str], stage: str, output_dir: Path) -> tuple[bool, str]:
    if not command:
        return False, f"{stage} command is not configured"
    try:
        result = subprocess.run(
            list(command),
            cwd=output_dir,
            check=False,
            capture_output=True,
            text=True,
        )
    except OSError as exc:
        return False, f"{stage} could not start: {exc}"
    if result.returncode != 0:
        detail = (result.stderr or result.stdout).strip()
        return False, f"{stage} exited with code {result.returncode}: {detail}"
    return True, ""


def _read_json(path: Path, stage: str) -> dict[str, Any]:
    if not path.is_file():
        raise FileNotFoundError(f"{stage} artifact not found: {path}")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError(f"{stage} artifact must contain a JSON object: {path}")
    return value


def run_pipeline(config: PipelineConfig) -> dict[str, Any]:
    """Run pre-score, Hadoop cleaning, and post-score in strict order."""

    task_id = f"ml1m-{uuid.uuid4().hex}"
    try:
        dataset_version = config.dataset_version or _dataset_version(config.input_dir)
    except (FileNotFoundError, OSError) as exc:
        report = _failure(
            task_id, None, config.rule_version, config.scoring_version, "input_validation", str(exc)
        )
        config.output_dir.mkdir(parents=True, exist_ok=True)
        _write_report(config.output_dir, report)
        return report

    config.output_dir.mkdir(parents=True, exist_ok=True)
    report_path = config.output_dir / "quality_report.json"
    executable = shutil.which(config.hadoop_command)
    if executable is None:
        report = _failure(
            task_id,
            dataset_version,
            config.rule_version,
            config.scoring_version,
            "hadoop_preflight",
            f"Hadoop command not found: {config.hadoop_command}",
        )
        _write_report(config.output_dir, report)
        return report

    ok, error = _run_stage((executable, "version"), "hadoop_preflight", config.output_dir)
    if not ok:
        report = _failure(task_id, dataset_version, config.rule_version, config.scoring_version, "hadoop_preflight", error)
        _write_report(config.output_dir, report)
        return report

    ok, error = _run_stage(config.pre_command, "pre_cleaning_quality", config.output_dir)
    if not ok:
        report = _failure(task_id, dataset_version, config.rule_version, config.scoring_version, "pre_cleaning_quality", error)
        _write_report(config.output_dir, report)
        return report
    try:
        pre = _read_json(config.output_dir / "pre_quality.json", "pre-cleaning")
        pre_stats = pre["statistics"]
        pre_score = score_quality(pre_stats)
    except (KeyError, TypeError, ValueError, OSError) as exc:
        report = _failure(task_id, dataset_version, config.rule_version, config.scoring_version, "pre_cleaning_quality", str(exc))
        _write_report(config.output_dir, report)
        return report

    ok, error = _run_stage(config.clean_command, "cleaning", config.output_dir)
    if not ok:
        report = _failure(task_id, dataset_version, config.rule_version, config.scoring_version, "cleaning", error)
        _write_report(config.output_dir, report)
        return report

    ok, error = _run_stage(config.post_command, "post_cleaning_quality", config.output_dir)
    if not ok:
        report = _failure(task_id, dataset_version, config.rule_version, config.scoring_version, "post_cleaning_quality", error)
        _write_report(config.output_dir, report)
        return report
    try:
        post = _read_json(config.output_dir / "post_quality.json", "post-cleaning")
        post_stats = post["statistics"]
        post_score = score_quality(post_stats)
        t1, t2 = post["T1"], post["T2"]
        disposition = post["disposition"]
        data_volume = post["data_volume"]
    except (KeyError, TypeError, ValueError, OSError) as exc:
        report = _failure(task_id, dataset_version, config.rule_version, config.scoring_version, "post_cleaning_quality", str(exc))
        _write_report(config.output_dir, report)
        return report

    report = {
        "task_id": task_id,
        "status": "succeeded",
        "dataset_version": dataset_version,
        "cleaned_data_version": post.get("cleaned_data_version"),
        "rule_version": config.rule_version,
        "scoring_version": config.scoring_version,
        "T1": t1,
        "T2": t2,
        "quality": {
            "formula": "dimension_score = 100 * numerator / total_records",
            "dimensions": list(DIMENSIONS),
            "pre": pre_score,
            "post": post_score,
            "delta": {key: round(post_score[key] - pre_score[key], 6) for key in DIMENSIONS},
        },
        "data_volume": data_volume,
        "disposition": disposition,
        "artifacts": {
            "pre_quality": str(config.output_dir / "pre_quality.json"),
            "post_quality": str(config.output_dir / "post_quality.json"),
        },
    }
    _write_report(config.output_dir, report)
    return report


def _write_report(output_dir: Path, report: dict[str, Any]) -> None:
    (output_dir / "quality_report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


def main() -> None:
    parser = argparse.ArgumentParser(description="Run Hadoop MovieLens quality pipeline")
    parser.add_argument("--input-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    report = run_pipeline(PipelineConfig(args.input_dir, args.output_dir))
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()

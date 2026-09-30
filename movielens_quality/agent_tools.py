"""Agent-callable tools for the MovieLens Hadoop governance pipeline."""

from __future__ import annotations

import json
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .hadoop_pipeline import (
    PipelineConfig,
    RULE_VERSION,
    SCORING_VERSION,
    _dataset_version,
    run_pipeline,
)

STATUSES = ("QUEUED", "RUNNING", "SUCCESS", "FAILED")
DATASET_NAME = "ml-1m"


@dataclass(frozen=True)
class AgentRequest:
    input_dir: Path
    output_dir: Path
    dataset_version: str | None = None
    rule_version: str = RULE_VERSION
    scoring_version: str = SCORING_VERSION
    hadoop_command: str = "hadoop"
    pre_command: tuple[str, ...] = ()
    clean_command: tuple[str, ...] = ()
    post_command: tuple[str, ...] = ()


def parse_natural_language_request(
    text: str,
    input_dir: str | Path,
    output_dir: str | Path,
) -> AgentRequest:
    """Parse only registered, safe options from a natural-language request."""

    if not text.strip():
        raise ValueError("Natural-language request cannot be empty")
    if not re.search(r"\bmovie\s* lens\b|\bmovielens\b|\bml-1m\b", text, re.I):
        raise ValueError("Request must explicitly target MovieLens 1M")

    dataset_match = re.search(r"dataset(?:_version| version)?\s*[:=]\s*([A-Za-z0-9_.-]+)", text, re.I)
    rule_match = re.search(r"rule(?:_version| version)?\s*[:=]\s*([A-Za-z0-9_.-]+)", text, re.I)
    scoring_match = re.search(r"(?:scoring|score)(?:_version| version)?\s*[:=]\s*([A-Za-z0-9_.-]+)", text, re.I)
    return AgentRequest(
        input_dir=Path(input_dir),
        output_dir=Path(output_dir),
        dataset_version=dataset_match.group(1) if dataset_match else None,
        rule_version=rule_match.group(1) if rule_match else RULE_VERSION,
        scoring_version=scoring_match.group(1) if scoring_match else SCORING_VERSION,
    )


class MovieLensGovernanceAgent:
    """Synchronous Agent tool facade with a durable JSON task registry."""

    def __init__(self, registry_path: str | Path):
        self.registry_path = Path(registry_path)
        self.registry_path.parent.mkdir(parents=True, exist_ok=True)

    def _read_registry(self) -> dict[str, Any]:
        if not self.registry_path.exists():
            return {}
        value = json.loads(self.registry_path.read_text(encoding="utf-8"))
        if not isinstance(value, dict):
            raise ValueError("Task registry must contain a JSON object")
        return value

    def _write_registry(self, registry: dict[str, Any]) -> None:
        self.registry_path.write_text(
            json.dumps(registry, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _save(self, task_id: str, record: dict[str, Any]) -> None:
        registry = self._read_registry()
        registry[task_id] = record
        self._write_registry(registry)

    def start_task(self, request: AgentRequest | str, **kwargs: Any) -> dict[str, Any]:
        """Queue and execute one Hadoop task, returning a structured result."""

        if isinstance(request, str):
            request = parse_natural_language_request(
                request,
                kwargs.pop("input_dir"),
                kwargs.pop("output_dir"),
            )
        if kwargs:
            raise TypeError(f"Unsupported request options: {', '.join(sorted(kwargs))}")

        task_id = f"agent-{__import__('uuid').uuid4().hex}"
        record: dict[str, Any] = {
            "task_id": task_id,
            "status": "QUEUED",
            "request": {
                "input_dir": str(request.input_dir),
                "output_dir": str(request.output_dir),
                "dataset_version": request.dataset_version,
                "rule_version": request.rule_version,
                "scoring_version": request.scoring_version,
            },
        }
        self._save(task_id, record)
        record["status"] = "RUNNING"
        self._save(task_id, record)

        try:
            if request.rule_version != RULE_VERSION:
                raise ValueError(
                    f"Rule version mismatch: requested {request.rule_version}, "
                    f"registered {RULE_VERSION}"
                )
            if request.scoring_version != SCORING_VERSION:
                raise ValueError(
                    f"Scoring version mismatch: requested {request.scoring_version}, "
                    f"registered {SCORING_VERSION}"
                )
            actual_dataset_version = _dataset_version(request.input_dir)
            if request.dataset_version and request.dataset_version != actual_dataset_version:
                raise ValueError(
                    f"Dataset version mismatch: requested {request.dataset_version}, "
                    f"actual {actual_dataset_version}"
                )

            pipeline_report = run_pipeline(
                PipelineConfig(
                    input_dir=request.input_dir,
                    output_dir=request.output_dir,
                    hadoop_command=request.hadoop_command,
                    pre_command=request.pre_command,
                    clean_command=request.clean_command,
                    post_command=request.post_command,
                    dataset_version=actual_dataset_version,
                    rule_version=request.rule_version,
                    scoring_version=request.scoring_version,
                )
            )
            status = "SUCCESS" if pipeline_report["status"] == "succeeded" else "FAILED"
            record.update(pipeline_report)
            record["backend_task_id"] = pipeline_report.get("task_id")
            record["task_id"] = task_id
            record["status"] = status
        except (OSError, ValueError, TypeError) as exc:
            record.update(
                {
                    "status": "FAILED",
                    "failed_stage": "agent_validation",
                    "error": str(exc),
                    "quality": None,
                }
            )

        log_path = request.output_dir / "agent.log"
        request.output_dir.mkdir(parents=True, exist_ok=True)
        log_path.write_text(
            json.dumps(
                {
                    "task_id": task_id,
                    "status": record["status"],
                    "failed_stage": record.get("failed_stage"),
                    "error": record.get("error"),
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        record["log_path"] = str(log_path)
        self._save(task_id, record)
        return record

    def get_task_status(self, task_id: str) -> dict[str, Any]:
        record = self._read_registry().get(task_id)
        if record is None:
            raise KeyError(f"Unknown task_id: {task_id}")
        return {
            "task_id": task_id,
            "status": record["status"],
            "failed_stage": record.get("failed_stage"),
            "error": record.get("error"),
            "log_path": record.get("log_path"),
        }

    def get_task_result(self, task_id: str) -> dict[str, Any]:
        record = self._read_registry().get(task_id)
        if record is None:
            raise KeyError(f"Unknown task_id: {task_id}")
        if record["status"] != "SUCCESS":
            return {
                "task_id": task_id,
                "status": record["status"],
                "failed_stage": record.get("failed_stage"),
                "error": record.get("error"),
                "log_path": record.get("log_path"),
            }
        output_dir = Path(record["request"]["output_dir"])
        report_path = output_dir / "quality_report.json"
        report = json.loads(report_path.read_text(encoding="utf-8"))
        anomalies_path = output_dir / "anomalies.jsonl"
        return {
            "task_id": task_id,
            "status": "SUCCESS",
            "quality": report["quality"],
            "anomalies": str(anomalies_path) if anomalies_path.exists() else None,
            "report": str(report_path),
            "log_path": record.get("log_path"),
        }

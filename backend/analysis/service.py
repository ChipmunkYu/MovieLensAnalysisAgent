from __future__ import annotations

import json
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from backend.analysis import classification, clustering, reduction
from backend.analysis.dataset import load_dataset
from backend.analysis.features import build_profiles
from backend.config import artifact_dir, data_version
from backend.models import AnalysisTask, TaskStatus, now

RUNNERS = {"classification": classification.run, "clustering": clustering.run, "dimensionality_reduction": reduction.run}


def _write_json(path: Path, value: object) -> dict:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"name": path.name, "path": str(path), "size": path.stat().st_size}


def create_analysis_task(session: Session, task_type: str, input_path: str, parameters: dict) -> AnalysisTask:
    if task_type not in RUNNERS:
        raise ValueError(f"不支持的分析任务类型：{task_type}")
    source_version = f"{data_version()}-clean" if "clean" in Path(input_path).name.lower() else data_version()
    task = AnalysisTask(task_type=task_type, input_path=input_path, input_data_version=source_version, parameters=parameters)
    session.add(task)
    session.commit()
    session.refresh(task)
    return task


def execute_analysis(session: Session, task: AnalysisTask) -> AnalysisTask:
    try:
        task.status, task.stage, task.started_at = TaskStatus.RUNNING, "loading_data", now(); session.commit()
        dataset = load_dataset(task.input_path)
        task.stage = "building_features"; session.commit()
        parameters = task.parameters or {}
        if task.task_type == "classification":
            result = classification.run(dataset, parameters.get("user_id"), parameters.get("movie_id"), parameters.get("algorithm"))
        elif task.task_type == "clustering":
            result = clustering.run(dataset, parameters.get("user_id"), parameters.get("algorithm"), parameters.get("n_clusters", 5))
        else:
            result = reduction.run(dataset, parameters.get("algorithm"), parameters.get("n_clusters", 5), parameters.get("dimensions", 2))
        task.stage = "publishing"; session.commit()
        output = artifact_dir() / "analysis" / task.id; output.mkdir(parents=True, exist_ok=True)
        _, _, _, profiles = build_profiles(dataset)
        artifacts = [_write_json(output / "result.json", result), _write_json(output / "user-profiles.json", {"profile_version": "user-profile-v2", "source_data_version": task.input_data_version, "time_boundary": result["data_split"], "profiles": profiles})]
        task.result, task.artifacts = result, artifacts
        task.output_version = result["model_version"]
        task.status, task.stage = TaskStatus.SUCCESS, "publishing"
    except Exception as exc:
        task.status, task.error = TaskStatus.FAILED, str(exc)
    task.finished_at = now(); session.commit(); session.refresh(task)
    return task


def run_analysis(session: Session, task_type: str, input_path: str, parameters: dict) -> AnalysisTask:
    task = create_analysis_task(session, task_type, input_path, parameters)
    return execute_analysis(session, task)


def work_analysis_once(session: Session) -> AnalysisTask | None:
    task = session.scalar(
        select(AnalysisTask).where(AnalysisTask.status == TaskStatus.QUEUED)
        .order_by(AnalysisTask.created_at).limit(1)
    )
    if not task:
        return None
    return execute_analysis(session, task)

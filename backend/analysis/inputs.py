"""Session-backed provenance checks for analysis inputs; artifacts are not raw inputs."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from sqlalchemy.orm import Session

from backend.config import artifact_dir, data_dir, data_version
from backend.executors import resolve_input
from backend.models import Task, TaskStatus

CLEAN_NAME = "ratings-cleaned.dat"


@dataclass(frozen=True)
class AnalysisSource:
    ratings: Path
    metadata: Path
    version: str


def _candidate(value: str) -> Path:
    path = Path(value).expanduser()
    return (path if path.is_absolute() else data_dir() / path).absolute()


def _is_artifact(path: Path) -> bool:
    roots = (artifact_dir().absolute(), artifact_dir().resolve())
    return any(candidate.is_relative_to(root) for candidate in (path, path.resolve()) for root in roots)


def raw_analysis_input(value: str) -> Path:
    if _is_artifact(_candidate(value)):
        raise ValueError("analysis artifacts require a registered successful cleaning task")
    return resolve_input(value)


def metadata_file(source: Path, name: str) -> Path:
    path = source.with_name(name)
    if not path.resolve().is_relative_to(data_dir().resolve()) or _is_artifact(path):
        raise ValueError(f"analysis metadata {name} must stay inside raw DATA_DIR, outside ARTIFACT_DIR")
    return path


def clean_analysis_source(task: Task) -> AnalysisSource:
    expected_version = f"{data_version()}-clean"
    if task.status != TaskStatus.SUCCESS or (task.result or {}).get("output_data_version") != expected_version:
        raise ValueError("clean analysis input requires a successful current-version cleaning task")
    if not task.id or task.id in (".", "..") or any(c in task.id for c in ("/", "\\", ":")):
        raise ValueError("invalid cleaning task directory")
    directory = artifact_dir().resolve() / task.id
    expected = directory / CLEAN_NAME
    if directory.resolve() != directory or expected.resolve() != expected:
        raise ValueError("redirected cleaning artifact or task directory")
    registered = False
    for item in task.artifacts or []:
        if item.get("name") != CLEAN_NAME:
            continue
        path = Path(item.get("path") or "")
        if (".." not in path.parts and path.absolute() in
                (expected, artifact_dir().absolute() / task.id / CLEAN_NAME) and path.resolve() == expected):
            registered = True
            break
    if not registered or not expected.is_file():
        raise ValueError("ratings-cleaned.dat must be registered at the exact cleaning task artifact path")
    try:
        origin = raw_analysis_input(task.input_path)
    except (OSError, ValueError) as exc:
        raise ValueError(f"cleaning task original raw input unavailable: {exc}") from exc
    # Validate metadata even when selecting the latest clean input, not only when loading it.
    metadata_file(origin, "movies.dat")
    metadata_file(origin, "users.dat")
    return AnalysisSource(expected, origin, expected_version)


def resolve_analysis_source(input_path: str, *, session: Session | None = None) -> AnalysisSource:
    candidate = _candidate(input_path)
    if not _is_artifact(candidate):
        source = raw_analysis_input(input_path)
        return AnalysisSource(source, source, data_version())
    if session is None:
        raise ValueError("analysis artifact input requires a database session")
    if ".." in candidate.parts:
        raise ValueError("traversal is not allowed in analysis artifact input")
    resolved = candidate.resolve()
    root = artifact_dir().resolve()
    if not resolved.is_relative_to(root):
        raise ValueError("analysis artifact escapes ARTIFACT_DIR")
    if candidate.is_relative_to(root):
        relative = candidate.relative_to(root)
    elif candidate.is_relative_to(artifact_dir().absolute()):
        relative = candidate.relative_to(artifact_dir().absolute())
    else:
        raise ValueError("analysis artifact aliases are not allowed")
    if len(relative.parts) != 2 or relative.name != CLEAN_NAME:
        raise ValueError("only a cleaning task's exact ratings-cleaned.dat is an analysis input")
    task = session.get(Task, relative.parts[0])
    if task is None:
        raise ValueError("analysis input has no registered cleaning task")
    source = clean_analysis_source(task)
    if source.ratings != resolved:
        raise ValueError("analysis input does not match the registered cleaning artifact")
    return source

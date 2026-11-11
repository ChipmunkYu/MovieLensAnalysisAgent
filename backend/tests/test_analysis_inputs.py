from datetime import timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from backend.agent import latest_clean_input, route
from backend.analysis.dataset import load_dataset
from backend.analysis.features import feature_schema
from backend.analysis.inputs import resolve_analysis_source
from backend.analysis.service import create_analysis_task, work_analysis_once
from backend.db import Base, get_db, make_engine
from backend.main import app
from backend.models import Task, TaskStatus, now
from backend.schemas import AgentMessage
from backend.service import create_task, work_once


@pytest.fixture(params=[False, True], ids=["sibling-artifacts", "nested-artifacts"])
def inputs(tmp_path, monkeypatch, request):
    raw = tmp_path / "raw"
    raw.mkdir()
    artifacts = raw / "artifacts" if request.param else tmp_path / "artifacts"
    artifacts.mkdir()
    lines = []
    for round_number in range(10):
        for user in range(1, 13):
            index = len(lines)
            lines.append(f"{user}::{round_number + 1}::{1 + (user + round_number) % 5}::{946684800 + index * 3600}\n")
    (raw / "ratings.dat").write_text("".join(lines), encoding="latin1")
    (raw / "movies.dat").write_text("".join(f"{movie}::Movie {movie}::Drama|Comedy\n" for movie in range(1, 11)), encoding="latin1")
    (raw / "users.dat").write_text("".join(f"{user}::M::{18 if user % 2 else 25}::1::12345\n" for user in range(1, 13)), encoding="latin1")
    monkeypatch.setenv("DATA_DIR", str(raw))
    monkeypatch.setenv("ARTIFACT_DIR", str(artifacts))
    monkeypatch.setenv("DATA_VERSION", "inputs-test-v1")
    monkeypatch.setenv("DEFAULT_INPUT_PATH", "ratings.dat")
    for key in ("T1", "T2", "MODEL_API_KEY", "MODEL_BASE_URL", "MODEL_NAME"):
        monkeypatch.delenv(key, raising=False)
    # TestClient startup must never initialize the configured live database.
    monkeypatch.setattr("backend.main.init_db", lambda: None)
    engine = make_engine(f"sqlite:///{tmp_path / 'inputs.db'}")
    Base.metadata.create_all(engine)
    factory = sessionmaker(engine, expire_on_commit=False)
    with factory() as session:
        clean = create_task(session, "ratings.dat", "local")
        work_once(session)
        assert clean.status == TaskStatus.SUCCESS
        yield session, clean, raw, artifacts
    engine.dispose()


def test_clean_agent_and_direct_api_keep_metadata(inputs):
    session, clean, raw, artifacts = inputs
    path = artifacts / clean.id / "ratings-cleaned.dat"
    dataset = load_dataset(str(path), session=session)
    assert dataset.source == path
    assert len(dataset.ratings) == 120
    assert dataset.age_buckets == [18, 25] and dataset.genre_names == ["Comedy", "Drama"]
    assert dataset.input_data_version == "inputs-test-v1-clean"
    with pytest.raises(ValueError, match="session"):
        load_dataset(str(path))
    reply, data = route(session, AgentMessage(message="预测用户1是否喜欢电影1"))
    assert data["status"] == "queued" and data["input_data_version"] == "inputs-test-v1-clean"
    completed = work_analysis_once(session)
    assert completed.status == TaskStatus.SUCCESS, completed.error
    assert completed.input_data_version == "inputs-test-v1-clean"
    assert completed.result["feature_schema"] == feature_schema(dataset)
    assert "user_age_18" in completed.result["feature_schema"]
    assert "genre_Drama" in completed.result["feature_schema"]

    def db():
        yield session
    app.dependency_overrides[get_db] = db
    try:
        with TestClient(app) as client:
            response = client.post("/api/analysis/classification", json={
                "input_path": str(path), "user_id": 1, "movie_id": 1})
            assert response.status_code == 201, response.text
            assert response.json()["status"] == "queued"
            completed = work_analysis_once(session)
            assert completed.status == TaskStatus.SUCCESS, completed.error
            assert completed.input_data_version == "inputs-test-v1-clean"
            assert completed.result["feature_schema"] == feature_schema(dataset)
    finally:
        app.dependency_overrides.clear()


def test_raw_named_clean_is_raw_and_worker_corrects_old_version(inputs):
    session, _, raw, _ = inputs
    path = raw / "already-clean-ratings.dat"
    path.write_bytes((raw / "ratings.dat").read_bytes())
    assert load_dataset(str(path)).input_data_version == "inputs-test-v1"
    task = create_analysis_task(session, "classification", str(path), {"user_id": 1, "movie_id": 1})
    assert task.input_data_version == "inputs-test-v1"
    task.input_data_version = "old-wrong-clean"
    session.commit()
    completed = work_analysis_once(session)
    assert completed.status == TaskStatus.SUCCESS, completed.error
    assert completed.input_data_version == "inputs-test-v1"


@pytest.mark.parametrize("invalid", ["failed", "stale", "unregistered", "cross-task", "wrong-name"])
def test_invalid_clean_provenance_denied_and_worker_fails_loading(inputs, invalid):
    session, clean, _, artifacts = inputs
    path = artifacts / clean.id / "ratings-cleaned.dat"
    if invalid == "failed":
        clean.status = TaskStatus.FAILED
    elif invalid == "stale":
        clean.result = {"output_data_version": "old-clean"}
    elif invalid == "unregistered":
        clean.artifacts = []
    elif invalid == "cross-task":
        clean.artifacts = [{"name": "ratings-cleaned.dat", "path": str(artifacts / "other" / path.name)}]
    else:
        clean.artifacts = [{"name": "result.json", "path": str(path)}]
    session.commit()
    with pytest.raises(ValueError):
        load_dataset(str(path), session=session)
    assert latest_clean_input(session) is None
    task = create_analysis_task(session, "classification", str(path), {"user_id": 1, "movie_id": 1})
    assert task.status == TaskStatus.QUEUED
    completed = work_analysis_once(session)
    assert completed.status == TaskStatus.FAILED and completed.stage == "loading_data"
    assert completed.error


def test_artifact_namespace_never_falls_through_to_raw(inputs):
    session, clean, raw, artifacts = inputs
    unknown = artifacts / "unknown" / "ratings-cleaned.dat"
    unknown.parent.mkdir()
    unknown.write_bytes((raw / "ratings.dat").read_bytes())
    analysis = artifacts / "analysis" / "unknown" / "result.json"
    analysis.parent.mkdir(parents=True)
    analysis.write_text("{}", encoding="utf-8")
    traversal = artifacts / clean.id / ".." / clean.id / "ratings-cleaned.dat"
    for path in (unknown, analysis, traversal, artifacts / clean.id / "report.json"):
        with pytest.raises(ValueError):
            resolve_analysis_source(str(path), session=session)
        with pytest.raises(ValueError):
            load_dataset(str(path))
    if artifacts.is_relative_to(raw):
        path = str((artifacts / clean.id / "ratings-cleaned.dat").relative_to(raw))
        assert resolve_analysis_source(path, session=session).version.endswith("-clean")
        # Even a lexical artifact path resolving back to a raw file cannot use the raw lane.
        with pytest.raises(ValueError):
            resolve_analysis_source(str(artifacts / ".." / "ratings.dat"), session=session)


def test_missing_origin_fails_and_latest_invalid_is_skipped(inputs):
    session, clean, raw, artifacts = inputs
    invalid = Task(id="newer", input_path="ratings.dat", executor="local", rules_version="2.0.0",
                   status=TaskStatus.SUCCESS, result={"output_data_version": "inputs-test-v1-clean"},
                   artifacts=[{"name": "ratings-cleaned.dat", "path": str(artifacts / clean.id / "ratings-cleaned.dat")}],
                   finished_at=now() + timedelta(seconds=1))
    session.add(invalid)
    session.commit()
    selected = latest_clean_input(session)
    assert resolve_analysis_source(selected, session=session).ratings == artifacts / clean.id / "ratings-cleaned.dat"
    (raw / "ratings.dat").unlink()
    with pytest.raises(ValueError, match="original raw input unavailable"):
        load_dataset(str(artifacts / clean.id / "ratings-cleaned.dat"), session=session)
    assert latest_clean_input(session) is None


@pytest.mark.parametrize("redirect", ["task-directory", "artifact-file", "metadata", "raw-alias", "artifact-to-raw"])
def test_symlink_redirect_simulation_is_denied(inputs, monkeypatch, redirect):
    session, clean, raw, artifacts = inputs
    directory = artifacts / clean.id
    path = directory / "ratings-cleaned.dat"
    original = Path.resolve
    redirected = directory if redirect == "task-directory" else raw / "movies.dat"
    destination = artifacts / "sibling" if redirect == "task-directory" else raw.parent / "outside-movies.dat"
    if redirect == "artifact-file":
        redirected, destination = path, artifacts / "sibling" / "ratings-cleaned.dat"
    elif redirect == "raw-alias":
        redirected, destination = raw / "alias.dat", path
        path = redirected
    elif redirect == "artifact-to-raw":
        redirected, destination = artifacts / "alias.dat", raw / "ratings.dat"
        path = redirected

    def resolve(self, *args, **kwargs):
        if self == redirected or self.is_relative_to(redirected):
            return destination / self.relative_to(redirected)
        return original(self, *args, **kwargs)
    monkeypatch.setattr(Path, "resolve", resolve)
    with pytest.raises(ValueError):
        load_dataset(str(path), session=session)
    if redirect in ("task-directory", "artifact-file", "metadata"):
        assert latest_clean_input(session) is None
    if redirect == "metadata":
        with pytest.raises(ValueError, match="metadata"):
            load_dataset("ratings.dat")

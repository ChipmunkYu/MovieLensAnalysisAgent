from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from backend.db import Base, get_db, make_engine
from backend.main import app
from backend.analysis.service import work_analysis_once


@pytest.fixture
def analysis_flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data = tmp_path / "data"; data.mkdir()
    lines = []
    timestamp = 946684800
    for round_number in range(10):
        for user in range(1, 13):
            movie = round_number + 1
            rating = 1 + ((user + movie) % 5)
            lines.append(f"{user}::{movie}::{rating}::{timestamp}\n")
            timestamp += 3600
    (data / "ratings.dat").write_text("".join(lines), encoding="iso-8859-1")
    (data / "movies.dat").write_text("".join(f"{movie}::Movie {movie}::{'Drama' if movie % 2 else 'Comedy'}\n" for movie in range(1, 11)), encoding="iso-8859-1")
    monkeypatch.setenv("DATA_DIR", str(data)); monkeypatch.setenv("DEFAULT_INPUT_PATH", "ratings.dat")
    monkeypatch.setenv("ARTIFACT_DIR", str(tmp_path / "artifacts"))
    monkeypatch.delenv("T1", raising=False); monkeypatch.delenv("T2", raising=False)
    engine = make_engine(f"sqlite:///{tmp_path / 'analysis.db'}"); Base.metadata.create_all(engine)
    Session = sessionmaker(engine, expire_on_commit=False)
    def test_db():
        with Session() as session: yield session
    app.dependency_overrides[get_db] = test_db
    try:
        with TestClient(app) as client: yield client, Session
    finally:
        app.dependency_overrides.clear()


def test_classification_agent_runs_two_real_models(analysis_flow):
    client, Session = analysis_flow
    response = client.post("/api/agent/messages", json={"message": "预测用户 1 是否会喜欢电影 1"})
    assert response.status_code == 200
    body = response.json()
    assert body["data"]["status"] == "queued"
    with Session() as session:
        work_analysis_once(session)
    result = client.get(f"/api/analysis/{body['task_id']}").json()["result"]
    assert result["task_type"] == "classification"
    assert {item["algorithm"] for item in result["results"]} == {"logistic_regression", "random_forest"}
    assert all(0 <= item["positive_probability"] <= 1 for item in result["results"])
    assert all("f1" in item["test_metrics"] for item in result["results"])
    assert all(item["model_state"] for item in result["results"])
    assert result["data_split"]["strategy"] == "chronological"


def test_clustering_profiles_are_persisted(analysis_flow):
    client, Session = analysis_flow
    response = client.post("/api/analysis/clustering", json={"input_path": "ratings.dat", "user_id": 1, "n_clusters": 4})
    assert response.status_code == 201
    body = response.json()
    with Session() as session:
        work_analysis_once(session)
    body = client.get(f"/api/analysis/{body['id']}").json()
    assert body["status"] == "succeeded"
    assert {item["algorithm"] for item in body["result"]["results"]} == {"kmeans", "dbscan"}
    assert all(item["assignments"] for item in body["result"]["results"])
    task_id = body["id"]
    task = client.get(f"/api/analysis/{task_id}").json()
    assert task["output_version"] == "clustering-v2"
    assert client.get(f"/api/analysis/{task_id}/artifacts/user-profiles.json").status_code == 200


def test_reduction_returns_coordinates_and_comparison(analysis_flow):
    client, Session = analysis_flow
    body = client.post("/api/analysis/reduction", json={"input_path": "ratings.dat", "dimensions": 2, "n_clusters": 4}).json()
    with Session() as session:
        work_analysis_once(session)
    body = client.get(f"/api/analysis/{body['id']}").json()
    assert body["status"] == "succeeded"
    result = body["result"]
    assert {item["algorithm"] for item in result["results"]} == {"pca", "svd"}
    assert all(item["coordinates"] for item in result["results"])
    assert all("before" in item["clustering_comparison"] and "after" in item["clustering_comparison"] for item in result["results"])

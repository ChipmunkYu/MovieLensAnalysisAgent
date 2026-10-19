from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import sessionmaker

from backend.db import Base, get_db, make_engine
from backend.main import app
from backend.models import Task, TaskStatus
from backend.service import create_task, work_once


@pytest.fixture
def flow(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data = tmp_path / "data"
    data.mkdir()
    (data / "ratings.dat").write_text(
        "1::10::4::946684800\n1::10::5::946771200\n2::20::9::946857600\nbad row\n3::30::3::978307200\n",
        encoding="iso-8859-1",
    )
    monkeypatch.setenv("DATA_DIR", str(data))
    monkeypatch.setenv("DEFAULT_INPUT_PATH", "ratings.dat")
    monkeypatch.setenv("ARTIFACT_DIR", str(tmp_path / "artifacts"))
    for name in ("MODEL_API_KEY", "MODEL_BASE_URL", "MODEL_NAME", "DEFAULT_EXECUTOR", "T1", "T2"):
        monkeypatch.delenv(name, raising=False)
    engine = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(engine, expire_on_commit=False)

    def test_db():
        with Session() as session:
            yield session

    app.dependency_overrides[get_db] = test_db
    try:
        with TestClient(app) as client:
            yield client, Session
    finally:
        app.dependency_overrides.clear()


def test_default_agent_work_result_and_artifact(flow):
    client, Session = flow
    created = client.post(
        "/api/agent/messages",
        json={"message": "请使用默认规则清洗 MovieLens 1M，评估清洗前后的五个数据质量维度，并说明处理了哪些问题。"},
    )
    assert created.status_code == 200
    body = created.json()
    assert body["task_id"] == body["data"]["id"]
    task_id = body["task_id"]
    assert client.get(f"/api/tasks/{task_id}").json()["status"] == "queued"

    with Session() as session:
        completed = work_once(session)
    assert completed and completed.status == TaskStatus.SUCCESS
    assert client.get(f"/api/tasks/{task_id}").json()["status"] == "succeeded"

    response = client.get(f"/api/tasks/{task_id}/result")
    assert response.status_code == 200
    result = response.json()
    assert result["rows_before"] == 5
    assert result["rows_after"] == 2
    assert {score["dimension"] for score in result["dimensions"]} == {
        "accurate", "completeness", "unique", "up_to_date", "consistent"
    }
    assert all(score["before"] != score["after"] for score in result["dimensions"][:3])
    assert result["rule_version"] == "2.0.0"
    assert result["quarantine"]["records"] == 2
    assert result["repairs"]["records"] == 0
    assert result["method"] and result["limitations"] and result["unverified"]

    artifacts = client.get(f"/api/tasks/{task_id}/artifacts").json()["artifacts"]
    assert "path" not in artifacts[0]
    download = client.get(artifacts[0]["download_url"])
    assert download.status_code == 200
    assert download.content.decode("iso-8859-1") == "1::10::5::946771200\n3::30::3::978307200\n"
    assert {artifact["name"] for artifact in artifacts} == {
        "ratings-cleaned.dat",
        "ratings-quarantine.tsv",
        "report.json",
    }

    with Session() as session:
        task = session.get(Task, task_id)
        outside = Path(task.artifacts[0]["path"]).parents[2] / "secret.txt"
        outside.write_text("not an artifact", encoding="utf-8")
        task.artifacts = [*task.artifacts, {"name": "secret.txt", "path": str(outside), "size": outside.stat().st_size}]
        session.commit()
    assert client.get(f"/api/tasks/{task_id}/artifacts/secret.txt").status_code == 404

    rules = client.get("/api/rules/default").json()
    assert rules["version"] == "2.0.0" and rules["name"] and rules["rules"] and rules["raw"]


def test_missing_hadoop_binary_marks_task_failed(flow, monkeypatch: pytest.MonkeyPatch):
    _, Session = flow
    monkeypatch.setenv("HADOOP_BIN", "definitely-not-installed-hadoop")
    with Session() as session:
        task = create_task(session, "ratings.dat", "hadoop")
        task_id = task.id
        completed = work_once(session)
        assert completed and completed.status == TaskStatus.FAILED
        assert "definitely-not-installed-hadoop" in completed.error
        assert "HADOOP_BIN" in completed.error
        assert session.get(Task, task_id).status == TaskStatus.FAILED


def test_agent_selects_hadoop_when_requested(flow, monkeypatch: pytest.MonkeyPatch):
    client, Session = flow
    monkeypatch.setenv("HADOOP_BIN", "definitely-not-installed-hadoop")
    created = client.post("/api/agent/messages", json={"message": "请用 Hadoop 清洗并对比五维评分"}).json()
    with Session() as session:
        task = session.get(Task, created["task_id"])
        assert task.executor == "hadoop"

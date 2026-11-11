import copy
import json
from pathlib import Path

import httpx
import pytest

from backend import agent
from backend.analysis.service import work_analysis_once
from backend.models import AnalysisTask, Task, TaskStatus
from backend.schemas import AgentMessage
from backend.task_evidence import BUSINESS_FILES, MAX_CALLS, TaskEvidence, evidence_tools
from backend.tests.test_flow import flow
from backend.tests.test_analysis import analysis_flow


@pytest.fixture(params=[False, True], ids=["clean", "analysis"])
def evidence(tmp_path, monkeypatch, request):
    root = tmp_path / "artifacts"
    monkeypatch.setenv("ARTIFACT_DIR", str(root))
    for name in ("MODEL_API_KEY", "MODEL_BASE_URL", "MODEL_NAME"):
        monkeypatch.delenv(name, raising=False)
    cls = AnalysisTask if request.param else Task
    task = cls(id="current", status=TaskStatus.FAILED, stage="building_features" if request.param else "clean",
               input_path="ratings.dat", error="test failure", artifacts=[], result={})
    if request.param:
        task.task_type = "clustering"
        task.parameters = {"user_id": 1, "n_clusters": 5}
        task.input_data_version = "test-v1"
    else:
        task.executor = "local"
        task.rules_version = "2.0.0"
    directory = root / "analysis" / task.id if request.param else root / task.id
    directory.mkdir(parents=True)
    path = directory / "result.json"
    path.write_text('{"evidence":"current only"}', encoding="utf-8")
    task.artifacts = [{"name": path.name, "path": str(path), "size": path.stat().st_size}]
    return TaskEvidence(task), directory, root


def test_registered_text_paging_and_encodings(evidence):
    reader, directory, _ = evidence
    assert reader.list_task_files()["files"][0]["name"] == "result.json"
    page = reader.call("read_task_file", {"name": "result.json", "limit": 8})
    assert page["content"] == '{"eviden'
    assert page["truncated"] and page["next_offset"] == 8
    tail = reader.call("read_task_file", {"name": "result.json", "offset": 8})
    assert not tail["truncated"] and tail["next_offset"] is None
    assert json.loads(page["content"] + tail["content"])["evidence"] == "current only"
    path = directory / "ratings.dat"
    path.write_bytes(b"caf\xe9\n")
    reader.task.artifacts.append({"name": path.name, "path": str(path)})
    assert reader.call("read_task_file", {"name": path.name})["content"] == "café\n"
    for args in ({"limit": 8193}, {"offset": -1}, {"offset": True}, {"offset": 9999}):
        assert "error" in reader.call("read_task_file", {"name": "result.json", **args})
    utf8 = directory / "utf8.json"
    utf8.write_text('"中文"', encoding="utf-8")
    reader.task.artifacts.append({"name": utf8.name, "path": str(utf8)})
    reader = TaskEvidence(reader.task)
    first = reader.call("read_task_file", {"name": utf8.name, "limit": 5})
    second = reader.call("read_task_file", {"name": utf8.name, "offset": first["next_offset"]})
    assert first["end_offset"] == 4 and first["content"] + second["content"] == '"中文"'


def test_reject_unknown_paths_cross_task_and_binary(evidence):
    reader, directory, root = evidence
    for name in ("../result.json", "/result.json", "C:\\result.json", "..\\result.json", "unknown.json", "result.json:stream"):
        assert "error" in reader.call("read_task_file", {"name": name})
    other = root / "other"
    other.mkdir()
    outside = other / "secret.json"
    outside.write_text("private", encoding="utf-8")
    reader.task.artifacts.append({"name": "secret.json", "path": str(outside)})
    assert "error" in TaskEvidence(reader.task).call("read_task_file", {"name": "secret.json"})
    for name, content in (("binary.json", b"\x00binary"), ("binary.png", b"printable"), ("invalid.json", b"\xffbad")):
        path = directory / name
        path.write_bytes(content)
        reader.task.artifacts.append({"name": name, "path": str(path)})
        assert "error" in TaskEvidence(reader.task).call("read_task_file", {"name": name})
    assert "error" in TaskEvidence(reader.task).call("read_task_file", {"name": "result.json", "task_id": "other"})
    (directory / "unregistered.json").write_text('"not registered"', encoding="utf-8")
    assert "error" in TaskEvidence(reader.task).call("read_task_file", {"name": "unregistered.json"})
    secret = directory / ".env"
    secret.write_text("not evidence", encoding="utf-8")
    reader.task.artifacts.append({"name": "alias.json", "path": str(secret)})
    assert "error" in TaskEvidence(reader.task).call("read_task_file", {"name": "alias.json"})


def test_symlink_file_and_task_directory_escape(evidence):
    reader, directory, root = evidence
    outside = root.parent / "outside"
    outside.mkdir()
    (outside / "result.json").write_text('"secret"', encoding="utf-8")
    link = directory / "link.json"
    try:
        link.symlink_to(outside / "result.json")
    except OSError:
        pytest.skip("symlink privilege unavailable")
    reader.task.artifacts.append({"name": link.name, "path": str(link)})
    assert "error" in reader.call("read_task_file", {"name": link.name})
    # A task directory redirected even to another directory inside ARTIFACT_DIR is invalid.
    link.unlink()
    (directory / "result.json").unlink()
    directory.rmdir()
    directory.symlink_to(outside, target_is_directory=True)
    assert "error" in reader.call("read_task_file", {"name": "result.json"})
    directory.unlink()
    sibling = root / "sibling"
    sibling.mkdir()
    (sibling / "result.json").write_text('"other task"', encoding="utf-8")
    directory.symlink_to(sibling, target_is_directory=True)
    assert "error" in reader.call("read_task_file", {"name": "result.json"})


@pytest.mark.parametrize("redirect", ["directory", "artifact"])
def test_redirected_realpath_without_symlink_privileges(evidence, monkeypatch, redirect):
    reader, directory, root = evidence
    sibling = root / "sibling"
    sibling.mkdir()
    outside = sibling / "result.json"
    outside.write_text('"other task"', encoding="utf-8")
    target = directory if redirect == "directory" else directory / "result.json"
    resolved = sibling if redirect == "directory" else outside
    original_resolve = Path.resolve

    def resolve(path, *args, **kwargs):
        return resolved if path == target else original_resolve(path, *args, **kwargs)

    monkeypatch.setattr(Path, "resolve", resolve)
    with pytest.raises(ValueError, match="redirected task directory" if redirect == "directory" else "artifact outside current task directory"):
        reader._artifact("result.json")
    assert "error" in reader.call("read_task_file", {"name": "result.json"})
    assert reader.list_task_files()["files"] == []


def test_plain_text_reply_preserves_filename_and_identifier_underscores():
    text = "# **依据** `backend/analysis/math_tools.py`：_ratings_cleaned_，__强调__，ratings_cleaned.dat"
    assert agent.plain_text_reply(text) == "依据 backend/analysis/math_tools.py：ratings_cleaned，强调，ratings_cleaned.dat"


def test_inventory_descriptions(evidence):
    reader, directory, _ = evidence
    artifacts = {
        "report.json": "质量评估", "ratings-cleaned.dat": "评分记录",
        "ratings-quarantine.tsv": "隔离原因", "result.json": "分析任务",
        "user-profiles.json": "用户画像", "custom.json": "具体内容需读取确认",
    }
    for name in artifacts:
        path = directory / name
        path.write_text("{}", encoding="utf-8")
        if name != "result.json":
            reader.task.artifacts.append({"name": name, "path": str(path)})
    inventory = reader.list_task_files()
    assert {item["name"] for item in inventory["files"]} == set(artifacts)
    for item in inventory["files"]:
        assert item["size"] == 2 and artifacts[item["name"]] in item["description"]
    business = {
        "backend/scoring.py": "五维评分", "backend/executors.py": "本地清洗",
        "backend/analysis/dataset.py": "数据加载", "backend/analysis/features.py": "特征构建",
        "backend/analysis/classification.py": "逻辑回归与决策树",
        "backend/analysis/clustering.py": "KMeans 与 DBSCAN",
        "backend/analysis/reduction.py": "PCA 与 SVD",
        "backend/analysis/math_tools.py": "标准化", "rules/default.json": "去重策略",
    }
    assert tuple(business) == BUSINESS_FILES
    assert [item["name"] for item in inventory["business_files"]] == list(business)
    for item in inventory["business_files"]:
        assert set(item) == {"name", "description"} and business[item["name"]] in item["description"]
    tool = next(item["function"] for item in evidence_tools() if item["function"]["name"] == "read_business_file")
    assert tool["parameters"]["properties"]["name"]["enum"] == list(business)


def test_business_allowlist_and_budget(evidence):
    reader, _, _ = evidence
    assert len(BUSINESS_FILES) == 9
    for name in BUSINESS_FILES:
        result = TaskEvidence(reader.task).call("read_business_file", {"name": name, "limit": 100})
        assert result["source"] == "current_business_source_not_execution_snapshot"
    for name in ("backend/agent.py", "backend/config.py", "backend/db.py", "backend/main.py", "backend/service.py",
                 "frontend/src/App.tsx", ".env", "rules/../rules/default.json"):
        assert "error" in TaskEvidence(reader.task).call("read_business_file", {"name": name})
    for _ in range(MAX_CALLS):
        assert "files" in reader.call("list_task_files", {})
    assert "error" in reader.call("list_task_files", {})


def configure_model(monkeypatch):
    monkeypatch.setenv("MODEL_API_KEY", "mock-key")
    monkeypatch.setenv("MODEL_BASE_URL", "https://model.invalid/v1")
    monkeypatch.setenv("MODEL_NAME", "mock-model")


def inspect_decision(url, payload):
    if any(tool["function"]["name"] == "inspect_current_task" for tool in payload["tools"]):
        return httpx.Response(200, json={"choices": [{"message": {"tool_calls": [{
            "id": "inspect", "type": "function", "function": {"name": "inspect_current_task", "arguments": "{}"},
        }]}}]}, request=httpx.Request("POST", url))


class TaskSession:
    def __init__(self, task):
        self.task = task

    def get(self, cls, task_id):
        return self.task if isinstance(self.task, cls) and task_id == self.task.id else None


def test_model_round_trip_includes_failed_context_and_sources(evidence, monkeypatch):
    reader, _, _ = evidence
    configure_model(monkeypatch)
    payloads = []

    def post(url, **kwargs):
        decision = inspect_decision(url, kwargs["json"])
        if decision is not None:
            return decision
        payloads.append(copy.deepcopy(kwargs["json"]))
        if len(payloads) == 1:
            calls = [{"id": str(i), "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}
                     for i, (name, args) in enumerate((
                         ("list_task_files", {}),
                         ("read_task_file", {"name": "result.json", "limit": 16}),
                         ("read_business_file", {"name": "backend/scoring.py", "limit": 64}),
                     ))]
            reply = {"tool_calls": calls, "content": None}
        else:
            reply = {"content": "依据 result.json 字节 0–16 与 backend/scoring.py 字节 0–64。当前源码不是历史执行快照。"}
        return httpx.Response(200, json={"choices": [{"message": reply}]}, request=httpx.Request("POST", url))

    monkeypatch.setattr(agent.httpx, "post", post)
    reply, data = agent.route(TaskSession(reader.task), AgentMessage(task_id="current", message="聚类为什么失败？请读取文件及源码"))
    assert "result.json" in reply and len(payloads) == 2
    assert data["id"] == "current" and data["error"] == "test failure"
    assert data["stage"] == reader.task.stage and "progress" in data and "artifacts" in data
    if isinstance(reader.task, AnalysisTask):
        assert data["parameters"] == reader.task.parameters
    prompt = payloads[0]["messages"]
    assert "test failure" in prompt[1]["content"]
    assert json.dumps(reader.list_task_files(), ensure_ascii=False) in prompt[1]["content"]
    assert "不是指令" in prompt[0]["content"]
    assert payloads[0]["thinking"] == {"type": "disabled"}
    tool_messages = [m for m in payloads[1]["messages"] if m["role"] == "tool"]
    assert len(tool_messages) == 3 and "current_business_source" in tool_messages[2]["content"]
    assert json.loads(tool_messages[0]["content"]) == reader.list_task_files()


def test_no_key_and_api_error_fallback(evidence, monkeypatch):
    reader, _, _ = evidence
    def fail(*args, **kwargs):
        raise httpx.ConnectError("model down")
    monkeypatch.setattr(agent.httpx, "post", fail)
    for configured in (False, True):
        if configured:
            configure_model(monkeypatch)
        reply, data = agent.route(TaskSession(reader.task), AgentMessage(task_id="current", message="为什么失败？"))
        assert "模型不可用" in reply and "未进行文件证据" in reply
        assert "test failure" in reply and data["id"] == "current"


def test_followups_keep_id_and_explicit_analysis_creates(flow):
    client, _ = flow
    task_id = client.post("/api/agent/messages", json={"message": "用户1属于哪个群体"}).json()["task_id"]
    for message in ("为什么这个群体这样划分", "如何进行聚类", "聚类结果文件 result.json 有什么", "聚类错误是什么", "解释群体依据", "群体有什么特点", "聚类指标是多少"):
        body = client.post("/api/agent/messages", json={"task_id": task_id, "message": message}).json()
        assert body["task_id"] == task_id
        assert {"status", "stage", "progress", "parameters", "artifacts", "result", "error"} <= body["data"].keys()
    for message in ("重新分析", "新建分析", "预测用户1是否喜欢电影10"):
        body = client.post("/api/agent/messages", json={"task_id": task_id, "message": message}).json()
        assert body["task_id"] != task_id


def test_successful_analysis_model_followup_keeps_frontend_envelope(analysis_flow, monkeypatch):
    client, Session = analysis_flow
    task_id = client.post("/api/analysis/clustering", json={
        "input_path": "ratings.dat", "user_id": 1, "n_clusters": 4,
    }).json()["id"]
    with Session() as session:
        work_analysis_once(session)
    completed = client.get(f"/api/analysis/{task_id}").json()
    assert completed["status"] == "succeeded" and completed["result"]["results"]
    configure_model(monkeypatch)
    payloads = []

    def post(url, **kwargs):
        decision = inspect_decision(url, kwargs["json"])
        if decision is not None:
            return decision
        payloads.append(copy.deepcopy(kwargs["json"]))
        if len(payloads) == 1:
            reply = {"tool_calls": [{"id": "result", "type": "function", "function": {
                "name": "read_task_file", "arguments": json.dumps({"name": "result.json"}),
            }}]}
        else:
            reply = {"content": "依据 `result.json` 字节 0–8192，聚类已完成；仅读取部分产物。"}
        return httpx.Response(200, json={"choices": [{"message": reply}]}, request=httpx.Request("POST", url))

    monkeypatch.setattr(agent.httpx, "post", post)
    response = client.post("/api/agent/messages", json={"task_id": task_id, "message": "读取聚类结果文件并解释依据"})
    assert response.status_code == 200
    body = response.json()
    assert body["task_id"] == task_id and "result.json" in body["reply"] and len(payloads) == 2
    for key in ("id", "status", "task_type", "result"):
        assert body["data"][key] == completed[key]
    assert body["data"]["artifacts"] == [
        {key: item[key] for key in ("name", "size", "download_url")} for item in completed["artifacts"]
    ]
    evidence = json.loads(next(m["content"] for m in payloads[1]["messages"] if m["role"] == "tool"))
    assert evidence["source"] == "current_task_artifact" and evidence["name"] == "result.json"
    artifact = next(item for item in body["data"]["artifacts"] if item["name"] == "result.json")
    downloaded = client.get(artifact["download_url"])
    assert downloaded.status_code == 200 and downloaded.json() == completed["result"]


def test_snapshot_and_model_turns_are_bounded(evidence, monkeypatch):
    reader, _, _ = evidence
    reader.task.result = {"large": ["中" * 10000] * 10000}
    assert len(json.dumps(agent.bounded_snapshot(reader.task), ensure_ascii=False).encode()) <= 16384
    configure_model(monkeypatch)
    turns = []
    def post(url, **kwargs):
        decision = inspect_decision(url, kwargs["json"])
        if decision is not None:
            return decision
        turns.append(1)
        reply = {"tool_calls": [{"id": str(len(turns)), "type": "function", "function": {
            "name": "list_task_files", "arguments": "{}"}}]}
        return httpx.Response(200, json={"choices": [{"message": reply}]}, request=httpx.Request("POST", url))
    monkeypatch.setattr(agent.httpx, "post", post)
    reply, _ = agent.route(TaskSession(reader.task), AgentMessage(task_id="current", message="查看文件"))
    assert len(turns) == 5 and "模型不可用" in reply

import copy
import json

import httpx
import pytest
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from backend import agent
from backend.db import Base, make_engine
from backend.models import AnalysisTask, Task, TaskStatus
from backend.schemas import AgentMessage


@pytest.fixture
def routing(tmp_path, monkeypatch):
    monkeypatch.setenv("DATABASE_URL", "sqlite://")
    monkeypatch.setenv("ARTIFACT_DIR", str(tmp_path))
    for name, value in (("MODEL_API_KEY", "mock-key"), ("MODEL_BASE_URL", "https://model.invalid/v1"), ("MODEL_NAME", "mock")):
        monkeypatch.setenv(name, value)
    engine = make_engine("sqlite://")
    Base.metadata.create_all(engine)
    with Session(engine, expire_on_commit=False) as session:
        task = AnalysisTask(id="current", task_type="classification", status=TaskStatus.SUCCESS,
                            stage="publishing", input_path="original/ratings.dat", input_data_version="test-v1",
                            parameters={"user_id": 1768, "movie_id": 10, "algorithm": "all"},
                            result={"results": [{"algorithm": "logistic_regression"}, {"algorithm": "decision_tree"}]}, artifacts=[])
        directory = tmp_path / "analysis" / task.id
        directory.mkdir(parents=True)
        path = directory / "result.json"
        path.write_text(json.dumps(task.result), encoding="utf-8")
        task.artifacts = [{"name": path.name, "path": str(path), "size": path.stat().st_size}]
        session.add(task)
        session.commit()
        yield session, task
    engine.dispose()


def operation(name, args="{}"):
    return {"tool_calls": [{"id": "op", "type": "function", "function": {"name": name, "arguments": args}}]}


def mock_model(monkeypatch, replies):
    payloads = []
    def post(url, **kwargs):
        payloads.append(copy.deepcopy(kwargs["json"]))
        reply = replies[len(payloads) - 1]
        if isinstance(reply, Exception):
            raise reply
        return httpx.Response(200, json={"choices": [{"message": reply}]}, request=httpx.Request("POST", url))
    monkeypatch.setattr(agent.httpx, "post", post)
    return payloads


def count(session):
    return sum(session.scalar(select(func.count()).select_from(cls)) for cls in (Task, AnalysisTask))


@pytest.mark.parametrize("message", [
    "刚刚预测用户 1768 是否会喜欢电影 10，用了哪两种算法",
    "刚才那次预测的算法告诉我一下", "不要预测用户1是否喜欢电影1",
    "‘预测用户1是否喜欢电影1’是我刚才说的", "重新训练是指什么",
])
def test_model_answers_past_or_negated_requests_without_writes(routing, monkeypatch, message):
    session, task = routing
    payloads = mock_model(monkeypatch, [{"content": "本次结果记录了 logistic_regression 和 decision_tree。"}])
    monkeypatch.setattr(agent, "deterministic", lambda *args: pytest.fail("configured path used keywords"))
    reply, data = agent.route(session, AgentMessage(task_id=task.id, message=message))
    assert "decision_tree" in reply and data == agent.task_snapshot(task) and count(session) == 1
    payload = payloads[0]
    assert payload["tool_choice"] == "auto" and len(payloads) == 1
    assert message in payload["messages"][1]["content"]
    assert json.dumps(agent.bounded_snapshot(task), ensure_ascii=False) in payload["messages"][1]["content"]
    names = {tool["function"]["name"] for tool in payload["tools"]}
    assert not {"get_task", "get_result"} & names


def test_inspect_delegates_to_bound_readonly_evidence(routing, monkeypatch):
    session, task = routing
    payloads = mock_model(monkeypatch, [operation("inspect_current_task"), operation("read_task_file", '{"name":"result.json"}'),
                                       {"content": "依据 result.json，算法是 logistic_regression 和 decision_tree。"}])
    reply, data = agent.route(session, AgentMessage(task_id=task.id, message="读取结果说明算法"))
    assert count(session) == 1 and data["id"] == task.id and "decision_tree" in reply
    evidence = json.loads(next(m["content"] for m in payloads[2]["messages"] if m["role"] == "tool"))
    assert evidence["source"] == "current_task_artifact" and "decision_tree" in evidence["content"]


@pytest.mark.parametrize("name,args", [("predict_rating_class", '{"user_id":1,"movie_id":10}'),
                                      ("create_task", '{"executor":"local"}'),
                                      ("run_user_clustering", '{"user_id":1,"n_clusters":3}'),
                                      ("run_dimensionality_reduction", '{"dimensions":2}')])
def test_clear_new_operation_creates_once_no_postcommit_model_call(routing, monkeypatch, name, args):
    session, _ = routing
    monkeypatch.setattr(agent, "resolve_analysis_input", lambda *args: "ratings.dat")
    payloads = mock_model(monkeypatch, [operation(name, args)])
    reply, data = agent.route(session, AgentMessage(message="请新建并执行这个操作"))
    assert count(session) == 2 and data["status"] == "queued" and "已创建" in reply and len(payloads) == 1


@pytest.mark.parametrize("api_args,tool_args,expected", [
    ({}, {}, {"input_path": "configured/ratings.dat", "executor": "hadoop"}),
    ({"input_path": "api/ratings.dat", "executor": "local"}, {},
     {"input_path": "api/ratings.dat", "executor": "local"}),
    ({"input_path": "api/ratings.dat", "executor": "local"},
     {"input_path": "tool/ratings.dat", "executor": "hadoop"},
     {"input_path": "tool/ratings.dat", "executor": "hadoop"}),
])
def test_default_cleaning_prompt_has_complete_pipeline_and_effective_defaults(routing, monkeypatch, api_args, tool_args, expected):
    session, _ = routing
    monkeypatch.setenv("DEFAULT_INPUT_PATH", "configured/ratings.dat")
    monkeypatch.setenv("DEFAULT_EXECUTOR", "hadoop")
    monkeypatch.setattr(agent, "deterministic", lambda *args: pytest.fail("model routing bypassed"))
    payloads = mock_model(monkeypatch, [operation("create_task", json.dumps(tool_args))])
    prompt = "请使用默认规则清洗 MovieLens 1M，评估清洗前后的五个数据质量维度，并说明处理了哪些问题。"
    reply, data = agent.route(session, AgentMessage(message=prompt, **api_args))
    assert count(session) == 2 and data["status"] == "queued" and "等待 Worker" in reply
    created = session.get(Task, data["id"])
    assert created.input_path == expected["input_path"] and created.executor == expected["executor"]
    assert created.result is None and len(payloads) == 1
    payload = payloads[0]
    assert payload["tool_choice"] == "auto" and prompt in payload["messages"][1]["content"]
    system = payload["messages"][0]["content"]
    assert json.dumps({"DEFAULT_INPUT_PATH": "configured/ratings.dat", "DEFAULT_EXECUTOR": "hadoop"}, ensure_ascii=False) in system
    effective = {"input_path": api_args.get("input_path", "configured/ratings.dat"),
                 "executor": api_args.get("executor", "hadoop")}
    assert json.dumps(effective, ensure_ascii=False) in system
    for instruction in ("相对配置 DATA_DIR", "不要追问路径、执行器或要求再次确认", "工具显式参数 > API 请求", "不能编造完成指标",
                        "最近已验证的清洗产物", "省略默认运行全部支持算法"):
        assert instruction in system
    tools = {tool["function"]["name"]: tool["function"] for tool in payload["tools"]}
    cleaning = tools["create_task"]
    for capability in ("清洗前评分", "清洗后评分", "隔离无效记录", "Accurate", "Complete", "Unique", "Up-to-date", "Consistent",
                       "counts", "dispositions", "ratings-cleaned.dat", "ratings-quarantine.tsv", "report.json", "不需要单独的评分"):
        assert capability in cleaning["description"]
    assert cleaning["parameters"]["required"] == []
    for key, value in effective.items():
        assert cleaning["parameters"]["properties"][key]["default"] == value
        assert cleaning["parameters"]["properties"][key]["description"]
    for name in ("predict_rating_class", "run_user_clustering", "run_dimensionality_reduction"):
        properties = tools[name]["parameters"]["properties"]
        assert properties["algorithm"]["default"] == "all"
        assert "最近已验证的清洗产物" in properties["input_path"]["description"]


@pytest.mark.parametrize("clean", [False, True])
def test_repeat_uses_original_type_input_and_parameters(routing, monkeypatch, clean):
    session, task = routing
    if clean:
        task = Task(input_path="original.dat", executor="local", rules_version="2.0.0", status=TaskStatus.SUCCESS)
        session.add(task)
        session.commit()
    before = count(session)
    payloads = mock_model(monkeypatch, [operation("repeat_current_task")])
    _, data = agent.route(session, AgentMessage(task_id=task.id, message="重新跑一次"))
    cls = Task if clean else AnalysisTask
    created = session.get(cls, data["id"])
    assert count(session) == before + 1 and len(payloads) == 1 and created.input_path == task.input_path
    if clean:
        assert created.executor == task.executor
    else:
        assert created.task_type == task.task_type and created.parameters == task.parameters


@pytest.mark.parametrize("reply", [
    httpx.ReadTimeout("timeout"), httpx.ConnectError("offline"), {}, {"content": None}, {"tool_calls": None},
    operation("unknown"), operation("get_task", '{"task_id":"other"}'), operation("inspect_current_task", '{"task_id":"other"}'),
    operation("repeat_current_task", '{"task_id":"other"}'), operation("create_task", "null"), operation("create_task", "[]"),
    operation("create_task", "{"), operation("create_task", '{"executor":"bad"}'),
    operation("predict_rating_class", '{"user_id":1}'), operation("predict_rating_class", '{"user_id":true,"movie_id":1}'),
    operation("predict_rating_class", '{"user_id":0,"movie_id":1}'), operation("run_user_clustering", '{"n_clusters":1}'),
    operation("run_dimensionality_reduction", '{"dimensions":21}'),
    {"tool_calls": operation("create_task")["tool_calls"] * 2},
])
def test_invalid_model_never_falls_back_to_creation(routing, monkeypatch, reply):
    session, task = routing
    mock_model(monkeypatch, [reply])
    monkeypatch.setattr(agent, "deterministic", lambda *args: pytest.fail("unsafe fallback"))
    reply, data = agent.route(session, AgentMessage(task_id=task.id, message="请使用默认规则清洗"))
    assert count(session) == 1 and "模型不可用" in reply and "未进行文件证据" in reply and data["id"] == task.id


def test_no_current_task_can_clarify_without_writes(routing, monkeypatch):
    session, _ = routing
    mock_model(monkeypatch, [{"content": "请提供用户和电影 ID，或选择已有任务。"}])
    reply, data = agent.route(session, AgentMessage(message="帮我看看"))
    assert "请提供" in reply and data == {} and count(session) == 1


@pytest.mark.parametrize("reply", [httpx.ReadTimeout("timeout"), operation("create_task", "null"),
                                  operation("repeat_current_task"), operation("inspect_current_task")])
def test_no_current_task_errors_cannot_create(routing, monkeypatch, reply):
    session, _ = routing
    mock_model(monkeypatch, [reply])
    reply, data = agent.route(session, AgentMessage(message="请使用默认规则清洗"))
    assert count(session) == 1 and data == {} and "模型不可用" in reply


@pytest.mark.parametrize("message", ["刚刚预测用户 1768 是否会喜欢电影 10，用了哪两种算法", "刚才那次预测的算法告诉我一下",
                                     "为什么这个概率这样", "重新训练是指什么", "不要预测用户1是否喜欢电影1"])
def test_no_key_followups_never_create(routing, monkeypatch, message):
    session, task = routing
    monkeypatch.delenv("MODEL_API_KEY")
    monkeypatch.setattr(agent.httpx, "post", lambda *args, **kwargs: pytest.fail("no key network call"))
    reply, data = agent.route(session, AgentMessage(task_id=task.id, message=message))
    assert count(session) == 1 and data == agent.task_snapshot(task)
    assert "确定性演示模式" in reply and "未进行文件证据" in reply


def test_no_key_ambiguous_and_missing_task_fail_closed(routing, monkeypatch):
    session, _ = routing
    monkeypatch.delenv("MODEL_API_KEY")
    for request in (AgentMessage(message="清洗是什么"), AgentMessage(task_id="missing", message="预测用户1是否喜欢电影1")):
        _, data = agent.route(session, request)
        assert data == {} and count(session) == 1

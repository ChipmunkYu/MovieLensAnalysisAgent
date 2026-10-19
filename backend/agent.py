from __future__ import annotations

import json
import os
import re

import httpx
from sqlalchemy.orm import Session

from backend.models import Task
from backend.config import default_executor, default_input_path
from backend.rules import load_rules
from backend.schemas import AgentMessage
from backend.service import create_task

TASK_ID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}\b")


def _task(session: Session, task_id: str) -> Task:
    task = session.get(Task, task_id)
    if not task:
        raise ValueError(f"task not found: {task_id}")
    return task


def call_tool(session: Session, name: str, args: dict) -> dict | list:
    if name == "create_task":
        task = create_task(
            session,
            args.get("input_path") or default_input_path(),
            args.get("executor") or default_executor(),
        )
        return {"id": task.id, "status": task.status.value}
    if name == "get_task":
        task = _task(session, args["task_id"])
        return {"id": task.id, "status": task.status.value, "error": task.error}
    if name == "get_result":
        task = _task(session, args["task_id"])
        return {"id": task.id, "status": task.status.value, "result": task.result, "error": task.error}
    if name == "get_rules":
        return load_rules()
    raise ValueError(f"unknown tool: {name}")


def choose_executor(request: AgentMessage, text: str) -> str:
    if request.executor:
        return request.executor
    return "hadoop" if "hadoop" in text else default_executor()


def deterministic(session: Session, request: AgentMessage) -> tuple[str, dict | list]:
    text = request.message.lower()
    match = TASK_ID.search(request.message)
    if match and ("结果" in text or "result" in text):
        data = call_tool(session, "get_result", {"task_id": match.group()})
        return "任务结果查询完成。", data
    if match and ("状态" in text or "status" in text):
        data = call_tool(session, "get_task", {"task_id": match.group()})
        return "任务状态查询完成。", data
    if any(word in text for word in ("清洗", "对比", "clean")):
        data = call_tool(
            session,
            "create_task",
            {
                "input_path": request.input_path or default_input_path(),
                "executor": choose_executor(request, text),
            },
        )
        return "已创建默认清洗与前后评分任务。", data
    if "规则" in text or "rule" in text:
        data = call_tool(session, "get_rules", {})
        return "当前默认清洗规则。", data
    raise ValueError("无法确定操作；请提出清洗请求，或提供任务 ID 查询状态、结果或规则")


def model_route(session: Session, request: AgentMessage) -> tuple[str, dict | list]:
    base_url = os.environ["MODEL_BASE_URL"].rstrip("/")
    tools = []
    specs = {
        "create_task": ("创建默认清洗和评分任务", {"input_path": {"type": "string"}, "executor": {"type": "string", "enum": ["local", "hadoop"]}}, []),
        "get_task": ("查询任务状态", {"task_id": {"type": "string"}}, ["task_id"]),
        "get_result": ("查询任务结果", {"task_id": {"type": "string"}}, ["task_id"]),
        "get_rules": ("查询默认规则", {}, []),
    }
    for name, (description, properties, required) in specs.items():
        tools.append({"type": "function", "function": {"name": name, "description": description, "parameters": {"type": "object", "properties": properties, "required": required}}})
    content = request.message
    if request.input_path:
        content += f"\ninput_path: {request.input_path}"
    if request.executor:
        content += f"\nexecutor: {request.executor}"
    response = httpx.post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {os.environ['MODEL_API_KEY']}"},
        json={"model": os.environ["MODEL_NAME"], "messages": [{"role": "user", "content": content}], "tools": tools, "tool_choice": "required"},
        timeout=30,
    )
    response.raise_for_status()
    calls = response.json()["choices"][0]["message"].get("tool_calls", [])
    if not calls:
        raise ValueError("model did not select a tool")
    call = calls[0]["function"]
    data = call_tool(session, call["name"], json.loads(call["arguments"]))
    return f"已执行工具 {call['name']}。", data


def route(session: Session, request: AgentMessage) -> tuple[str, dict | list]:
    configured = all(os.getenv(name) for name in ("MODEL_API_KEY", "MODEL_BASE_URL", "MODEL_NAME"))
    return model_route(session, request) if configured else deterministic(session, request)

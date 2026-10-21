from __future__ import annotations

import json
import os
import re

import httpx
from sqlalchemy.orm import Session

from backend.models import AnalysisTask, Task
from backend.config import default_executor, default_input_path
from backend.rules import load_rules
from backend.schemas import AgentMessage
from backend.service import create_task
from backend.analysis.service import create_analysis_task

TASK_ID = re.compile(r"\b[0-9a-fA-F]{8}-[0-9a-fA-F-]{27,}\b")
CREATE_HINTS = ("重新清洗", "再清洗", "重新跑", "重新执行", "再跑", "新任务", "重新创建", "再次执行")
DIMENSION_NAMES = {
    "accurate": "准确性",
    "completeness": "完整性",
    "complete": "完整性",
    "unique": "唯一性",
    "up_to_date": "时效性",
    "timeliness": "时效性",
    "consistent": "一致性",
}


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
    analysis_types = {
        "predict_rating_class": "classification",
        "run_user_clustering": "clustering",
        "run_dimensionality_reduction": "dimensionality_reduction",
        "compare_clustering_before_after_reduction": "dimensionality_reduction",
    }
    if name in analysis_types:
        task = create_analysis_task(session, analysis_types[name], args.pop("input_path", None) or default_input_path(), args)
        return {"id": task.id, "task_type": task.task_type, "status": task.status.value, "stage": task.stage,
                "input_data_version": task.input_data_version, "output_version": task.output_version,
                "parameters": task.parameters, "result": task.result, "progress": task.progress,
                "artifacts": [{"name": item["name"], "size": item["size"], "download_url": f"/api/analysis/{task.id}/artifacts/{item['name']}"} for item in task.artifacts or []]}
    raise ValueError(f"unknown tool: {name}")


def is_create_request(message: str) -> bool:
    text = message.lower()
    return any(word in text for word in CREATE_HINTS)


def is_analysis_request(message: str) -> bool:
    text = message.lower()
    if any(word in text for word in ("为什么", "依据", "限制", "多少信息", "结果使用", "当前")):
        return False
    has_ids = re.search(r"用户\s*(?:id\s*)?[：:#]?\s*\d+", text, re.I) and re.search(r"电影\s*(?:id\s*)?[：:#]?\s*\d+", text, re.I)
    return bool((has_ids and any(word in text for word in ("预测", "高分", "评分类别", "喜欢"))) or "聚类" in text or any(word in text for word in ("降维", "pca", "svd")))


def analysis_followup(task: AnalysisTask, message: str) -> tuple[str, dict]:
    result = task.result or {}; text = message.lower()
    data = {"id": task.id, "task_type": task.task_type, "status": task.status.value, "input_data_version": task.input_data_version, "output_version": task.output_version, "result": result}
    if task.status.value != "succeeded":
        return f"分析任务状态为 {task.status.value}。{task.error or ''}", data
    if task.task_type == "dimensionality_reduction" and any(word in text for word in ("信息", "保留", "效果", "变差", "为什么")):
        parts = []
        for item in result.get("results", []):
            comparison = item["clustering_comparison"]
            parts.append(f"{item['algorithm'].upper()} 保留 {item['total_information_retained']:.1%} 信息，轮廓系数由 {comparison['before']['silhouette_score']} 变为 {comparison['after']['silhouette_score']}")
        return "；".join(parts) + "。二维压缩会舍弃部分方差，因此可视化更直观，但群体边界可能变弱。", data
    if task.task_type == "clustering" and any(word in text for word in ("为什么", "群体", "特点", "依据")):
        selected = next((item.get("selected_user") for item in result.get("results", []) if item.get("selected_user")), None)
        if selected:
            profile, summary = selected["profile"], selected["cluster_summary"]
            return f"该判断依据训练期画像：评分 {profile['rating_count']} 次，均分 {profile['average_rating']}，高分比例 {profile['high_rating_ratio']}。所在群体共 {summary['size']} 人，特征是{summary['description']}。", data
    if task.task_type == "classification" and any(word in text for word in ("概率", "依据", "为什么", "指标")):
        features = "、".join(result.get("feature_schema", []))
        return f"概率由训练期之前的用户与电影历史特征计算，包括{features}。测试集指标随结果一并返回；这是预测相关性，不是因果结论。", data
    limitations = "；".join(result.get("limitations") or [])
    return f"该结果使用 {task.input_data_version}，输出版本为 {task.output_version}。{limitations}", data


def model_configured() -> bool:
    return all(os.getenv(name) for name in ("MODEL_API_KEY", "MODEL_BASE_URL", "MODEL_NAME"))


def plain_text_reply(content: str) -> str:
    text = content.replace("\r\n", "\n")
    text = re.sub(r"[*_`#]+", "", text)
    text = re.sub(r"\n[ \t]+\n", "\n\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def task_snapshot(task: Task) -> dict:
    result = task.result or {}
    dispositions = result.get("dispositions") or []
    return {
        "id": task.id,
        "status": task.status.value,
        "input_path": task.input_path,
        "executor": task.executor,
        "rules_version": task.rules_version,
        "error": task.error,
        "rows_before": result.get("rows_before"),
        "rows_after": result.get("rows_after"),
        "dimensions": result.get("dimensions"),
        "dispositions": dispositions,
        "explanation": result.get("explanation"),
        "artifacts": [{"name": item.get("name"), "size": item.get("size")} for item in (task.artifacts or [])],
    }


def followup_via_model(task: Task, message: str) -> str:
    snapshot = task_snapshot(task)
    base_url = os.environ["MODEL_BASE_URL"].rstrip("/")
    payload = {
        "model": os.environ["MODEL_NAME"],
        "thinking": {"type": "disabled"},
        "messages": [
            {
                "role": "system",
                "content": (
                    "你是 MovieLens 数据治理任务的结果解读助手。"
                    "你只能基于用户提供的任务快照回答，不得编造不存在的分数、记录、规则或文件。"
                    "如果快照里没有足够证据，就直接说明“当前结果里没有提供这部分信息”。"
                    "回答使用中文，简洁、直接，优先解释分数变化、处置依据、未解决问题和局限。"
                    "输出应有清晰层次感，使用 2 到 4 段自然段或分行短句来组织说明。"
                    "不要使用 Markdown 语法，不要使用星号加粗、反引号、井号标题。"
                    "可以使用普通中文分行、中文序词或阿拉伯数字分点，但输出必须仍然是纯文本。"
                ),
            },
            {
                "role": "user",
                "content": (
                    f"用户追问：{message}\n"
                    f"当前任务快照：{json.dumps(snapshot, ensure_ascii=False)}"
                ),
            },
        ],
    }
    response = httpx.post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {os.environ['MODEL_API_KEY']}"},
        json=payload,
        timeout=30,
    )
    if response.is_error:
        print(
            "DeepSeek followup failed:",
            json.dumps(
                {
                    "status_code": response.status_code,
                    "url": str(response.request.url),
                    "payload": payload,
                    "response_text": response.text,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    response.raise_for_status()
    content = response.json()["choices"][0]["message"].get("content")
    if not content:
        raise ValueError("model did not return followup content")
    return plain_text_reply(content)


def _dimension_text(result: dict, text: str) -> str | None:
    dimensions = result.get("dimensions") or []
    if not dimensions:
        return None
    selected = None
    for item in dimensions:
        key = str(item.get("dimension", "")).lower()
        zh = DIMENSION_NAMES.get(key, "")
        if key in text or zh in text:
            selected = item
            break
    if not selected:
        return None
    key = str(selected.get("dimension", "")).lower()
    label = DIMENSION_NAMES.get(key, selected.get("dimension", "该维度"))
    before = selected.get("before", 0)
    after = selected.get("after", 0)
    note = selected.get("note")
    reply = f"{label}从 {before} 提升到 {after}。"
    if note:
        reply += f" 当前口径是：{note}。"
    return reply


def followup_reply(task: Task, message: str) -> tuple[str, dict]:
    text = message.lower()
    snapshot = task_snapshot(task)
    if task.status.value != "succeeded":
        status = task.status.value
        if task.error:
            return f"当前任务状态为 {status}，失败原因是：{task.error}", snapshot
        return f"当前任务状态为 {status}，结果尚未生成。", snapshot

    result = task.result or {}
    if model_configured():
        try:
            return followup_via_model(task, message), snapshot
        except Exception:
            pass

    if "规则" in text:
        explanation = result.get("explanation") or "当前结果未附带额外说明。"
        return f"本次任务使用默认规则版本 {task.rules_version}。{explanation}", snapshot

    dimension_text = _dimension_text(result, text)
    if dimension_text:
        return dimension_text, snapshot

    if any(word in text for word in ("记录", "数据量", "多少", "几条", "rows")):
        dispositions = result.get("dispositions") or []
        parts = [f"本次任务把记录数从 {result.get('rows_before', '未知')} 条处理到 {result.get('rows_after', '未知')} 条。"]
        if dispositions:
            parts.append("主要处置包括：" + "；".join(f"{item.get('label')} {item.get('count')} 条" for item in dispositions))
        return "".join(parts), snapshot

    if any(word in text for word in ("异常", "问题", "处置", "依据", "说明", "为什么", "报告")):
        explanation = result.get("explanation") or "当前结果未附带额外说明。"
        dispositions = result.get("dispositions") or []
        if dispositions:
            explanation += " 处置结果包括：" + "；".join(
                f"{item.get('label')} {item.get('count')} 条，原因：{item.get('reason')}" for item in dispositions
            )
        return explanation, snapshot

    dimensions = result.get("dimensions") or []
    summary = "；".join(
        f"{DIMENSION_NAMES.get(str(item.get('dimension', '')).lower(), item.get('dimension', '维度'))} {item.get('before')}→{item.get('after')}"
        for item in dimensions
    )
    explanation = result.get("explanation") or "当前结果未附带额外说明。"
    return f"当前任务已完成。{explanation}" + (f" 五维变化为：{summary}。" if summary else ""), snapshot


def choose_executor(request: AgentMessage, text: str) -> str:
    if request.executor:
        return request.executor
    return "hadoop" if "hadoop" in text else default_executor()


def deterministic(session: Session, request: AgentMessage) -> tuple[str, dict | list]:
    text = request.message.lower()
    user_match = re.search(r"用户\s*(?:id\s*)?[：:#]?\s*(\d+)", text, re.I)
    movie_match = re.search(r"电影\s*(?:id\s*)?[：:#]?\s*(\d+)", text, re.I)
    algorithm = None
    for token, value in (("logistic", "logistic_regression"), ("逻辑回归", "logistic_regression"), ("random forest", "random_forest"), ("随机森林", "random_forest"), ("dbscan", "dbscan"), ("k-means", "kmeans"), ("kmeans", "kmeans"), ("pca", "pca"), ("svd", "svd")):
        if token in text:
            algorithm = value; break
    common = {"input_path": request.input_path or default_input_path(), "algorithm": algorithm}
    if any(word in text for word in ("降维", "pca", "svd", "信息保留")):
        data = call_tool(session, "run_dimensionality_reduction", common | {"n_clusters": 5, "dimensions": 2})
        if not data.get("result"):
            return "降维分析任务已创建，Worker 完成后可查看二维坐标和聚类对照。", data
        result = data["result"]; first = result["results"][0]
        return f"降维分析已完成：{first['algorithm'].upper()} 将 {first['original_dimension']} 维画像压缩到 {first['reduced_dimension']} 维，信息保留率为 {first['total_information_retained']:.1%}。", data
    if any(word in text for word in ("聚类", "群体", "哪一类", "用户群")):
        data = call_tool(session, "run_user_clustering", common | {"user_id": int(user_match.group(1)) if user_match else None, "n_clusters": 5})
        if not data.get("result"):
            return "用户聚类任务已创建，Worker 完成后可查看群体归属和群体摘要。", data
        selected = data["result"]["results"][0].get("selected_user")
        reply = "用户聚类已完成，已生成群体指标与可解释摘要。"
        if selected: reply = f"用户 {selected['user_id']} 属于群体 {selected['cluster_id']}：{selected['cluster_summary']['description']}。"
        return reply, data
    if any(word in text for word in ("预测", "高分", "评分类别", "喜欢")):
        data = call_tool(session, "predict_rating_class", common | {"user_id": int(user_match.group(1)) if user_match else None, "movie_id": int(movie_match.group(1)) if movie_match else None})
        if not data.get("result"):
            return "评分分类任务已创建，Worker 完成后可查看预测类别和高分概率。", data
        values = data["result"]["results"]
        reply = "；".join(f"{item['algorithm']} 预测为{item['prediction_label']}，高分概率 {item['positive_probability']:.1%}" for item in values) + "。"
        return reply, data
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
        "predict_rating_class": ("预测用户是否会给电影打出不低于4分的评分", {"user_id": {"type": "integer"}, "movie_id": {"type": "integer"}, "algorithm": {"type": "string"}, "input_path": {"type": "string"}}, ["user_id", "movie_id"]),
        "run_user_clustering": ("构建用户画像并执行用户聚类", {"user_id": {"type": "integer"}, "algorithm": {"type": "string"}, "n_clusters": {"type": "integer"}, "input_path": {"type": "string"}}, []),
        "run_dimensionality_reduction": ("把用户画像降维并比较聚类效果", {"algorithm": {"type": "string"}, "dimensions": {"type": "integer"}, "n_clusters": {"type": "integer"}, "input_path": {"type": "string"}}, []),
    }
    for name, (description, properties, required) in specs.items():
        tools.append({"type": "function", "function": {"name": name, "description": description, "parameters": {"type": "object", "properties": properties, "required": required}}})
    content = request.message
    if request.input_path:
        content += f"\ninput_path: {request.input_path}"
    if request.executor:
        content += f"\nexecutor: {request.executor}"
    payload = {
        "model": os.environ["MODEL_NAME"],
        "messages": [{"role": "user", "content": content}],
        # DeepSeek rejects `tool_choice: required` when thinking mode is on.
        "thinking": {"type": "disabled"},
        "tools": tools,
        "tool_choice": "required",
    }
    response = httpx.post(
        f"{base_url}/chat/completions",
        headers={"Authorization": f"Bearer {os.environ['MODEL_API_KEY']}"},
        json=payload,
        timeout=30,
    )
    if response.is_error:
        print(
            "DeepSeek request failed:",
            json.dumps(
                {
                    "status_code": response.status_code,
                    "url": str(response.request.url),
                    "payload": payload,
                    "response_text": response.text,
                },
                ensure_ascii=False,
            ),
            flush=True,
        )
    response.raise_for_status()
    calls = response.json()["choices"][0]["message"].get("tool_calls", [])
    if not calls:
        raise ValueError("model did not select a tool")
    call = calls[0]["function"]
    data = call_tool(session, call["name"], json.loads(call["arguments"]))
    return f"已执行工具 {call['name']}。", data


def route(session: Session, request: AgentMessage) -> tuple[str, dict | list]:
    if request.task_id and not is_create_request(request.message) and not is_analysis_request(request.message):
        analysis = session.get(AnalysisTask, request.task_id)
        if analysis:
            return analysis_followup(analysis, request.message)
        task = _task(session, request.task_id)
        return followup_reply(task, request.message)
    try:
        return deterministic(session, request)
    except ValueError:
        pass
    configured = model_configured()
    return model_route(session, request) if configured else deterministic(session, request)

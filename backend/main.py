from __future__ import annotations

from contextlib import asynccontextmanager

from pathlib import Path
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.middleware.cors import CORSMiddleware
from sqlalchemy.orm import Session

from backend.agent import route
from backend.db import get_db, init_db
from backend.models import AnalysisTask, Task, TaskStatus
from backend.config import artifact_dir
from backend.rules import public_rules
from backend.schemas import AgentMessage, AgentReply, AnalysisRequest, AnalysisTaskView, ArtifactList, TaskCreate, TaskResult, TaskView
from backend.service import create_task
from backend.analysis.service import create_analysis_task


@asynccontextmanager
async def lifespan(_: FastAPI):
    init_db()
    yield


app = FastAPI(title="MovieLens Analysis Agent", version="0.1.0", lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


def find_task(task_id: str, session: Session) -> Task:
    task = session.get(Task, task_id)
    if not task:
        raise HTTPException(404, "task not found")
    return task


@app.post("/api/tasks", response_model=TaskView, status_code=201)
def post_task(body: TaskCreate, session: Session = Depends(get_db)):
    return create_task(session, body.input_path, body.executor)


@app.get("/api/tasks/{task_id}", response_model=TaskView)
def get_task(task_id: str, session: Session = Depends(get_db)):
    return find_task(task_id, session)


@app.get("/api/tasks/{task_id}/result", response_model=TaskResult)
def get_result(task_id: str, session: Session = Depends(get_db)):
    task = find_task(task_id, session)
    if task.status != TaskStatus.SUCCESS:
        raise HTTPException(409, f"task is {task.status.value}")
    return task.result


@app.get("/api/tasks/{task_id}/artifacts", response_model=ArtifactList)
def get_artifacts(task_id: str, session: Session = Depends(get_db)):
    task = find_task(task_id, session)
    if task.status != TaskStatus.SUCCESS:
        raise HTTPException(409, f"artifacts unavailable while task is {task.status.value}")
    artifacts = [
        {"name": item["name"], "size": item["size"], "download_url": f"/api/tasks/{task.id}/artifacts/{quote(item['name'])}"}
        for item in task.artifacts or []
    ]
    return {"id": task.id, "artifacts": artifacts}


@app.get("/api/tasks/{task_id}/artifacts/{name}")
def download_artifact(task_id: str, name: str, session: Session = Depends(get_db)):
    task = find_task(task_id, session)
    if task.status != TaskStatus.SUCCESS:
        raise HTTPException(409, "artifact unavailable")
    item = next((value for value in task.artifacts or [] if value.get("name") == name), None)
    if not item:
        raise HTTPException(404, "artifact not found")
    allowed = (artifact_dir() / task.id).resolve()
    path = Path(item["path"]).resolve()
    if allowed not in path.parents or not path.is_file():
        raise HTTPException(404, "artifact not found")
    return FileResponse(path, filename=name)


@app.get("/api/rules/default")
def get_default_rules():
    return public_rules()


@app.post("/api/analysis/classification", response_model=AnalysisTaskView, status_code=201)
def post_classification(body: AnalysisRequest, session: Session = Depends(get_db)):
    return create_analysis_task(session, "classification", body.input_path, body.model_dump(exclude={"input_path"}, exclude_none=True))


@app.post("/api/analysis/clustering", response_model=AnalysisTaskView, status_code=201)
def post_clustering(body: AnalysisRequest, session: Session = Depends(get_db)):
    return create_analysis_task(session, "clustering", body.input_path, body.model_dump(exclude={"input_path"}, exclude_none=True))


@app.post("/api/analysis/reduction", response_model=AnalysisTaskView, status_code=201)
def post_reduction(body: AnalysisRequest, session: Session = Depends(get_db)):
    return create_analysis_task(session, "dimensionality_reduction", body.input_path, body.model_dump(exclude={"input_path"}, exclude_none=True))


@app.get("/api/analysis/{task_id}", response_model=AnalysisTaskView)
def get_analysis(task_id: str, session: Session = Depends(get_db)):
    task = session.get(AnalysisTask, task_id)
    if not task:
        raise HTTPException(404, "analysis task not found")
    return task


@app.get("/api/analysis/{task_id}/artifacts/{name}")
def download_analysis_artifact(task_id: str, name: str, session: Session = Depends(get_db)):
    task = session.get(AnalysisTask, task_id)
    if not task or task.status != TaskStatus.SUCCESS:
        raise HTTPException(404, "analysis artifact not found")
    item = next((value for value in task.artifacts or [] if value.get("name") == name), None)
    if not item:
        raise HTTPException(404, "analysis artifact not found")
    allowed, path = (artifact_dir() / "analysis" / task.id).resolve(), Path(item["path"]).resolve()
    if allowed not in path.parents or not path.is_file():
        raise HTTPException(404, "analysis artifact not found")
    return FileResponse(path, filename=name)


@app.post("/api/agent/messages", response_model=AgentReply)
def agent_message(body: AgentMessage, session: Session = Depends(get_db)):
    try:
        reply, data = route(session, body)
        task_id = data.get("id") if isinstance(data, dict) and isinstance(data.get("id"), str) else None
        return AgentReply(reply=reply, task_id=task_id, data=data)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc

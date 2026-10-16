from __future__ import annotations

from datetime import datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from backend.config import default_input_path
from backend.models import TaskStatus


class TaskCreate(BaseModel):
    input_path: str = Field(default_factory=default_input_path, min_length=1)
    executor: Literal["local", "hadoop"] = "local"


class TaskView(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: str
    input_path: str
    executor: str
    rules_version: str
    status: TaskStatus
    error: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class AgentMessage(BaseModel):
    message: str = Field(min_length=1)
    input_path: str | None = None


class AgentReply(BaseModel):
    reply: str
    task_id: str | None = None
    data: dict | list | None = None


class DimensionScore(BaseModel):
    dimension: str
    before: float
    after: float
    note: str


class Disposition(BaseModel):
    label: str
    count: int
    reason: str


class TaskResult(BaseModel):
    dimensions: list[DimensionScore]
    rows_before: int
    rows_after: int
    dispositions: list[Disposition]
    explanation: str
    rule_version: str


class ArtifactView(BaseModel):
    name: str
    size: int
    download_url: str


class ArtifactList(BaseModel):
    id: str
    artifacts: list[ArtifactView]

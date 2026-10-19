from __future__ import annotations

import enum
import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, DateTime, Enum, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from backend.db import Base

STAGE_PROGRESS = {
    "hadoop_preflight": 5,
    "upload": 15,
    "score_before": 30,
    "clean": 55,
    "quarantine": 65,
    "score_after": 75,
    "download": 90,
    "publish": 100,
}


class TaskStatus(str, enum.Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCESS = "succeeded"
    FAILED = "failed"


def now() -> datetime:
    return datetime.now(timezone.utc)


class Task(Base):
    __tablename__ = "tasks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    input_path: Mapped[str] = mapped_column(Text)
    executor: Mapped[str] = mapped_column(String(16), default="local")
    rules_version: Mapped[str] = mapped_column(String(32))
    status: Mapped[TaskStatus] = mapped_column(Enum(TaskStatus), default=TaskStatus.QUEUED, index=True)
    stage: Mapped[str | None] = mapped_column(String(32), nullable=True)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    artifacts: Mapped[list | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=now)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    @property
    def progress(self) -> int:
        if self.status == TaskStatus.SUCCESS:
            return 100
        return STAGE_PROGRESS.get(self.stage or "", 0)

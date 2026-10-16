from __future__ import annotations

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from backend.executors import run_hadoop, run_local
from backend.models import Task, TaskStatus, now
from backend.rules import load_rules


def create_task(session: Session, input_path: str, executor: str = "local") -> Task:
    task = Task(input_path=input_path, executor=executor, rules_version=load_rules()["version"])
    session.add(task)
    session.commit()
    session.refresh(task)
    return task


def claim_one(session: Session) -> Task | None:
    task_id = session.scalar(select(Task.id).where(Task.status == TaskStatus.QUEUED).order_by(Task.created_at).limit(1))
    if not task_id:
        return None
    claimed = session.execute(
        update(Task).where(Task.id == task_id, Task.status == TaskStatus.QUEUED).values(status=TaskStatus.RUNNING, started_at=now())
    )
    session.commit()
    return session.get(Task, task_id) if claimed.rowcount else None


def work_once(session: Session) -> Task | None:
    task = claim_one(session)
    if not task:
        return None
    try:
        rules = load_rules()
        runner = run_local if task.executor == "local" else run_hadoop
        task.result, task.artifacts = runner(task.id, task.input_path, rules)
        task.status = TaskStatus.SUCCESS
    except Exception as exc:  # task failures belong in persistent task state
        task.status = TaskStatus.FAILED
        task.error = str(exc)
    task.finished_at = now()
    session.commit()
    session.refresh(task)
    return task

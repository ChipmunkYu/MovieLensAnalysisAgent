from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import DeclarativeBase, sessionmaker

from backend.config import database_url


class Base(DeclarativeBase):
    pass


def make_engine(url: str | None = None):
    value = url or database_url()
    return create_engine(value, connect_args={"check_same_thread": False} if value.startswith("sqlite") else {})


engine = make_engine()
SessionLocal = sessionmaker(engine, expire_on_commit=False)


def init_db() -> None:
    from backend import models  # noqa: F401

    Base.metadata.create_all(engine)


def get_db():
    with SessionLocal() as session:
        yield session

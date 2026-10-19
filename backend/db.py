from __future__ import annotations

from sqlalchemy import create_engine, inspect, text
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
    _add_missing_columns()


def _add_missing_columns() -> None:
    """Apply the few additive migrations needed by existing local databases."""
    columns = {column["name"] for column in inspect(engine).get_columns("tasks")}
    if "stage" not in columns:
        with engine.begin() as connection:
            connection.execute(text("ALTER TABLE tasks ADD COLUMN stage VARCHAR(32)"))


def get_db():
    with SessionLocal() as session:
        yield session

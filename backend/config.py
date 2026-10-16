from __future__ import annotations

import os
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
load_dotenv(ROOT / ".env")


def env_path(name: str, default: str) -> Path:
    value = Path(os.getenv(name, default)).expanduser()
    return value if value.is_absolute() else (ROOT / value).resolve()


def database_url() -> str:
    return os.getenv("DATABASE_URL", f"sqlite:///{ROOT / 'movie_tasks.db'}")


def data_dir() -> Path:
    return env_path("DATA_DIR", "data")


def default_input_path() -> str:
    return os.getenv("DEFAULT_INPUT_PATH", "sample/ratings.dat")


def artifact_dir() -> Path:
    return env_path("ARTIFACT_DIR", "data/artifacts")


def rules_file() -> Path:
    return env_path("RULES_FILE", "rules/default.json")

"""Drive the Hadoop path against a pure-Python fake cluster.

There is no Hadoop on the development machine, so the fake emulates the exact
CLI surface the driver uses (``hadoop version``, ``hadoop jar``, ``hdfs dfs``)
and really executes the Streaming mapper/reducer code.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest
from sqlalchemy.orm import sessionmaker

from backend import hadoop_runner
from backend.db import Base, make_engine
from backend.models import TaskStatus
from backend.service import create_task, work_once

ROOT = Path(__file__).resolve().parents[2]
REAL_RUN = subprocess.run
SAMPLE = (
    "1::10::4::946684800\n"
    "1::10::5::946771200\n"
    "2::20::9::946857600\n"
    "bad row\n"
    "3::30::3::978307200\n"
    "4::40::4.0::1009843200\n"
    "5::50::0::1041379200\n"
    "6::60::5::100\n"
)


@pytest.fixture
def cluster(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    data = tmp_path / "data"
    data.mkdir()
    (data / "ratings.dat").write_bytes(SAMPLE.encode("latin-1"))
    monkeypatch.setenv("DATA_DIR", str(data))
    monkeypatch.setenv("DEFAULT_INPUT_PATH", "ratings.dat")
    monkeypatch.setenv("ARTIFACT_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("HADOOP_BIN", "hadoop")
    monkeypatch.setenv("HDFS_BIN", "hdfs")
    monkeypatch.setenv("STREAMING_PYTHON", sys.executable)
    monkeypatch.setenv("HDFS_WORK_DIR", "/movielens/work")
    monkeypatch.setenv("DATA_VERSION", "ml1m-test-v1")
    for name in ("T1", "T2", "HADOOP_JOB_REDUCES", "HADOOP_HOME"):
        monkeypatch.delenv(name, raising=False)
    jar = tmp_path / "hadoop-streaming-test.jar"
    jar.write_bytes(b"")
    monkeypatch.setenv("HADOOP_STREAMING_JAR", str(jar))

    hdfs_root = tmp_path / "hdfs"
    hdfs_root.mkdir()

    def hdfs_path(value: str) -> Path:
        return hdfs_root / value.lstrip("/").replace("/", os.sep)

    def fake_run(command, capture_output=False, text=False, timeout=None, cwd=None, **_):
        stage_cwd = Path(cwd) if cwd else Path.cwd()

        if len(command) > 1 and command[1] == "version":
            return subprocess.CompletedProcess(command, 0, "fake hadoop\n", "")

        if command[0] == "hdfs":
            args = command[2:]
            if args[0] == "-mkdir":
                hdfs_path(args[-1]).mkdir(parents=True, exist_ok=True)
            elif args[0] == "-rm":
                target = hdfs_path(args[-1])
                if target.is_dir():
                    shutil.rmtree(target)
                elif target.exists():
                    target.unlink()
            elif args[0] == "-put":
                source, target = Path(args[-2]), hdfs_path(args[-1])
                target.parent.mkdir(parents=True, exist_ok=True)
                if source.is_dir():
                    shutil.copytree(source, target, dirs_exist_ok=True)
                else:
                    shutil.copy2(source, target)
            elif args[0] == "-get":
                source, target = hdfs_path(args[-2]), Path(args[-1])
                if target.is_dir():
                    shutil.rmtree(target)
                elif target.exists():
                    target.unlink()
                shutil.copytree(source, target)
            return subprocess.CompletedProcess(command, 0, "", "")

        flags: dict[str, str] = {}
        index = 2
        while index < len(command):
            token = command[index]
            if token == "-D":
                index += 2
                continue
            if token in ("-file", "-mapper", "-reducer", "-input", "-output"):
                flags[token] = command[index + 1]
                index += 2
                continue
            index += 1

        lines: list[str] = []
        for path in sorted(hdfs_path(flags["-input"]).rglob("*")):
            if path.is_file():
                lines.extend(path.read_text(encoding="latin-1").splitlines())

        mapper = Path(flags["-mapper"].split()[-1]).name
        reducer = Path(flags["-reducer"].split()[-1]).name
        mapped = REAL_RUN(
            [sys.executable, mapper],
            input=("\n".join(lines) + "\n").encode("latin-1"),
            capture_output=True,
            cwd=str(stage_cwd),
        )
        if mapped.returncode:
            return subprocess.CompletedProcess(command, mapped.returncode, b"", mapped.stderr)
        shuffled = ("\n".join(sorted(mapped.stdout.decode("latin-1").splitlines())) + "\n").encode("latin-1")
        reduced = REAL_RUN(
            [sys.executable, reducer],
            input=shuffled,
            capture_output=True,
            cwd=str(stage_cwd),
        )
        if reduced.returncode:
            return subprocess.CompletedProcess(command, reduced.returncode, b"", reduced.stderr)

        output = hdfs_path(flags["-output"])
        output.mkdir(parents=True, exist_ok=True)
        (output / "part-00000").write_bytes(reduced.stdout)
        return subprocess.CompletedProcess(command, 0, "", "")

    monkeypatch.setattr(hadoop_runner.subprocess, "run", fake_run)

    engine = make_engine(f"sqlite:///{tmp_path / 'test.db'}")
    Base.metadata.create_all(engine)
    Session = sessionmaker(engine, expire_on_commit=False)
    yield Session, tmp_path


def test_hadoop_pipeline_matches_local_executor(cluster):
    Session, tmp_path = cluster
    with Session() as session:
        hadoop_task = create_task(session, "ratings.dat", "hadoop")
        hadoop_task = work_once(session)
        assert hadoop_task.status == TaskStatus.SUCCESS, hadoop_task.error
        hadoop_result = hadoop_task.result
        hadoop_artifacts = hadoop_task.artifacts

        local_task = create_task(session, "ratings.dat", "local")
        local_task = work_once(session)
        assert local_task.status == TaskStatus.SUCCESS, local_task.error

    assert hadoop_result["dimensions"] == local_task.result["dimensions"]
    assert hadoop_result["rows_before"] == local_task.result["rows_before"] == 8
    assert hadoop_result["rows_after"] == local_task.result["rows_after"] == 3
    assert hadoop_result["dispositions"] == local_task.result["dispositions"]
    assert hadoop_result["repairs"] == {"records": 1, "by_kind": {"number_format": 1}}
    assert hadoop_result["quarantine"] == {"records": 4, "by_reason": {"format": 1, "range": 2, "timestamp": 1}}
    assert hadoop_result["data_version"] == "ml1m-test-v1"
    assert hadoop_result["output_data_version"] == "ml1m-test-v1-clean"
    assert hadoop_result["t1"] is None and hadoop_result["t2"] is None
    assert hadoop_result["hdfs_paths"]["clean"].endswith("/clean")

    cleaned = Path(hadoop_artifacts[0]["path"]).read_text(encoding="iso-8859-1")
    assert sorted(cleaned.splitlines()) == [
        "1::10::5::946771200",
        "3::30::3::978307200",
        "4::40::4::1009843200",
    ]
    quarantine = Path(hadoop_artifacts[1]["path"]).read_text(encoding="iso-8859-1").splitlines()
    assert len(quarantine) == 4
    assert {artifact["name"] for artifact in hadoop_artifacts} == {
        "ratings-cleaned.dat",
        "ratings-quarantine.tsv",
        "report.json",
    }
    assert (tmp_path / "hdfs" / "movielens" / "work" / hadoop_task.id / "report.json").is_file()
    assert (tmp_path / "hdfs" / "movielens" / "work" / hadoop_task.id / "quarantine").is_dir()


def test_missing_streaming_jar_fails_with_actionable_message(cluster, monkeypatch: pytest.MonkeyPatch):
    Session, tmp_path = cluster
    monkeypatch.setenv("HADOOP_STREAMING_JAR", str(tmp_path / "missing.jar"))
    with Session() as session:
        task = create_task(session, "ratings.dat", "hadoop")
        completed = work_once(session)
        assert completed.status == TaskStatus.FAILED
        assert "HADOOP_STREAMING_JAR" in completed.error

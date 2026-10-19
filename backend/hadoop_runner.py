"""Submit the MovieLens cleaning pipeline to a real Hadoop cluster.

The driver uses only the standard Hadoop CLI (``hadoop``/``hdfs``) so it works
against any cluster the user can already reach with those clients. It never
fabricates results: every failure is raised with the stage and command output.
"""

from __future__ import annotations

import glob
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

from backend import scoring
from backend.config import artifact_dir, data_version, time_boundary

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SCRIPTS = ROOT / "hadoop" / "streaming"
SCRIPT_NAMES = ("score_mapper.py", "score_reducer.py", "clean_mapper.py", "clean_reducer.py")


class HadoopError(RuntimeError):
    """Raised for any Hadoop-side failure; the message is shown to the user."""


def _env(name: str, default: str) -> str:
    return os.getenv(name, default)


def _timeout() -> int:
    return int(_env("HADOOP_JOB_TIMEOUT", "1800"))


def _tail(text: str | None, limit: int = 2000) -> str:
    value = (text or "").strip()
    return value[-limit:]


def _run(command: list[str], stage: str, timeout: int | None = None, cwd: Path | None = None):
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=timeout or _timeout(),
            cwd=str(cwd) if cwd else None,
        )
    except FileNotFoundError as exc:
        raise HadoopError(
            f"[{stage}] 找不到命令 '{command[0]}'。请确认已安装 Hadoop 客户端，"
            f"并检查 HADOOP_BIN / HDFS_BIN 或 PATH。"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise HadoopError(f"[{stage}] 命令超时（>{timeout or _timeout()} 秒）：{' '.join(command)}") from exc


def _check(command: list[str], stage: str, timeout: int | None = None) -> str:
    completed = _run(command, stage, timeout=timeout)
    if completed.returncode:
        raise HadoopError(
            f"[{stage}] 命令失败（退出码 {completed.returncode}）：{' '.join(command)}\n"
            f"{_tail(completed.stderr or completed.stdout)}"
        )
    return completed.stdout


def _try(command: list[str], stage: str) -> None:
    """Run a best-effort command (for example removing a directory that may not exist)."""
    try:
        _run(command, stage, timeout=120)
    except HadoopError:
        pass


def _find_streaming_jar(hadoop: str) -> str:
    explicit = os.getenv("HADOOP_STREAMING_JAR")
    if explicit:
        if not Path(explicit).is_file():
            raise HadoopError(
                f"HADOOP_STREAMING_JAR 指向的文件不存在：{explicit}。"
                "请改为集群上 hadoop-streaming-*.jar 的真实路径。"
            )
        return explicit

    home = os.getenv("HADOOP_HOME")
    if home:
        matches = sorted(glob.glob(str(Path(home) / "share" / "hadoop" / "tools" / "lib" / "hadoop-streaming-*.jar")))
        if matches:
            return matches[-1]

    completed = _run([hadoop, "classpath"], "hadoop_preflight", timeout=120)
    if completed.returncode == 0:
        for entry in completed.stdout.strip().split(os.pathsep):
            if "hadoop-streaming" in entry and entry.endswith(".jar"):
                return entry

    raise HadoopError(
        "找不到 hadoop-streaming jar。请设置 HADOOP_STREAMING_JAR 指向 "
        "$HADOOP_HOME/share/hadoop/tools/lib/hadoop-streaming-*.jar，"
        "或确认 HADOOP_HOME 正确。"
    )


def preflight() -> tuple[str, str, str]:
    hadoop = _env("HADOOP_BIN", "hadoop")
    hdfs = _env("HDFS_BIN", "hdfs")
    _check([hadoop, "version"], "hadoop_preflight", timeout=120)
    _check([hdfs, "version"], "hadoop_preflight", timeout=120)
    return hadoop, hdfs, _find_streaming_jar(hadoop)


def _stage(workdir: Path, rules: dict) -> Path:
    scripts = Path(_env("HADOOP_STREAMING_SCRIPTS", str(DEFAULT_SCRIPTS)))
    staging = workdir / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    for name in SCRIPT_NAMES:
        source = scripts / name
        if not source.is_file():
            raise HadoopError(f"缺少 Streaming 脚本：{source}")
        shutil.copy2(source, staging / name)
    shutil.copy2(Path(scoring.__file__), staging / "scoring.py")
    (staging / "rules.json").write_text(json.dumps(rules, ensure_ascii=False, indent=2), encoding="utf-8")
    return staging


def _job(
    *,
    hadoop: str,
    jar: str,
    staging: Path,
    mapper: str,
    reducer: str,
    source: str,
    output: str,
    name: str,
    stage: str,
    progress,
) -> None:
    if progress:
        progress(stage)
    python = _env("STREAMING_PYTHON", "python3")
    command = [hadoop, "jar", jar, "-D", f"mapreduce.job.name={name}"]
    reduces = os.getenv("HADOOP_JOB_REDUCES")
    if reduces:
        command += ["-D", f"mapreduce.job.reduces={reduces}"]
    for filename in ("scoring.py", "rules.json", mapper, reducer):
        command += ["-file", filename]
    command += [
        "-mapper", f"{python} {mapper}",
        "-reducer", f"{python} {reducer}",
        "-input", source,
        "-output", output,
    ]
    completed = _run(command, stage, cwd=staging)
    if completed.returncode:
        raise HadoopError(
            f"[{stage}] MapReduce 作业失败（退出码 {completed.returncode}）：{name}\n"
            f"{_tail(completed.stderr or completed.stdout)}"
        )


def _hdfs(hdfs: str, *args: str, stage: str) -> None:
    _check([hdfs, "dfs", *args], stage)


def _hdfs_try(hdfs: str, *args: str, stage: str) -> None:
    _try([hdfs, "dfs", *args], stage)


def _read_counts(directory: Path) -> dict:
    counts: dict = {"total": 0, "valid": 0, "complete": 0, "consistent": 0, "timely": 0, "unique": 0, "bad": {}}
    parts = sorted(directory.glob("part-*"))
    if not parts:
        raise HadoopError(f"Hadoop 作业没有产生 part 输出目录：{directory}")
    for part in parts:
        with part.open(encoding="utf-8") as stream:
            for raw in stream:
                fields = raw.rstrip("\r\n").split("\t")
                if len(fields) != 3 or fields[0] != "M":
                    continue
                key, value = fields[1], int(fields[2])
                if key.startswith("__bad:"):
                    reason = key[len("__bad:"):]
                    counts["bad"][reason] = counts["bad"].get(reason, 0) + value
                elif key == "__unique":
                    counts["unique"] += value
                elif key.startswith("__"):
                    name = key[len("__"):]
                    counts[name] = counts.get(name, 0) + value
    return counts


def _get(hdfs: str, source: str, destination: Path, stage: str) -> Path:
    _hdfs(hdfs, "-get", source, str(destination), stage=stage)
    if not destination.is_dir():
        raise HadoopError(f"[{stage}] 未取回输出目录：{source}")
    return destination


def run_hadoop(task_id: str, input_value: str, rules: dict, progress=None) -> tuple[dict, list]:
    from backend.executors import resolve_input

    hadoop, hdfs, jar = preflight()
    source = resolve_input(input_value)
    work_root = _env("HDFS_WORK_DIR", "/movielens/work").rstrip("/")
    base = f"{work_root}/{task_id}"
    paths = {name: f"{base}/{name}" for name in ("raw", "before", "clean", "after")}

    with tempfile.TemporaryDirectory(prefix=f"movielens-{task_id[:8]}-") as temp:
        workdir = Path(temp)
        staging = _stage(workdir, rules)

        if progress:
            progress("upload")
        for name in ("before", "clean", "after"):
            _hdfs_try(hdfs, "-rm", "-r", "-f", paths[name], stage="upload")
        _hdfs(hdfs, "-mkdir", "-p", paths["raw"], stage="upload")
        _hdfs(hdfs, "-rm", "-r", "-f", paths["raw"], stage="upload")
        _hdfs(hdfs, "-mkdir", "-p", paths["raw"], stage="upload")
        _hdfs(hdfs, "-put", "-f", str(source), f"{paths['raw']}/{source.name}", stage="upload")

        _job(
            hadoop=hadoop, jar=jar, staging=staging,
            mapper="score_mapper.py", reducer="score_reducer.py",
            source=paths["raw"], output=paths["before"],
            name=f"movielens-score-before-{task_id[:8]}", stage="score_before", progress=progress,
        )
        _job(
            hadoop=hadoop, jar=jar, staging=staging,
            mapper="clean_mapper.py", reducer="clean_reducer.py",
            source=paths["raw"], output=paths["clean"],
            name=f"movielens-clean-{task_id[:8]}", stage="clean", progress=progress,
        )
        _job(
            hadoop=hadoop, jar=jar, staging=staging,
            mapper="score_mapper.py", reducer="score_reducer.py",
            source=paths["clean"], output=paths["after"],
            name=f"movielens-score-after-{task_id[:8]}", stage="score_after", progress=progress,
        )

        if progress:
            progress("download")
        before_dir = _get(hdfs, paths["before"], workdir / "before", "download")
        after_dir = _get(hdfs, paths["after"], workdir / "after", "download")
        clean_dir = _get(hdfs, paths["clean"], workdir / "clean", "download")

        before = scoring.counts_to_metrics(_read_counts(before_dir))
        after = scoring.counts_to_metrics(_read_counts(after_dir))

        output_dir = artifact_dir() / task_id
        output_dir.mkdir(parents=True, exist_ok=True)
        cleaned = output_dir / "ratings-cleaned.dat"
        with cleaned.open("w", encoding="iso-8859-1", newline="\n") as stream:
            for part in sorted(clean_dir.glob("part-*")):
                text = part.read_text(encoding="iso-8859-1")
                if text and not text.endswith("\n"):
                    text += "\n"
                stream.write(text)

        result = {
            "dimensions": scoring.dimensions_from(before, after, rules),
            "rows_before": before["total"],
            "rows_after": after["total"],
            "dispositions": scoring.dispositions_from(before, after),
            "explanation": scoring.explain(before, after),
            "rule_version": rules["version"],
            "data_version": data_version(),
            "output_data_version": f"{data_version()}-clean",
            "t1": time_boundary("T1"),
            "t2": time_boundary("T2"),
            "hdfs_paths": paths,
        }

        report = output_dir / "report.json"
        report.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")

        if progress:
            progress("publish")
        _try([hdfs, "dfs", "-put", "-f", str(report), f"{base}/report.json"], "publish")

    artifacts = [
        {"name": cleaned.name, "path": str(cleaned), "size": cleaned.stat().st_size},
        {"name": report.name, "path": str(report), "size": report.stat().st_size},
    ]
    return result, artifacts

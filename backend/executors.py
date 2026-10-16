from __future__ import annotations

import json
import math
import os
import shlex
import shutil
import subprocess
from pathlib import Path

from backend.config import artifact_dir, data_dir, rules_file


def resolve_input(value: str) -> Path:
    root = data_dir().resolve()
    candidate = Path(value).expanduser()
    candidate = candidate.resolve() if candidate.is_absolute() else (root / candidate).resolve()
    if candidate != root and root not in candidate.parents:
        raise ValueError(f"input_path must be inside DATA_DIR ({root})")
    if not candidate.is_file():
        raise FileNotFoundError(f"input file not found: {candidate}")
    return candidate


def _parse(line: str, rules: dict) -> tuple[int, int, float, int]:
    fields = line.rstrip("\r\n").split(rules["format"]["delimiter"])
    if len(fields) != 4:
        raise ValueError("expected four fields")
    user_id, movie_id, rating, timestamp = int(fields[0]), int(fields[1]), float(fields[2]), int(fields[3])
    validation = rules["validation"]
    if validation["positive_ids"] and (user_id <= 0 or movie_id <= 0):
        raise ValueError("ids must be positive")
    if not math.isfinite(rating) or not validation["rating_min"] <= rating <= validation["rating_max"]:
        raise ValueError("rating outside allowed range")
    if validation["non_negative_timestamp"] and timestamp < 0:
        raise ValueError("timestamp must be non-negative")
    return user_id, movie_id, rating, timestamp


def _score(part: int, whole: int, scale: int) -> float:
    return round(scale * part / whole, 2) if whole else 0.0


def run_local(task_id: str, input_value: str, rules: dict) -> tuple[dict, list]:
    source = resolve_input(input_value)
    rows: dict[tuple[int, int], tuple[int, int, float, int]] = {}
    total = valid = complete = consistent = timely = 0
    delimiter = rules["format"]["delimiter"]
    timely_min = rules["scoring"]["timely_timestamp_min"]
    with source.open(encoding="iso-8859-1") as stream:
        for line in stream:
            total += 1
            fields = line.rstrip("\r\n").split(delimiter)
            is_complete = len(fields) == 4 and all(fields)
            complete += is_complete
            try:
                parsed = (int(fields[0]), int(fields[1]), float(fields[2]), int(fields[3])) if is_complete else None
                is_consistent = parsed is not None and math.isfinite(parsed[2])
            except (ValueError, OverflowError):
                parsed, is_consistent = None, False
            consistent += is_consistent
            timely += bool(is_consistent and parsed and parsed[3] >= timely_min)
            try:
                row = _parse(line, rules)
            except (ValueError, OverflowError):
                continue
            valid += 1
            key = row[0], row[1]
            if key not in rows or row[3] >= rows[key][3]:
                rows[key] = row

    output_dir = artifact_dir() / task_id
    output_dir.mkdir(parents=True, exist_ok=True)
    cleaned = output_dir / "ratings-cleaned.dat"
    ordered = sorted(rows.values(), key=lambda row: (row[0], row[1]))
    with cleaned.open("w", encoding="iso-8859-1", newline="\n") as stream:
        for user_id, movie_id, rating, timestamp in ordered:
            stream.write(delimiter.join((str(user_id), str(movie_id), f"{rating:g}", str(timestamp))) + "\n")

    scale = rules["scoring"]["dimension_scale"]
    after_score = float(scale) if ordered else 0.0
    dimensions = [
        {"dimension": "accurate", "before": _score(valid, total, scale), "after": after_score, "note": "符合 ID、评分和时间戳校验的记录比例"},
        {"dimension": "completeness", "before": _score(complete, total, scale), "after": after_score, "note": "四个字段均存在的记录比例"},
        {"dimension": "unique", "before": _score(len(rows), valid, scale), "after": after_score, "note": "按 user_id 与 movie_id 去重后的比例"},
        {"dimension": "up_to_date", "before": _score(timely, consistent, scale), "after": after_score, "note": f"时间戳不早于 {timely_min} 的可解析记录比例"},
        {"dimension": "consistent", "before": _score(consistent, total, scale), "after": after_score, "note": "字段类型符合 MovieLens 格式的记录比例"},
    ]
    result = {
        "dimensions": dimensions,
        "rows_before": total,
        "rows_after": len(ordered),
        "dispositions": [
            {"label": "拒绝无效记录", "count": total - valid, "reason": "格式、类型或取值不符合默认规则"},
            {"label": "删除重复记录", "count": valid - len(rows), "reason": "相同用户和电影仅保留最新时间戳"},
            {"label": "保留有效记录", "count": len(rows), "reason": "通过校验并完成去重"},
        ],
        "explanation": f"从 {total} 条实际输入中校验并按用户、电影去重，得到 {len(ordered)} 条可用记录。",
        "rule_version": rules["version"],
    }
    result_file = output_dir / "result.json"
    result_file.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    artifacts = [
        {"name": cleaned.name, "path": str(cleaned), "size": cleaned.stat().st_size},
        {"name": result_file.name, "path": str(result_file), "size": result_file.stat().st_size},
    ]
    return result, artifacts


def hadoop_preflight() -> str:
    binary = os.getenv("HADOOP_BIN", "hadoop")
    resolved = shutil.which(binary)
    if not resolved:
        raise RuntimeError(f"Hadoop executable '{binary}' was not found in PATH")
    check = subprocess.run([resolved, "version"], capture_output=True, text=True, timeout=15)
    if check.returncode:
        raise RuntimeError(f"Hadoop preflight failed: {(check.stderr or check.stdout).strip()}")
    return resolved


def run_hadoop(task_id: str, input_value: str, rules: dict) -> tuple[dict, list]:
    template = os.getenv("HADOOP_COMMAND")
    if not template:
        raise RuntimeError("HADOOP_COMMAND is required for the Hadoop executor")
    source = resolve_input(input_value)
    output_dir = artifact_dir() / task_id
    output_dir.mkdir(parents=True, exist_ok=True)
    cleaned, result_file = output_dir / "ratings-cleaned.dat", output_dir / "result.json"
    values = {"input": source, "output": cleaned, "result": result_file, "rules": rules_file()}
    command = [part.format(**values) for part in shlex.split(template, posix=os.name != "nt")]
    if command[0] == os.getenv("HADOOP_BIN", "hadoop"):
        command[0] = hadoop_preflight()
    elif not shutil.which(command[0]):
        raise RuntimeError(f"Hadoop command client '{command[0]}' was not found in PATH")

    preflight = os.getenv("HADOOP_PREFLIGHT_COMMAND")
    if preflight:
        check = subprocess.run(shlex.split(preflight, posix=os.name != "nt"), capture_output=True, text=True, timeout=15)
        if check.returncode:
            raise RuntimeError(f"Hadoop remote preflight failed: {(check.stderr or check.stdout).strip()}")
    completed = subprocess.run(command, capture_output=True, text=True)
    if completed.returncode:
        raise RuntimeError(f"Hadoop command failed ({completed.returncode}): {(completed.stderr or completed.stdout).strip()}")
    if not cleaned.is_file() or not result_file.is_file():
        raise RuntimeError("Hadoop command did not create the configured output and result files")
    result = json.loads(result_file.read_text(encoding="utf-8"))
    return result, [{"name": path.name, "path": str(path), "size": path.stat().st_size} for path in (cleaned, result_file)]

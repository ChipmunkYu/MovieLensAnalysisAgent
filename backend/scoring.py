"""Shared scoring logic for the local executor and Hadoop Streaming jobs.

This module must stay dependency-free and must not import other ``backend``
modules: it is shipped to cluster nodes with ``-file`` and imported there as a
top-level ``scoring`` module.
"""

from __future__ import annotations

import math

DIMENSIONS = ("accurate", "completeness", "unique", "up_to_date", "consistent")
COUNTER_KEYS = ("total", "valid", "complete", "consistent", "timely", "unique")


def parse_row(line: str, rules: dict) -> dict | None:
    """Parse and validate one ratings line. Returns None when the row is invalid."""
    fields = line.rstrip("\r\n").split(rules["format"]["delimiter"])
    if len(fields) != 4:
        return None
    try:
        user, movie, rating, timestamp = int(fields[0]), int(fields[1]), float(fields[2]), int(fields[3])
    except (ValueError, OverflowError):
        return None
    validation = rules["validation"]
    if validation["positive_ids"] and (user <= 0 or movie <= 0):
        return None
    if not math.isfinite(rating) or not validation["rating_min"] <= rating <= validation["rating_max"]:
        return None
    if validation["non_negative_timestamp"] and timestamp < 0:
        return None
    return {"user": user, "movie": movie, "rating": rating, "timestamp": timestamp}


def classify(line: str, rules: dict) -> dict:
    """Classify one raw line without keeping any state."""
    fields = line.rstrip("\r\n").split(rules["format"]["delimiter"])
    complete = len(fields) == 4 and all(field.strip() for field in fields)
    parsed = None
    if complete:
        try:
            parsed = (int(fields[0]), int(fields[1]), float(fields[2]), int(fields[3]))
            if not math.isfinite(parsed[2]):
                parsed = None
        except (ValueError, OverflowError):
            parsed = None
    row = parse_row(line, rules)
    consistent = parsed is not None
    timely = parsed is not None and parsed[3] >= rules["scoring"]["timely_timestamp_min"]
    bad = None if row is not None else ("range" if consistent else "format")
    return {
        "complete": complete,
        "consistent": consistent,
        "valid": row is not None,
        "timely": timely,
        "bad": bad,
        "row": row,
    }


def new_metrics() -> dict:
    return {
        "total": 0,
        "valid": 0,
        "complete": 0,
        "consistent": 0,
        "timely": 0,
        "pairs": set(),
        "bad": {},
    }


def add_line(metrics: dict, line: str, rules: dict) -> dict:
    """Accumulate one line and return its classification."""
    info = classify(line, rules)
    metrics["total"] += 1
    metrics["valid"] += info["valid"]
    metrics["complete"] += info["complete"]
    metrics["consistent"] += info["consistent"]
    metrics["timely"] += info["timely"]
    if info["bad"]:
        metrics["bad"][info["bad"]] = metrics["bad"].get(info["bad"], 0) + 1
    if info["row"]:
        metrics["pairs"].add((info["row"]["user"], info["row"]["movie"]))
    return info


def finalize(metrics: dict) -> dict:
    result = {key: metrics[key] for key in ("total", "valid", "complete", "consistent", "timely")}
    result["unique"] = len(metrics["pairs"])
    result["bad"] = dict(metrics["bad"])
    return result


def counts_to_metrics(counts: dict) -> dict:
    """Build a metrics dict from counters produced by the Hadoop reducers."""
    return {
        "total": int(counts.get("total", 0)),
        "valid": int(counts.get("valid", 0)),
        "complete": int(counts.get("complete", 0)),
        "consistent": int(counts.get("consistent", 0)),
        "timely": int(counts.get("timely", 0)),
        "unique": int(counts.get("unique", 0)),
        "bad": dict(counts.get("bad", {})),
    }


def merge_counts(target: dict, part: dict) -> None:
    """Sum reducer output from several part files into ``target``."""
    for key in COUNTER_KEYS:
        target[key] = target.get(key, 0) + int(part.get(key, 0))
    bad = target.setdefault("bad", {})
    for reason, count in part.get("bad", {}).items():
        bad[reason] = bad.get(reason, 0) + int(count)


def _score(part: int, whole: int, scale: float) -> float:
    return round(scale * part / whole, 2) if whole else 0.0


def dimensions_from(before: dict, after: dict, rules: dict) -> list[dict]:
    scale = rules["scoring"]["dimension_scale"]
    specs = (
        ("accurate", "valid", "total", "按 ID、评分与时间戳校验通过的记录比例"),
        ("completeness", "complete", "total", "四个字段均非空的记录比例"),
        ("unique", "unique", "valid", "按用户与电影去重后的记录比例"),
        ("up_to_date", "timely", "consistent", "时间戳不早于阈值且可解析的记录比例"),
        ("consistent", "consistent", "total", "字段类型可解析为 MovieLens 格式的记录比例"),
    )
    return [
        {
            "dimension": name,
            "before": _score(before[part], before[whole], scale),
            "after": _score(after[part], after[whole], scale),
            "note": note,
        }
        for name, part, whole, note in specs
    ]


def dispositions_from(before: dict, after: dict) -> list[dict]:
    return [
        {
            "label": "拒绝无效记录",
            "count": before["total"] - before["valid"],
            "reason": "格式、类型或取值不符合默认规则",
        },
        {
            "label": "删除重复记录",
            "count": before["valid"] - before["unique"],
            "reason": "相同用户和电影仅保留最新时间戳",
        },
        {
            "label": "保留有效记录",
            "count": after["total"],
            "reason": "通过校验并完成去重",
        },
    ]


def explain(before: dict, after: dict) -> str:
    rejected = before["total"] - before["valid"]
    deduplicated = before["valid"] - before["unique"]
    return (
        f"输入 {before['total']} 条评分记录，其中 {rejected} 条未通过格式或取值校验被拒绝，"
        f"{deduplicated} 条为重复评分（同一用户与电影保留最新时间戳），"
        f"最终得到 {after['total']} 条可用记录。"
    )

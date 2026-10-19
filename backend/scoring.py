"""Shared cleaning and scoring logic for the local executor and Hadoop Streaming.

Design notes
------------
The five dimensions must measure *different* things, otherwise a single defect
moves several scores at once and the comparison stops being informative:

* Complete    – required fields exist and are not blank.
* Consistent  – values are written in the canonical MovieLens form.
* Accurate    – values are inside the business domain (positive ids, integer 1-5).
* Up-to-date  – timestamps fall inside the declared collection window.
* Unique      – one record per (user, movie) business key.

A record is *usable* after cleaning when it passes all of the above. Records
that fail are quarantined with a reason rather than silently deleted, and
format-only problems are repaired without changing the value.

This module must stay dependency-free and must not import other ``backend``
modules: it is shipped to cluster nodes with ``-file`` and imported there as a
top-level ``scoring`` module.
"""

from __future__ import annotations

import math
import re
from datetime import datetime

DIMENSIONS = ("accurate", "completeness", "unique", "up_to_date", "consistent")
COUNTER_KEYS = ("total", "complete", "consistent", "accurate", "timely", "unique", "repaired")
CANONICAL_INT = re.compile(r"^(0|[1-9][0-9]*)$")

METHOD = {
    "accurate": "UserID、MovieID 为正整数且评分为 1–5 的整数；这是取值范围约束，不能证明评分真实反映了用户偏好。",
    "completeness": "UserID、MovieID、Rating、Timestamp 四个必填字段均存在且非空白。",
    "unique": "可用记录中，不同 (UserID, MovieID) 业务键的数量占可用记录数的比例。",
    "up_to_date": "可用记录中时间戳落在数据集声明采集窗口内的比例；衡量时间戳是否可信，不代表数据相对今天的时效性。",
    "consistent": "四个字段以规范整数写法、使用 :: 分隔的记录比例；只做格式修复的问题计入清洗后改善。",
}

LIMITATIONS = [
    "MovieLens 1M 是 2000–2003 年的历史归档，Up-to-date 只检查时间戳是否落在声明采集期内，不能用来判断数据今天是否新鲜。",
    "无法验证评分是否真实反映用户偏好；Accurate 只能验证取值范围和业务键约束，格式正确不等于内容真实。",
    "本轮只评估 ratings.dat；users.dat 与 movies.dat 的字段完整性、类型拼写和跨表引用未纳入评分。",
    "同一用户对同一电影在同一时间出现不同评分时，按重复记录处理并保留最新或最后一条，未单独统计冲突。",
    "评分数量特别多或特别少的用户属于业务现象，不作为质量错误；是否剔除应由后续建模阶段决定。",
]

UNVERIFIED = [
    "用户性别、年龄、职业、邮编的真实性。",
    "电影标题、年份和类型标注的正确性。",
    "评分与用户真实观影偏好的一致程度。",
]

_WINDOW_CACHE: dict[tuple[str, str], tuple[int, int]] = {}


def _epoch(value: str) -> int:
    return int(datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp())


def _window(rules: dict) -> tuple[int, int]:
    dataset = rules["dataset"]
    key = (dataset["coverage_start"], dataset["coverage_end"])
    if key not in _WINDOW_CACHE:
        _WINDOW_CACHE[key] = (_epoch(key[0]), _epoch(key[1]))
    return _WINDOW_CACHE[key]


def classify(line: str, rules: dict) -> dict:
    """Classify one raw line and expose the canonical row when values are valid."""
    raw = line.rstrip("\r\n")
    fields = raw.split(rules["format"]["delimiter"])
    info = {
        "complete": False,
        "consistent": False,
        "accurate": False,
        "timely": False,
        "repaired": False,
        "repair_kinds": [],
        "bad": None,
        "row": None,
        "raw": raw,
    }

    if len(fields) != 4 or not all(field.strip() for field in fields):
        info["bad"] = "format"
        return info
    info["complete"] = True

    stripped = [field.strip() for field in fields]
    kinds = []
    if any(field != value for field, value in zip(fields, stripped)):
        kinds.append("whitespace")
    if not all(CANONICAL_INT.match(value) for value in stripped):
        kinds.append("number_format")
    info["repair_kinds"] = kinds
    info["consistent"] = not kinds

    try:
        user, movie = int(stripped[0]), int(stripped[1])
        rating = float(stripped[2])
        timestamp = int(stripped[3])
    except (ValueError, OverflowError):
        info["bad"] = "format"
        return info

    validation = rules["validation"]
    integral = math.isfinite(rating) and rating.is_integer()
    if validation["rating_integer"] and not integral:
        info["bad"] = "range"
        return info
    if validation["positive_ids"] and (user <= 0 or movie <= 0):
        info["bad"] = "range"
        return info
    if not validation["rating_min"] <= rating <= validation["rating_max"]:
        info["bad"] = "range"
        return info

    info["accurate"] = True
    info["row"] = {"user": user, "movie": movie, "rating": rating, "timestamp": timestamp}

    start, end = _window(rules)
    if timestamp < start or timestamp > end:
        info["bad"] = "timestamp"
        return info

    info["timely"] = True
    info["repaired"] = not info["consistent"]
    return info


def new_metrics() -> dict:
    metrics = {key: 0 for key in COUNTER_KEYS}
    metrics["pairs"] = set()
    metrics["bad"] = {}
    metrics["repair_kinds"] = {}
    return metrics


def add_line(metrics: dict, line: str, rules: dict) -> dict:
    info = classify(line, rules)
    metrics["total"] += 1
    metrics["complete"] += info["complete"]
    metrics["consistent"] += info["consistent"]
    metrics["accurate"] += info["accurate"]
    metrics["timely"] += info["timely"]
    if info["repaired"]:
        metrics["repaired"] += 1
        for kind in info["repair_kinds"]:
            metrics["repair_kinds"][kind] = metrics["repair_kinds"].get(kind, 0) + 1
    if info["bad"]:
        metrics["bad"][info["bad"]] = metrics["bad"].get(info["bad"], 0) + 1
    if info["timely"]:
        metrics["pairs"].add((info["row"]["user"], info["row"]["movie"]))
    return info


def finalize(metrics: dict) -> dict:
    result = {key: metrics[key] for key in COUNTER_KEYS}
    result["unique"] = len(metrics["pairs"])
    result["usable"] = metrics["timely"]
    result["bad"] = dict(metrics["bad"])
    result["repair_kinds"] = dict(metrics["repair_kinds"])
    return result


def counts_to_metrics(counts: dict) -> dict:
    """Build a metrics dict from the counters produced by the Hadoop reducers."""
    result = {key: int(counts.get(key, 0)) for key in COUNTER_KEYS}
    result["unique"] = int(counts.get("unique", 0))
    result["usable"] = result["timely"]
    result["bad"] = dict(counts.get("bad", {}))
    result["repair_kinds"] = dict(counts.get("repair_kinds", {}))
    return result


def merge_counts(target: dict, part: dict) -> None:
    for key in (*COUNTER_KEYS, "unique"):
        target[key] = target.get(key, 0) + int(part.get(key, 0))
    for field in ("bad", "repair_kinds"):
        bucket = target.setdefault(field, {})
        for name, count in part.get(field, {}).items():
            bucket[name] = bucket.get(name, 0) + int(count)


def _score(part: int, whole: int, scale: float) -> float:
    return round(scale * part / whole, 2) if whole else 0.0


def dimensions_from(before: dict, after: dict, rules: dict) -> list[dict]:
    scale = rules["scoring"]["dimension_scale"]
    specs = (
        ("accurate", "accurate", "total", METHOD["accurate"]),
        ("completeness", "complete", "total", METHOD["completeness"]),
        ("unique", "unique", "usable", METHOD["unique"]),
        ("up_to_date", "timely", "accurate", METHOD["up_to_date"]),
        ("consistent", "consistent", "total", METHOD["consistent"]),
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


def repair_summary(metrics: dict) -> dict:
    return {"records": metrics["repaired"], "by_kind": dict(metrics["repair_kinds"])}


def quarantine_summary(metrics: dict) -> dict:
    return {"records": metrics["total"] - metrics["usable"], "by_reason": dict(metrics["bad"])}


def dispositions_from(before: dict, after: dict) -> list[dict]:
    return [
        {
            "label": "修复后保留",
            "count": before["repaired"],
            "reason": "去除首尾空白或规范化数字写法，取值未改变",
        },
        {
            "label": "去重删除",
            "count": max(0, before["usable"] - before["unique"]),
            "reason": "同一用户与电影只保留时间戳最新的一条",
        },
        {
            "label": "隔离待处理",
            "count": before["total"] - before["usable"],
            "reason": "格式、取值或时间戳不符合规则，未进入结果集",
        },
        {
            "label": "保留有效记录",
            "count": after["total"],
            "reason": "通过校验、修复与去重",
        },
    ]


def explain(before: dict, after: dict) -> str:
    repaired = before["repaired"]
    duplicated = max(0, before["usable"] - before["unique"])
    quarantined = before["total"] - before["usable"]
    return (
        f"输入 {before['total']} 条评分记录：{repaired} 条只需格式修复，"
        f"{duplicated} 条重复记录被合并，{quarantined} 条因格式、取值或时间戳问题被隔离"
        f"（未删除，见隔离文件），最终保留 {after['total']} 条。"
    )

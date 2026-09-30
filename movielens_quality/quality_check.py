"""Deterministic MovieLens 1M parser and quality checker.

The checker deliberately does not mutate source files. It reads the three
official ``.dat`` files as ISO-8859-1 text and writes findings to a separate
output directory when requested.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

ENCODING = "iso-8859-1"
EXPECTED_FIELDS = {"ratings.dat": 4, "users.dat": 5, "movies.dat": 3}
VALID_GENDERS = {"M", "F"}
VALID_AGES = {1, 18, 25, 35, 45, 50, 56}
VALID_OCCUPATIONS = set(range(21))


@dataclass(frozen=True)
class Anomaly:
    file: str
    line_number: int
    reason: str
    raw_record: str
    fields: tuple[str, ...]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def _read_records(path: Path) -> Iterable[tuple[int, str]]:
    with path.open("r", encoding=ENCODING, newline="") as handle:
        for line_number, raw in enumerate(handle, start=1):
            yield line_number, raw.rstrip("\r\n")


def _add_anomaly(
    anomalies: list[Anomaly],
    file_name: str,
    line_number: int,
    reason: str,
    raw: str,
    fields: list[str],
) -> None:
    anomalies.append(Anomaly(file_name, line_number, reason, raw, tuple(fields)))


def _parse_int(value: str) -> int | None:
    try:
        return int(value)
    except ValueError:
        return None


def _new_file_stats() -> dict[str, Any]:
    return {
        "total_records": 0,
        "valid_records": 0,
        "complete_records": 0,
        "type_range_valid_records": 0,
        "duplicate_records": 0,
        "anomaly_records": 0,
        "anomaly_counts": {},
    }


def inspect_dataset(
    input_dir: str | Path,
    output_dir: str | Path | None = None,
) -> dict[str, Any]:
    """Parse and check a MovieLens directory.

    ``input_dir`` must contain ``ratings.dat``, ``users.dat`` and
    ``movies.dat``. The returned report contains only JSON-compatible values.
    If ``output_dir`` is provided, ``anomalies.jsonl`` and ``stats.json`` are
    written there; no file under ``input_dir`` is written.
    """

    source = Path(input_dir)
    missing = [name for name in EXPECTED_FIELDS if not (source / name).is_file()]
    if missing:
        raise FileNotFoundError(f"Missing MovieLens files: {', '.join(missing)}")

    anomalies: list[Anomaly] = []
    stats = {name: _new_file_stats() for name in EXPECTED_FIELDS}
    users: dict[int, tuple[Any, ...]] = {}
    movies: dict[int, tuple[Any, ...]] = {}
    ratings: list[tuple[int, int, int, int, int, str]] = []
    rating_references: list[tuple[int, int, str, int]] = []

    for file_name, expected_fields in EXPECTED_FIELDS.items():
        file_stats = stats[file_name]
        for line_number, raw in _read_records(source / file_name):
            file_stats["total_records"] += 1
            fields = raw.split("::")
            reasons: list[str] = []

            if len(fields) != expected_fields:
                reasons.append("field_count")
            has_empty_field = any(not field.strip() for field in fields)
            if file_name == "movies.dat" and len(fields) == expected_fields:
                has_empty_field = has_empty_field or any(
                    not genre.strip() for genre in fields[2].split("|")
                )
            if has_empty_field:
                reasons.append("empty_field")
            if len(fields) == expected_fields and not has_empty_field:
                file_stats["complete_records"] += 1

            parsed: tuple[Any, ...] | None = None
            if file_name == "ratings.dat" and len(fields) == 4:
                user_id = _parse_int(fields[0].strip())
                movie_id = _parse_int(fields[1].strip())
                rating = _parse_int(fields[2].strip())
                timestamp = _parse_int(fields[3].strip())
                if user_id is not None and movie_id is not None:
                    rating_references.append((user_id, movie_id, raw, line_number))
                if None in (user_id, movie_id, rating, timestamp):
                    reasons.append("type")
                elif not (user_id > 0 and movie_id > 0 and 1 <= rating <= 5 and timestamp >= 0):
                    reasons.append("range")
                else:
                    parsed = (user_id, movie_id, rating, timestamp)
            elif file_name == "users.dat" and len(fields) == 5:
                user_id = _parse_int(fields[0].strip())
                age = _parse_int(fields[2].strip())
                occupation = _parse_int(fields[3].strip())
                if user_id is None or age is None or occupation is None:
                    reasons.append("type")
                elif (
                    user_id <= 0
                    or fields[1].strip() not in VALID_GENDERS
                    or age not in VALID_AGES
                    or occupation not in VALID_OCCUPATIONS
                ):
                    reasons.append("range_or_category")
                else:
                    parsed = (
                        user_id,
                        fields[1].strip(),
                        age,
                        occupation,
                        fields[4].strip(),
                    )
            elif file_name == "movies.dat" and len(fields) == 3:
                movie_id = _parse_int(fields[0].strip())
                if movie_id is None:
                    reasons.append("type")
                elif movie_id <= 0:
                    reasons.append("range")
                else:
                    parsed = (movie_id, fields[1].strip(), fields[2].strip())

            if reasons:
                for reason in sorted(set(reasons)):
                    _add_anomaly(anomalies, file_name, line_number, reason, raw, fields)
                file_stats["anomaly_records"] += 1
                continue

            file_stats["type_range_valid_records"] += 1
            if file_name == "users.dat":
                assert parsed is not None
                user_id = parsed[0]
                if user_id in users:
                    file_stats["duplicate_records"] += 1
                    reason = "duplicate_key" if users[user_id] == parsed else "conflicting_key"
                    _add_anomaly(anomalies, file_name, line_number, reason, raw, fields)
                else:
                    users[user_id] = parsed
            elif file_name == "movies.dat":
                assert parsed is not None
                movie_id = parsed[0]
                if movie_id in movies:
                    file_stats["duplicate_records"] += 1
                    reason = "duplicate_key" if movies[movie_id] == parsed else "conflicting_key"
                    _add_anomaly(anomalies, file_name, line_number, reason, raw, fields)
                else:
                    movies[movie_id] = parsed
            else:
                assert parsed is not None
                ratings.append((line_number, *parsed, raw))

    rating_keys: Counter[tuple[int, int, int, int]] = Counter()
    for line_number, user_id, movie_id, rating, timestamp, raw in ratings:
        key = (user_id, movie_id, rating, timestamp)
        rating_keys[key] += 1
        if rating_keys[key] > 1:
            stats["ratings.dat"]["duplicate_records"] += 1
            _add_anomaly(
                anomalies,
                "ratings.dat",
                line_number,
                "duplicate_key",
                raw,
                raw.split("::"),
            )
    for user_id, movie_id, raw, line_number in rating_references:
        if user_id not in users:
            _add_anomaly(
                anomalies, "ratings.dat", line_number, "missing_user_reference", raw, raw.split("::")
            )
        if movie_id not in movies:
            _add_anomaly(
                anomalies, "ratings.dat", line_number, "missing_movie_reference", raw, raw.split("::")
            )

    anomaly_counts: dict[str, Counter[str]] = defaultdict(Counter)
    for anomaly in anomalies:
        anomaly_counts[anomaly.file][anomaly.reason] += 1
    for file_name, file_stats in stats.items():
        file_stats["anomaly_counts"] = dict(sorted(anomaly_counts[file_name].items()))
        file_stats["valid_records"] = (
            file_stats["type_range_valid_records"] - file_stats["duplicate_records"]
        )

    report: dict[str, Any] = {
        "encoding": ENCODING,
        "delimiter": "::",
        "header_skipped": False,
        "input_directory": str(source.resolve()),
        "files": stats,
        "cross_table": {
            "unique_user_ids": len(users),
            "unique_movie_ids": len(movies),
            "rating_records_checked": len(ratings),
            "missing_user_references": anomaly_counts["ratings.dat"]["missing_user_reference"],
            "missing_movie_references": anomaly_counts["ratings.dat"]["missing_movie_reference"],
        },
        "anomaly_count": len(anomalies),
        "anomalies": [item.as_dict() for item in anomalies],
    }

    if output_dir is not None:
        destination = Path(output_dir)
        destination.mkdir(parents=True, exist_ok=True)
        with (destination / "anomalies.jsonl").open("w", encoding="utf-8", newline="\n") as handle:
            for anomaly in anomalies:
                handle.write(json.dumps(anomaly.as_dict(), ensure_ascii=False) + "\n")
        (destination / "stats.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Check MovieLens 1M .dat files")
    parser.add_argument("--input-dir", required=True, type=Path)
    parser.add_argument("--output-dir", required=True, type=Path)
    args = parser.parse_args()
    report = inspect_dataset(args.input_dir, args.output_dir)
    print(json.dumps(report["files"], ensure_ascii=False, indent=2))
    print(f"anomaly_count={report['anomaly_count']}")


if __name__ == "__main__":
    main()

"""Deterministic MovieLens 1M parser and quality checker.

The checker deliberately does not mutate source files. It reads the three
official ``.dat`` files as ISO-8859-1 text and writes findings to a separate
output directory when requested.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Iterable

ENCODING = "iso-8859-1"
EXPECTED_FIELDS = {"ratings.dat": 4, "users.dat": 5, "movies.dat": 3}
VALID_GENDERS = {"M", "F"}
VALID_AGES = {1, 18, 25, 35, 45, 50, 56}
VALID_OCCUPATIONS = set(range(21))
VALID_GENRES = {
    "Action",
    "Adventure",
    "Animation",
    "Children's",
    "Comedy",
    "Crime",
    "Documentary",
    "Drama",
    "Fantasy",
    "Film-Noir",
    "Horror",
    "Musical",
    "Mystery",
    "Romance",
    "Sci-Fi",
    "Thriller",
    "War",
    "Western",
}
MIN_TIMESTAMP = 946684800
MAX_TIMESTAMP = 1046476799
UP_TO_DATE_WINDOW_DAYS = 90
UP_TO_DATE_WINDOW_START = MAX_TIMESTAMP - UP_TO_DATE_WINDOW_DAYS * 86400


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


def _new_file_stats(file_name: str) -> dict[str, Any]:
    statistics = {
        "total_records": 0,
        "accurate_fields_passed": 0,
        "accurate_fields_checked": 0,
        "eligible_records": 0,
        "exact_duplicate_records": 0,
        "conflict_records": 0,
        "conflict_groups": 0,
        "valid_records": 0,
        "complete_records": 0,
        "consistent_records": 0,
        "type_range_valid_records": 0,
        "duplicate_records": 0,
        "anomaly_records": 0,
        "anomaly_counts": {},
    }
    if file_name == "ratings.dat":
        statistics["up_to_date_records"] = 0
    return statistics


def _accurate_field_results(file_name: str, fields: list[str]) -> list[bool]:
    results: list[bool] = []

    def add_integer(index: int, valid: Any) -> None:
        if index < len(fields) and fields[index].strip():
            value = _parse_int(fields[index].strip())
            results.append(value is not None and valid(value))

    if file_name == "ratings.dat":
        add_integer(0, lambda value: value > 0)
        add_integer(1, lambda value: value > 0)
        add_integer(2, lambda value: 1 <= value <= 5)
        add_integer(3, lambda value: MIN_TIMESTAMP <= value <= MAX_TIMESTAMP)
    elif file_name == "users.dat":
        add_integer(0, lambda value: value > 0)
        if len(fields) > 1 and fields[1].strip():
            results.append(fields[1].strip() in VALID_GENDERS)
        add_integer(2, lambda value: value in VALID_AGES)
        add_integer(3, lambda value: value in VALID_OCCUPATIONS)
    elif file_name == "movies.dat":
        add_integer(0, lambda value: value > 0)
        if len(fields) > 1 and fields[1].strip():
            year = re.search(r"\(([^()]*)\)$", fields[1].strip())
            if year:
                candidate = year.group(1)
                results.append(
                    len(candidate) == 4
                    and candidate.isdigit()
                    and 1888 <= int(candidate) <= 2003
                )
        if len(fields) > 2 and fields[2].strip():
            genres = [genre for genre in fields[2].split("|") if genre]
            results.append(bool(genres) and all(genre in VALID_GENRES for genre in genres))
    return results


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
    stats = {name: _new_file_stats(name) for name in EXPECTED_FIELDS}
    users: dict[int, tuple[Any, ...]] = {}
    movies: dict[int, tuple[Any, ...]] = {}
    ratings: list[tuple[int, int, int, int, int, str]] = []
    rating_references: list[tuple[int, int, str, int]] = []
    identity_groups: dict[str, dict[Any, list[tuple[int, tuple[Any, ...], str, list[str]]]]] = {
        name: defaultdict(list) for name in EXPECTED_FIELDS
    }
    inconsistent_lines: dict[str, set[int]] = {name: set() for name in EXPECTED_FIELDS}

    for file_name, expected_fields in EXPECTED_FIELDS.items():
        file_stats = stats[file_name]
        for line_number, raw in _read_records(source / file_name):
            file_stats["total_records"] += 1
            fields = raw.split("::")
            if file_name == "ratings.dat" and len(fields) == expected_fields:
                timestamp = _parse_int(fields[3].strip())
                if timestamp is not None and UP_TO_DATE_WINDOW_START <= timestamp <= MAX_TIMESTAMP:
                    file_stats["up_to_date_records"] += 1
            accurate_results = _accurate_field_results(file_name, fields)
            file_stats["accurate_fields_checked"] += len(accurate_results)
            file_stats["accurate_fields_passed"] += sum(accurate_results)
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
                if any(reason in {"field_count", "empty_field", "type"} for reason in reasons):
                    inconsistent_lines[file_name].add(line_number)
                for reason in sorted(set(reasons)):
                    _add_anomaly(anomalies, file_name, line_number, reason, raw, fields)
                file_stats["anomaly_records"] += 1
                continue

            file_stats["type_range_valid_records"] += 1
            file_stats["eligible_records"] += 1
            assert parsed is not None
            if file_name == "users.dat":
                user_id = parsed[0]
                identity_groups[file_name][user_id].append((line_number, parsed, raw, fields))
                users.setdefault(user_id, parsed)
            elif file_name == "movies.dat":
                movie_id = parsed[0]
                identity_groups[file_name][movie_id].append((line_number, parsed, raw, fields))
                genres = parsed[2].split("|")
                if (
                    any(not genre or genre not in VALID_GENRES for genre in genres)
                    or len(genres) != len(set(genres))
                ):
                    inconsistent_lines[file_name].add(line_number)
                    _add_anomaly(anomalies, file_name, line_number, "genre_format", raw, fields)
                movies.setdefault(movie_id, parsed)
            else:
                if not MIN_TIMESTAMP <= parsed[3] <= MAX_TIMESTAMP:
                    inconsistent_lines[file_name].add(line_number)
                ratings.append((line_number, *parsed, raw))
                identity_groups[file_name][(parsed[0], parsed[1], parsed[3])].append(
                    (line_number, parsed, raw, fields)
                )

    for file_name, groups in identity_groups.items():
        file_stats = stats[file_name]
        for rows in groups.values():
            if len({parsed for _, parsed, _, _ in rows}) == 1:
                duplicates = rows[1:]
                file_stats["exact_duplicate_records"] += len(duplicates)
                for line_number, _, raw, fields in duplicates:
                    _add_anomaly(anomalies, file_name, line_number, "exact_duplicate", raw, fields)
            else:
                file_stats["conflict_groups"] += 1
                file_stats["conflict_records"] += len(rows)
                for line_number, _, raw, fields in rows:
                    inconsistent_lines[file_name].add(line_number)
                    _add_anomaly(anomalies, file_name, line_number, "conflict", raw, fields)
        file_stats["duplicate_records"] = (
            file_stats["exact_duplicate_records"] + file_stats["conflict_records"]
        )

    for user_id, movie_id, raw, line_number in rating_references:
        if user_id not in users:
            inconsistent_lines["ratings.dat"].add(line_number)
            _add_anomaly(
                anomalies, "ratings.dat", line_number, "missing_user_reference", raw, raw.split("::")
            )
        if movie_id not in movies:
            inconsistent_lines["ratings.dat"].add(line_number)
            _add_anomaly(
                anomalies, "ratings.dat", line_number, "missing_movie_reference", raw, raw.split("::")
            )

    anomaly_counts: dict[str, Counter[str]] = defaultdict(Counter)
    for anomaly in anomalies:
        anomaly_counts[anomaly.file][anomaly.reason] += 1
    for file_name, file_stats in stats.items():
        file_stats["anomaly_counts"] = dict(sorted(anomaly_counts[file_name].items()))
        file_stats["valid_records"] = (
            file_stats["eligible_records"]
            - file_stats["exact_duplicate_records"]
            - file_stats["conflict_records"]
        )
        file_stats["consistent_records"] = (
            file_stats["total_records"] - len(inconsistent_lines[file_name])
        )

    report: dict[str, Any] = {
        "encoding": ENCODING,
        "delimiter": "::",
        "header_skipped": False,
        "input_directory": str(source.resolve()),
        "up_to_date_rule": {
            "applicable_tables": ["ratings.dat"],
            "reference_timestamp": MAX_TIMESTAMP,
            "window_days": UP_TO_DATE_WINDOW_DAYS,
            "window_start_timestamp": UP_TO_DATE_WINDOW_START,
        },
        "consistent_rule": {
            "users.dat": ["uniform_structure_and_types", "no_conflicting_user_id"],
            "movies.dat": [
                "uniform_structure_and_types",
                "no_conflicting_movie_id",
                "canonical_unique_genres",
            ],
            "ratings.dat": [
                "uniform_structure_and_types",
                "timestamp_in_seconds",
                "existing_user_and_movie_references",
                "no_conflicting_user_movie_timestamp",
            ],
        },
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

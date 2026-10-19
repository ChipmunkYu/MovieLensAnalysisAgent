from __future__ import annotations

import json
import shutil
import subprocess
import sys
from pathlib import Path

from backend import hadoop_runner, scoring
from backend.executors import run_local

ROOT = Path(__file__).resolve().parents[2]
STREAMING = ROOT / "hadoop" / "streaming"
SCRIPT_NAMES = (
    "score_mapper.py",
    "score_reducer.py",
    "clean_mapper.py",
    "clean_reducer.py",
    "quarantine_mapper.py",
    "quarantine_reducer.py",
)
RULES = json.loads((ROOT / "rules" / "default.json").read_text(encoding="utf-8"))

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


def stage_scripts(directory: Path) -> Path:
    for name in SCRIPT_NAMES:
        shutil.copy2(STREAMING / name, directory / name)
    shutil.copy2(Path(scoring.__file__), directory / "scoring.py")
    (directory / "rules.json").write_text(json.dumps(RULES), encoding="utf-8")
    return directory


def run_script(directory: Path, name: str, payload: bytes) -> bytes:
    completed = subprocess.run(
        [sys.executable, name],
        input=payload,
        capture_output=True,
        cwd=str(directory),
    )
    assert completed.returncode == 0, completed.stderr.decode("utf-8", "replace")
    return completed.stdout


def shuffle(payload: bytes) -> bytes:
    """Emulate the Hadoop shuffle: group and sort by key (the text before the tab)."""
    lines = payload.decode("latin-1").splitlines()
    return ("\n".join(sorted(lines)) + "\n").encode("latin-1")


def in_process_metrics() -> dict:
    metrics = scoring.new_metrics()
    for line in SAMPLE.splitlines():
        scoring.add_line(metrics, line, RULES)
    return scoring.finalize(metrics)


def test_score_pipe_matches_in_process_scoring(tmp_path: Path):
    staging = stage_scripts(tmp_path)
    reduced = run_script(staging, "score_reducer.py", shuffle(run_script(staging, "score_mapper.py", SAMPLE.encode("latin-1"))))
    output = tmp_path / "out"
    output.mkdir()
    (output / "part-00000").write_bytes(reduced)

    counts = hadoop_runner._read_counts(output)
    assert scoring.counts_to_metrics(counts) == in_process_metrics()
    assert counts == {
        "total": 8,
        "complete": 7,
        "consistent": 6,
        "accurate": 5,
        "timely": 4,
        "unique": 3,
        "repaired": 1,
        "bad": {"format": 1, "range": 2, "timestamp": 1},
        "repair_kinds": {"number_format": 1},
    }


def test_clean_pipe_matches_local_executor(tmp_path: Path, monkeypatch):
    staging = stage_scripts(tmp_path)
    reduced = run_script(staging, "clean_reducer.py", shuffle(run_script(staging, "clean_mapper.py", SAMPLE.encode("latin-1"))))

    data = tmp_path / "data"
    data.mkdir()
    (data / "ratings.dat").write_bytes(SAMPLE.encode("latin-1"))
    monkeypatch.setenv("DATA_DIR", str(data))
    monkeypatch.setenv("ARTIFACT_DIR", str(tmp_path / "artifacts"))
    run_local("stream-test", "ratings.dat", RULES)

    cleaned = (tmp_path / "artifacts" / "stream-test" / "ratings-cleaned.dat").read_text(encoding="iso-8859-1")
    assert sorted(reduced.decode("latin-1").splitlines()) == sorted(cleaned.splitlines())
    assert cleaned.splitlines() == [
        "1::10::5::946771200",
        "3::30::3::978307200",
        "4::40::4::1009843200",
    ]


def test_quarantine_pipe_keeps_rejected_rows_with_reasons(tmp_path: Path):
    staging = stage_scripts(tmp_path)
    reduced = run_script(
        staging,
        "quarantine_reducer.py",
        shuffle(run_script(staging, "quarantine_mapper.py", SAMPLE.encode("latin-1"))),
    )
    lines = reduced.decode("latin-1").splitlines()
    assert len(lines) == 4
    reasons = sorted(line.split("\t", 1)[0] for line in lines)
    assert reasons == ["format", "range", "range", "timestamp"]


def test_dimensions_measure_distinct_properties():
    before = in_process_metrics()
    after = scoring.counts_to_metrics(
        {"total": before["unique"], "complete": before["unique"], "consistent": before["unique"],
         "accurate": before["unique"], "timely": before["unique"], "unique": before["unique"],
         "repaired": 0, "bad": {}, "repair_kinds": {}}
    )
    dimensions = {item["dimension"]: item for item in scoring.dimensions_from(before, after, RULES)}
    assert list(dimensions) == list(scoring.DIMENSIONS)
    assert dimensions["accurate"]["before"] == 62.5
    assert dimensions["completeness"]["before"] == 87.5
    assert dimensions["unique"]["before"] == 75.0
    assert dimensions["up_to_date"]["before"] == 80.0
    assert dimensions["consistent"]["before"] == 75.0
    assert all(item["after"] == 100.0 for item in dimensions.values())
    assert scoring.quarantine_summary(before) == {
        "records": 4,
        "by_reason": {"format": 1, "range": 2, "timestamp": 1},
    }
    assert scoring.repair_summary(before) == {"records": 1, "by_kind": {"number_format": 1}}

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
SCRIPT_NAMES = ("score_mapper.py", "score_reducer.py", "clean_mapper.py", "clean_reducer.py")
RULES = json.loads((ROOT / "rules" / "default.json").read_text(encoding="utf-8"))

SAMPLE = (
    "1::10::4::100\n"
    "1::10::5::200\n"
    "2::20::9::300\n"
    "bad row\n"
    "3::30::3::400\n"
    "5::50::0::600\n"
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
    lines = payload.decode("ascii").splitlines()
    return ("\n".join(sorted(lines)) + "\n").encode("ascii")


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
    assert counts == in_process_metrics()
    assert counts == {
        "total": 6,
        "valid": 3,
        "complete": 5,
        "consistent": 5,
        "timely": 5,
        "unique": 2,
        "bad": {"format": 1, "range": 2},
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
    assert cleaned.splitlines() == ["1::10::5::200", "3::30::3::400"]


def test_dimensions_cover_all_five_quality_dimensions():
    before = in_process_metrics()
    after = scoring.counts_to_metrics(
        {"total": before["unique"], "valid": before["unique"], "complete": before["unique"],
         "consistent": before["unique"], "timely": before["unique"], "unique": before["unique"], "bad": {}}
    )
    dimensions = scoring.dimensions_from(before, after, RULES)
    assert [item["dimension"] for item in dimensions] == list(scoring.DIMENSIONS)
    assert all(item["after"] == 100.0 for item in dimensions)

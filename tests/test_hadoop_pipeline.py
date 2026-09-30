import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from movielens_quality.hadoop_pipeline import PipelineConfig, run_pipeline, score_quality


class HadoopPipelineTests(unittest.TestCase):
    def test_scoring_formula_is_explicit_and_reusable(self):
        stats = {
            "total_records": 10,
            "accurate_records": 9,
            "complete_records": 8,
            "duplicate_records": 1,
            "up_to_date_records": 7,
            "consistent_records": 6,
        }
        self.assertEqual(
            score_quality(stats),
            {
                "Accurate": 90.0,
                "Complete": 80.0,
                "Unique": 90.0,
                "Up-to-date": 70.0,
                "Consistent": 60.0,
            },
        )

    def test_missing_hadoop_returns_failure_report_without_scores(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            for name in ("ratings.dat", "users.dat", "movies.dat"):
                (source / name).write_bytes(b"source")

            with patch("movielens_quality.hadoop_pipeline.shutil.which", return_value=None):
                report = run_pipeline(PipelineConfig(source, output))

            self.assertEqual(report["status"], "failed")
            self.assertEqual(report["failed_stage"], "hadoop_preflight")
            self.assertIsNone(report["quality"])
            saved = json.loads((output / "quality_report.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["error"], report["error"])


if __name__ == "__main__":
    unittest.main()

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from movielens_quality.hadoop_pipeline import (
    PipelineConfig,
    _score_delta,
    run_pipeline,
    score_quality,
    score_quality_by_table,
)


class HadoopPipelineTests(unittest.TestCase):
    def test_table_scores_macro_average_up_to_date_for_ratings_only(self):
        scores = score_quality_by_table(
            {
                "ratings": {
                    "total_records": 1_000_000,
                    "accurate_fields_passed": 4_000_000,
                    "accurate_fields_checked": 4_000_000,
                    "complete_records": 1_000_000,
                    "eligible_records": 1_000_000,
                    "exact_duplicate_records": 0,
                    "conflict_records": 0,
                    "up_to_date_records": 1_000_000,
                    "consistent_records": 1_000_000,
                },
                "users": {
                    "total_records": 1,
                    "accurate_fields_passed": 0,
                    "accurate_fields_checked": 4,
                    "complete_records": 0,
                    "eligible_records": 1,
                    "exact_duplicate_records": 1,
                    "conflict_records": 0,
                    "up_to_date_records": 0,
                    "consistent_records": 0,
                },
                "movies": {
                    "total_records": 0,
                    "accurate_fields_passed": 0,
                    "accurate_fields_checked": 0,
                    "complete_records": 0,
                    "eligible_records": 0,
                    "exact_duplicate_records": 0,
                    "conflict_records": 0,
                    "up_to_date_records": 0,
                    "consistent_records": 0,
                },
            }
        )

        self.assertEqual(scores["per_table"]["ratings"]["Accurate"], 100.0)
        self.assertEqual(scores["per_table"]["users"]["Accurate"], 0.0)
        self.assertEqual(scores["per_table"]["movies"]["Accurate"], 0.0)
        self.assertIsNone(scores["per_table"]["users"]["Up-to-date"])
        self.assertIsNone(scores["per_table"]["movies"]["Up-to-date"])
        self.assertEqual(scores["dataset"]["Up-to-date"], 100.0)
        self.assertEqual(
            {key: value for key, value in scores["dataset"].items() if key != "Up-to-date"},
            dict.fromkeys(("Accurate", "Complete", "Unique", "Consistent"), 50.0),
        )

    def test_missing_up_to_date_count_is_na_and_propagates_to_dataset(self):
        stats = {
            "total_records": 1,
            "accurate_fields_passed": 1,
            "accurate_fields_checked": 1,
            "complete_records": 1,
            "eligible_records": 1,
            "exact_duplicate_records": 0,
            "conflict_records": 0,
            "consistent_records": 1,
        }

        self.assertIsNone(score_quality(stats)["Up-to-date"])
        self.assertIsNone(score_quality_by_table({"users": stats})["dataset"]["Up-to-date"])
        self.assertIsNone(_score_delta(None, 50.0))
        self.assertIsNone(_score_delta(50.0, None))

    def test_accurate_uses_field_count_denominator(self):
        stats = {
            "total_records": 10,
            "accurate_fields_passed": 3,
            "accurate_fields_checked": 4,
            "complete_records": 8,
            "eligible_records": 10,
            "exact_duplicate_records": 1,
            "conflict_records": 0,
            "up_to_date_records": 7,
            "consistent_records": 6,
        }

        self.assertEqual(score_quality(stats)["Accurate"], 75.0)

    def test_unique_uses_eligible_exact_and_conflict_counts(self):
        stats = {
            "total_records": 10,
            "accurate_fields_passed": 10,
            "accurate_fields_checked": 10,
            "complete_records": 10,
            "eligible_records": 10,
            "exact_duplicate_records": 1,
            "conflict_records": 2,
            "up_to_date_records": 10,
            "consistent_records": 8,
        }

        self.assertEqual(score_quality(stats)["Unique"], 70.0)
        stats.update(eligible_records=0, exact_duplicate_records=0, conflict_records=0)
        self.assertIsNone(score_quality(stats)["Unique"])

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

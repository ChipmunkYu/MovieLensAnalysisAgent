import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from movielens_quality.agent_tools import (
    RULE_VERSION,
    SCORING_VERSION,
    MovieLensGovernanceAgent,
    parse_natural_language_request,
)


class AgentToolTests(unittest.TestCase):
    def test_parses_registered_versions_from_natural_language(self):
        request = parse_natural_language_request(
            "清洗 MovieLens 1M，dataset_version: data-1 "
            "rule_version: rule-1 scoring_version: score-1",
            "source",
            "output",
        )
        self.assertEqual(request.dataset_version, "data-1")
        self.assertEqual(request.rule_version, "rule-1")
        self.assertEqual(request.scoring_version, "score-1")

    def test_version_mismatch_is_rejected_before_hadoop(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            for name in ("ratings.dat", "users.dat", "movies.dat"):
                (source / name).write_bytes(b"source")
            agent = MovieLensGovernanceAgent(root / "registry.json")

            with patch("movielens_quality.agent_tools.run_pipeline") as pipeline:
                result = agent.start_task(
                    parse_natural_language_request(
                        f"MovieLens 1M rule_version: wrong",
                        source,
                        root / "output",
                    )
                )

            self.assertEqual(result["status"], "FAILED")
            self.assertEqual(result["failed_stage"], "agent_validation")
            pipeline.assert_not_called()
            self.assertIn("mismatch", result["error"])

    def test_failed_task_status_and_result_include_log(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            for name in ("ratings.dat", "users.dat", "movies.dat"):
                (source / name).write_bytes(b"source")
            agent = MovieLensGovernanceAgent(root / "registry.json")
            request = parse_natural_language_request("MovieLens 1M", source, root / "output")

            with patch("movielens_quality.hadoop_pipeline.shutil.which", return_value=None):
                result = agent.start_task(request)

            self.assertEqual(result["status"], "FAILED")
            status = agent.get_task_status(result["task_id"])
            self.assertEqual(status["status"], "FAILED")
            details = agent.get_task_result(result["task_id"])
            self.assertEqual(details["status"], "FAILED")
            self.assertTrue(Path(details["log_path"]).is_file())

    def test_success_result_exposes_quality_anomalies_and_report(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            output = root / "output"
            source.mkdir()
            for name in ("ratings.dat", "users.dat", "movies.dat"):
                (source / name).write_bytes(b"source")
            output.mkdir()
            (output / "quality_report.json").write_text(
                '{"quality":{"pre":{"Accurate":90},"post":{"Accurate":95}},'
                '"status":"succeeded"}',
                encoding="utf-8",
            )
            (output / "anomalies.jsonl").write_text("", encoding="utf-8")
            agent = MovieLensGovernanceAgent(root / "registry.json")
            request = parse_natural_language_request("MovieLens 1M", source, output)
            backend_report = {
                "task_id": "backend-task",
                "status": "succeeded",
                "dataset_version": "actual",
                "quality": {"pre": {}, "post": {}, "delta": {}},
            }
            with patch("movielens_quality.agent_tools.run_pipeline", return_value=backend_report):
                result = agent.start_task(request)

            self.assertEqual(result["status"], "SUCCESS")
            details = agent.get_task_result(result["task_id"])
            self.assertEqual(details["status"], "SUCCESS")
            self.assertTrue(details["report"].endswith("quality_report.json"))
            self.assertTrue(details["anomalies"].endswith("anomalies.jsonl"))


if __name__ == "__main__":
    unittest.main()

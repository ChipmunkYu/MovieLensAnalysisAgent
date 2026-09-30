import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from movielens_quality.agent_tools import parse_natural_language_request
from movielens_quality.web_app import TaskStore


class WebAppTests(unittest.TestCase):
    def test_submit_and_poll_exposes_real_failure(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "source"
            source.mkdir()
            for name in ("ratings.dat", "users.dat", "movies.dat"):
                (source / name).write_bytes(b"source")
            store = TaskStore(
                __import__("movielens_quality.agent_tools", fromlist=["MovieLensGovernanceAgent"])
                .MovieLensGovernanceAgent(root / "registry.json"),
                source,
                root / "outputs",
            )
            with patch("movielens_quality.hadoop_pipeline.shutil.which", return_value=None):
                task_id = store.submit("请清洗 MovieLens 1M")
                for _ in range(20):
                    result = store.result(task_id)
                    if result["status"] != "RUNNING":
                        break
                    time.sleep(0.01)
            self.assertEqual(result["status"], "FAILED")
            self.assertEqual(result["failed_stage"], "hadoop_preflight")
            self.assertIn("Hadoop command not found", result["error"])
            store.executor.shutdown(wait=True)

    def test_parse_request_remains_structured(self):
        request = parse_natural_language_request(
            "MovieLens 1M rule_version: ml1m-cleaning-v1.0",
            "source",
            "output",
        )
        self.assertEqual(request.input_dir, Path("source"))
        self.assertEqual(request.rule_version, "ml1m-cleaning-v1.0")


if __name__ == "__main__":
    unittest.main()

import json
import tempfile
import unittest
from pathlib import Path

from movielens_quality.quality_check import inspect_dataset


class QualityCheckTests(unittest.TestCase):
    def write_fixture(self, directory: Path) -> None:
        (directory / "users.dat").write_text(
            "1::F::25::1::00123\n2::M::35::20::90210\n", encoding="iso-8859-1"
        )
        (directory / "movies.dat").write_text(
            "10::Caf\xe9 (2000)::Drama\n20::Other (2001)::Comedy\n",
            encoding="iso-8859-1",
        )
        (directory / "ratings.dat").write_text(
            "1::10::5::100\n1::10::5::100\n2::99::0::100\n2::99::0::bad\n",
            encoding="iso-8859-1",
        )

    def test_parses_iso88591_without_header_and_detects_quality_issues(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary) / "source"
            output = Path(temporary) / "report"
            source.mkdir()
            self.write_fixture(source)

            report = inspect_dataset(source, output)

            self.assertEqual(report["encoding"], "iso-8859-1")
            self.assertEqual(report["delimiter"], "::")
            self.assertFalse(report["header_skipped"])
            self.assertEqual(report["files"]["users.dat"]["total_records"], 2)
            self.assertEqual(report["cross_table"]["missing_movie_references"], 2)
            reasons = {item["reason"] for item in report["anomalies"]}
            self.assertIn("duplicate_key", reasons)
            self.assertIn("range", reasons)
            self.assertIn("type", reasons)
            self.assertTrue((output / "anomalies.jsonl").is_file())
            saved = json.loads((output / "stats.json").read_text(encoding="utf-8"))
            self.assertEqual(saved["delimiter"], "::")
            self.assertEqual(
                (source / "movies.dat").read_text(encoding="iso-8859-1").splitlines()[0],
                "10::Café (2000)::Drama",
            )

    def test_missing_source_file_is_reported(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            (source / "users.dat").write_text("", encoding="iso-8859-1")
            with self.assertRaises(FileNotFoundError):
                inspect_dataset(source)


if __name__ == "__main__":
    unittest.main()

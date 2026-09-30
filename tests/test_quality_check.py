import json
import tempfile
import unittest
from pathlib import Path

from movielens_quality.quality_check import (
    MAX_TIMESTAMP,
    UP_TO_DATE_WINDOW_DAYS,
    UP_TO_DATE_WINDOW_START,
    inspect_dataset,
)


class QualityCheckTests(unittest.TestCase):
    def write_fixture(self, directory: Path) -> None:
        (directory / "users.dat").write_text(
            "1::F::25::1::00123\n2::M::35::20::90210\n", encoding="iso-8859-1"
        )
        (directory / "movies.dat").write_text(
            "10::Caf\xe9 (2000)::Drama\n20::Other (2001)::Comedy\n"
            "30::Broken (2002)::Action||Comedy\n40::Also Broken (2003)::Comedy|\n",
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
            self.assertEqual(report["files"]["ratings.dat"]["complete_records"], 4)
            self.assertEqual(report["files"]["ratings.dat"]["type_range_valid_records"], 2)
            self.assertEqual(report["files"]["ratings.dat"]["valid_records"], 1)
            self.assertEqual(report["files"]["movies.dat"]["complete_records"], 2)
            self.assertEqual(report["cross_table"]["missing_movie_references"], 2)
            reasons = {item["reason"] for item in report["anomalies"]}
            self.assertIn("exact_duplicate", reasons)
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

    def test_counts_accurate_fields_independently(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            (source / "users.dat").write_text("1::F::25::1::00123\n", encoding="iso-8859-1")
            (source / "movies.dat").write_text(
                "1::Good (2000)::Drama|Comedy\n2::No Year::Drama|Unknown\n",
                encoding="iso-8859-1",
            )
            (source / "ratings.dat").write_text(
                "1::1::5::946684800\n1::1::6::1000000000000\n",
                encoding="iso-8859-1",
            )

            files = inspect_dataset(source)["files"]

            self.assertEqual(
                (files["ratings.dat"]["accurate_fields_passed"], files["ratings.dat"]["accurate_fields_checked"]),
                (6, 8),
            )
            self.assertEqual(
                (files["users.dat"]["accurate_fields_passed"], files["users.dat"]["accurate_fields_checked"]),
                (4, 4),
            )
            self.assertEqual(
                (files["movies.dat"]["accurate_fields_passed"], files["movies.dat"]["accurate_fields_checked"]),
                (4, 5),
            )

    def test_counts_consistent_records_without_penalizing_exact_duplicates(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            (source / "users.dat").write_text(
                "1::F::25::1::00123\n2::M::35::2::90210\n2::F::35::2::90210\n",
                encoding="iso-8859-1",
            )
            (source / "movies.dat").write_text(
                "10::Good (2000)::Drama|Comedy\n"
                "10::Good (2000)::Drama|Comedy\n"
                "20::Bad (2001)::Drama|Drama\n",
                encoding="iso-8859-1",
            )
            (source / "ratings.dat").write_text(
                "1::10::5::946684800\n"
                "1::10::5::946684800\n"
                "1::10::4::946684801\n"
                "1::10::3::946684801\n"
                "1::99::5::946684802\n",
                encoding="iso-8859-1",
            )

            report = inspect_dataset(source)

            self.assertEqual(report["files"]["users.dat"]["consistent_records"], 1)
            self.assertEqual(report["files"]["movies.dat"]["consistent_records"], 2)
            self.assertEqual(report["files"]["ratings.dat"]["consistent_records"], 2)
            self.assertEqual(
                {item["reason"] for item in report["anomalies"]},
                {"conflict", "exact_duplicate", "genre_format", "missing_movie_reference"},
            )

            ratings = report["files"]["ratings.dat"]
            self.assertEqual(ratings["eligible_records"], 5)
            self.assertEqual(ratings["exact_duplicate_records"], 1)
            self.assertEqual(ratings["conflict_records"], 2)
            self.assertEqual(ratings["conflict_groups"], 1)

    def test_up_to_date_uses_inclusive_fixed_window_for_ratings_only(self):
        with tempfile.TemporaryDirectory() as temporary:
            source = Path(temporary)
            (source / "users.dat").write_text("1::F::25::1::00123\n", encoding="iso-8859-1")
            (source / "movies.dat").write_text("1::Good (2000)::Drama\n", encoding="iso-8859-1")
            timestamps = (UP_TO_DATE_WINDOW_START, MAX_TIMESTAMP, UP_TO_DATE_WINDOW_START - 1)
            (source / "ratings.dat").write_text(
                "".join(f"1::1::0::{timestamp}\n" for timestamp in timestamps)
                + "1::1::5::bad\n1::1::5::1046476799000\n",
                encoding="iso-8859-1",
            )

            report = inspect_dataset(source)

            self.assertEqual(report["files"]["ratings.dat"]["total_records"], 5)
            self.assertEqual(report["files"]["ratings.dat"]["up_to_date_records"], 2)
            self.assertNotIn("up_to_date_records", report["files"]["users.dat"])
            self.assertNotIn("up_to_date_records", report["files"]["movies.dat"])
            self.assertEqual(
                report["up_to_date_rule"],
                {
                    "applicable_tables": ["ratings.dat"],
                    "reference_timestamp": MAX_TIMESTAMP,
                    "window_days": UP_TO_DATE_WINDOW_DAYS,
                    "window_start_timestamp": UP_TO_DATE_WINDOW_START,
                },
            )


if __name__ == "__main__":
    unittest.main()

import unittest
from unittest.mock import patch

import search_engine


class FakeArrowTable:
    def __init__(self, rows):
        self.rows = rows

    def to_arrow(self):
        return self

    def to_pylist(self):
        return self.rows


class SearchBehaviorTests(unittest.TestCase):
    def setUp(self):
        self.rows = [
            {
                "file_name": "quarterly-report.pdf",
                "file_path": "/work/finance/quarterly-report.pdf",
                "file_type": "pdf",
                "content": "Revenue increased by 12 percent.",
            },
            {
                "file_name": "meeting-notes.txt",
                "file_path": "/work/meetings/meeting-notes.txt",
                "file_type": "txt",
                "content": "Discussed the quarterly reporting schedule.",
            },
        ]
        self.table_patch = patch.object(
            search_engine, "_table", FakeArrowTable(self.rows)
        )
        self.table_patch.start()
        self.addCleanup(self.table_patch.stop)

    def test_regex_search_is_limited_to_selected_field(self):
        results = search_engine.search(
            "quarterly", search_field="file_name", regex=True
        )

        self.assertEqual([result.file_name for result in results], ["quarterly-report.pdf"])
        self.assertEqual(results[0].match_type, "regex")

    def test_regex_search_respects_file_type_filter(self):
        results = search_engine.search(
            "report", regex=True, file_types={"txt"}, search_field="file_name"
        )

        self.assertEqual(results, [])

    def test_invalid_regular_expression_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Invalid regular expression"):
            search_engine.search("[", regex=True)

    def test_regex_can_search_file_content(self):
        results = search_engine.search(
            r"12\s+percent", search_field="content", regex=True
        )

        self.assertEqual([result.file_name for result in results], ["quarterly-report.pdf"])


if __name__ == "__main__":
    unittest.main()
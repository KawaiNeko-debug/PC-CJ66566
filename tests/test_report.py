import json
import tempfile
import unittest
from pathlib import Path

from openpyxl import load_workbook

from report import load_result_files, write_merged_json, write_xlsx


class ReportTests(unittest.TestCase):
    def test_merge_and_xlsx(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            first = root / "account-1" / "result.json"
            second = root / "account-2" / "result.json"
            first.parent.mkdir()
            second.parent.mkdir()
            first.write_text(
                json.dumps(
                    {
                        "results": [
                            {
                                "account_index": 1,
                                "masked_username": "ABC****0001",
                                "status": "completed",
                                "initial_chances": 2,
                                "exchange_succeeded": 3,
                                "draw_count": 5,
                                "remaining_chances": 0,
                                "prizes": [{"draw_index": 1, "title": "5金豆"}],
                                "completed_at": "2026-08-05 10:00:00",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            second.write_text(
                json.dumps(
                    {
                        "results": [
                            {
                                "account_index": 2,
                                "masked_username": "ABC****0002",
                                "status": "pending",
                                "initial_chances": 0,
                                "draw_count": 0,
                                "remaining_chances": 0,
                                "prizes": [],
                                "completed_at": "2026-08-04 10:00:00",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            records = load_result_files(root)
            self.assertEqual([row["account_index"] for row in records], [1, 2])

            merged = root / "merged.json"
            xlsx = root / "summary.xlsx"
            write_merged_json(merged, records)
            write_xlsx(xlsx, records)

            merged_payload = json.loads(merged.read_text(encoding="utf-8"))
            self.assertEqual(merged_payload["total_draws"], 5)
            workbook = load_workbook(xlsx)
            self.assertEqual(workbook["抽奖汇总"].max_row, 3)
            self.assertEqual(workbook["统计"]["B1"].value, 2)


if __name__ == "__main__":
    unittest.main()

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from openpyxl import load_workbook


MODULE_ROOT = Path(__file__).resolve().parents[1]
if str(MODULE_ROOT) not in sys.path:
    sys.path.insert(0, str(MODULE_ROOT))

from mall_report import (  # noqa: E402
    build_telegram_summary,
    load_result_files,
    main,
    summary_payload,
    write_xlsx,
)


class MallReportTests(unittest.TestCase):
    def sample_record(self) -> dict:
        return {
            "account_index": 1,
            "group_name": "group2",
            "masked_username": "A12****34C5",
            "customer_code": "A12B34C5",
            "status": "completed",
            "success": True,
            "detail": "商城抽奖完成，共执行 1 次",
            "initial_chances": 1,
            "draw_count": 1,
            "remaining_chances": 0,
            "unfinished": False,
            "completed_at": "2026-08-06 08:00:00",
            "prizes": [
                {
                    "draw_index": 1,
                    "title": "1000积分",
                    "prize_type": "point",
                    "valid_end": "2027-12-31 23:59:59",
                    "bind_required": False,
                }
            ],
        }

    def test_load_latest_result_and_build_workbook(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result_path = root / "result-1" / "result.json"
            result_path.parent.mkdir(parents=True)
            result_path.write_text(
                json.dumps({"results": [self.sample_record()]}, ensure_ascii=False),
                encoding="utf-8",
            )
            records = load_result_files(root)
            self.assertEqual(len(records), 1)
            summary = summary_payload(records)
            self.assertEqual(summary["total_draws"], 1)
            self.assertEqual(summary["prize_counts"], {"1000积分": 1})

            target = root / "summary.xlsx"
            write_xlsx(target, records)
            workbook = load_workbook(target)
            self.assertEqual(workbook.sheetnames, ["商城抽奖汇总", "统计", "奖品分布"])
            sheet = workbook["商城抽奖汇总"]
            self.assertEqual(sheet["B2"].value, "A12B34C5")
            self.assertEqual(sheet["C2"].value, "商城2组账号1")
            self.assertEqual(sheet["K2"].value, "1000积分")

    def test_telegram_summary(self):
        message = build_telegram_summary([self.sample_record()])
        self.assertIn("完成账号: 1", message)
        self.assertIn("1000积分: 1", message)

    def test_document_is_attempted_when_message_fails(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            result_path = root / "result.json"
            result_path.write_text(
                json.dumps({"results": [self.sample_record()]}, ensure_ascii=False),
                encoding="utf-8",
            )
            output = root / "summary.xlsx"
            argv = [
                "mall_report.py",
                str(root),
                "--merged-json",
                str(root / "merged.json"),
                "--xlsx",
                str(output),
                "--telegram",
            ]
            with (
                patch("sys.argv", argv),
                patch("mall_report.send_telegram_message", return_value=False),
                patch("mall_report.send_telegram_document", return_value=True) as document,
            ):
                self.assertEqual(main(), 0)
            document.assert_called_once_with(output)


if __name__ == "__main__":
    unittest.main()

import json
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from openpyxl import load_workbook

from report import (
    build_telegram_summary,
    load_result_files,
    send_telegram_document,
    send_telegram_message,
    timestamped_xlsx_path,
    write_merged_json,
    write_xlsx,
    merge_account_attempts,
    row_values,
)


class ReportTests(unittest.TestCase):
    def test_single_mobile_draw_keeps_unknown_counts_in_report(self):
        from lottery import PrizeRecord, execute_mobile_draw, result_template
        client = Mock()
        client.draw_once.return_value = (True, [PrizeRecord(1, "5金豆")], "")
        record = result_template("A001", 1)
        with patch.dict("os.environ", {"TOKEN_CHECK_ONLY": "false"}):
            execute_mobile_draw(client, record)
        merged = merge_account_attempts([record])
        self.assertIsNone(merged["initial_chances"])
        self.assertIsNone(merged["final_chance_check"])
        values = row_values(merged, 1)
        self.assertEqual(values[8], "未查询")
        self.assertEqual(values[10], 1)
        self.assertEqual(values[11], "未查询")
        self.assertEqual(values[12], "否")
        message = build_telegram_summary([merged])
        self.assertIn("抽奖成功: 1/1", message)
        self.assertIn("剩余次数: 未查询", message)
        self.assertNotIn("仍有剩余次数账号: 0", message)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "mobile.xlsx"
            write_xlsx(path, [merged])
            workbook = load_workbook(path)
            self.assertEqual(workbook.worksheets[0].cell(2, 9).value, "未查询")
            self.assertEqual(workbook.worksheets[0].cell(2, 12).value, "未查询")
            stats = dict(workbook["统计"].values)
            self.assertEqual(stats["原有抽奖次数合计"], "未查询")
            self.assertEqual(stats["仍有剩余次数账号"], "未查询")
            workbook.close()

    def test_timestamped_xlsx_path_includes_date_hour_and_minute(self):
        now = datetime(2026, 9, 3, 1, 26, tzinfo=timezone(timedelta(hours=8)))
        self.assertEqual(
            timestamped_xlsx_path(Path("merged/lottery-summary.xlsx"), now),
            Path("merged/lottery-summary-2026-09-03_01-26.xlsx"),
        )
        existing = Path("merged/lottery-summary-2026-09-03_01-26.xlsx")
        self.assertEqual(timestamped_xlsx_path(existing, now), existing)

    def test_telegram_sends_text_and_xlsx(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            xlsx = Path(temp_dir) / "summary.xlsx"
            xlsx.write_bytes(b"test-xlsx")
            response = Mock(status_code=200, text="ok")
            with (
                patch.dict(
                    "os.environ",
                    {"TELEGRAM_BOT_TOKEN": "token", "TELEGRAM_CHAT_ID": "chat"},
                ),
                patch("report.requests.post", return_value=response) as post,
            ):
                self.assertTrue(send_telegram_message("汇总"))
                self.assertTrue(send_telegram_document(xlsx))

            self.assertIn("/sendMessage", post.call_args_list[0].args[0])
            self.assertIn("/sendDocument", post.call_args_list[1].args[0])

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
                                "customer_code": "CUST0001",
                                "masked_username": "ABC****0001",
                                "status": "completed",
                                "success": True,
                                "initial_chances": 2,
                                "exchange_succeeded": 3,
                                "draw_count": 5,
                                "remaining_chances": 0,
                                "prizes": [
                                    {
                                        "draw_index": 1,
                                        "title": "5金豆",
                                        "won_at": "2026-08-05 09:58:03",
                                        "expiry_date": "2026-08-12",
                                    }
                                ],
                                "blind_box_coupons": [
                                    {"name": "SMT 25元优惠券"},
                                    {"name": "FA商城30元优惠券"},
                                ],
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
                                "customer_code": "CUST0002",
                                "masked_username": "ABC****0002",
                                "status": "pending",
                                "success": False,
                                "initial_chances": 0,
                                "draw_count": 0,
                                "remaining_chances": 2,
                                "final_chance_check": 2,
                                "unfinished": True,
                                "prizes": [],
                                "blind_box_coupons": [{"name": "SMT 25元优惠券"}],
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
            self.assertEqual(workbook["统计"]["B2"].value, 2)
            self.assertEqual(workbook["抽奖汇总"]["L3"].value, 2)
            self.assertEqual(workbook["抽奖汇总"]["M3"].value, "是")
            self.assertEqual(workbook["抽奖汇总"]["O1"].value, "抽奖1")
            self.assertEqual(workbook["抽奖汇总"]["P1"].value, "过期时间")
            self.assertEqual(workbook["抽奖汇总"]["O2"].value, "5金豆")
            self.assertEqual(workbook["抽奖汇总"]["F2"].value, "2026-08-05 09:58")
            self.assertEqual(
                workbook["抽奖汇总"]["P2"].value.strftime("%Y-%m-%d"), "2026-08-12"
            )
            self.assertEqual(workbook["抽奖汇总"].freeze_panes, "G2")
            self.assertIn("SMT 25元优惠券", workbook.sheetnames)
            coupon_sheet = workbook["SMT 25元优惠券"]
            self.assertEqual(coupon_sheet.max_column, 1)
            self.assertEqual(coupon_sheet["A1"].value, "客编")
            self.assertEqual(coupon_sheet["A2"].value, "CUST0001")
            self.assertEqual(coupon_sheet["A3"].value, "CUST0002")

            message = build_telegram_summary(records)
            self.assertIn("总账号数: 2", message)
            self.assertIn("抽奖成功: 1/2", message)
            self.assertIn("5金豆: 1", message)
            self.assertIn("未完成账号: 1", message)
            self.assertIn("仍有剩余次数账号: 1", message)
            self.assertIn("ABC****0002: 最终校验剩余 2 次", message)
            self.assertIn("SMT 25元优惠券: 2 个账号", message)

    def test_failed_account_is_unfinished_even_when_remaining_is_zero(self):
        records = [
            {
                "account_index": 1,
                "status": "partial",
                "success": False,
                "detail": "活动已结束",
                "final_chance_check": 0,
                "remaining_chances": 0,
                "prizes": [],
            }
        ]
        message = build_telegram_summary(records)
        self.assertIn("未完成账号: 1", message)
        self.assertIn("仍有剩余次数账号: 0", message)
        self.assertIn("活动已结束", message)

    def test_newer_retry_result_replaces_initial_slider_failure(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            initial = root / "result-group1-1" / "result.json"
            retry = root / "result-group1-1-retry1" / "result.json"
            initial.parent.mkdir()
            retry.parent.mkdir()
            initial.write_text(
                json.dumps(
                    {
                        "results": [
                            {
                                "account_index": 1,
                                "status": "failed",
                                "success": False,
                                "detail": "滑块验证未通过",
                                "completed_at": "2026-08-05 10:00:00",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            retry.write_text(
                json.dumps(
                    {
                        "results": [
                            {
                                "account_index": 1,
                                "status": "completed",
                                "success": True,
                                "detail": "所有可用抽奖次数已执行完毕",
                                "completed_at": "2026-08-05 10:05:00",
                            }
                        ]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            records = load_result_files(root)
            self.assertEqual(len(records), 1)
            self.assertTrue(records[0]["success"])
            self.assertEqual(records[0]["status"], "completed")

    def test_retry_result_preserves_prizes_and_totals_from_all_attempts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            initial = root / "result-group1-1" / "result.json"
            retry = root / "result-group1-1-retry1" / "result.json"
            initial.parent.mkdir()
            retry.parent.mkdir()
            initial.write_text(
                json.dumps(
                    {
                        "results": [{
                            "account_index": 1,
                            "status": "partial",
                            "success": False,
                            "detail": "活动已结束",
                            "initial_points": 20,
                            "initial_chances": 2,
                            "exchange_attempted": 1,
                            "exchange_succeeded": 0,
                            "draw_count": 2,
                            "prizes": [
                                {"draw_index": 1, "title": "6金豆", "won_at": "2026-09-03 00:01:00"},
                                {"draw_index": 2, "title": "6金豆", "won_at": "2026-09-03 00:02:00"},
                            ],
                            "blind_box_coupons": [{"name": "SMT 25元现金券"}],
                            "completed_at": "2026-09-03 00:03:00",
                        }]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
            retry.write_text(
                json.dumps(
                    {
                        "results": [{
                            "account_index": 1,
                            "status": "completed",
                            "success": True,
                            "detail": "所有可用抽奖次数已执行完毕",
                            "initial_points": 32,
                            "final_points": 35,
                            "initial_chances": 0,
                            "exchange_attempted": 3,
                            "exchange_succeeded": 3,
                            "draw_count": 3,
                            "prizes": [
                                {"draw_index": 1, "title": "6金豆", "won_at": "2026-09-03 00:11:00"},
                                {"draw_index": 2, "title": "6金豆", "won_at": "2026-09-03 00:12:00"},
                                {"draw_index": 3, "title": "6金豆", "won_at": "2026-09-03 00:13:00"},
                            ],
                            "blind_box_coupons": [{"name": "SMT 25元现金券"}],
                            "completed_at": "2026-09-03 00:14:00",
                        }]
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )

            record = load_result_files(root)[0]
            self.assertEqual(record["initial_points"], 20)
            self.assertEqual(record["initial_chances"], 2)
            self.assertEqual(record["exchange_attempted"], 4)
            self.assertEqual(record["exchange_succeeded"], 3)
            self.assertEqual(record["draw_count"], 5)
            self.assertEqual([item["draw_index"] for item in record["prizes"]], [1, 2, 3, 4, 5])
            self.assertEqual(len(record["blind_box_coupons"]), 1)


if __name__ == "__main__":
    unittest.main()

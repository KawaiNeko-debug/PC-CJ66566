import unittest

from lottery import (
    api_succeeded,
    derive_status,
    parse_exchange_detail,
    parse_lucky_count,
    parse_turn_prizes,
)


class LotteryParsingTests(unittest.TestCase):
    def test_success_false_is_never_accepted_even_with_code_200(self):
        self.assertFalse(api_succeeded({"success": False, "code": 200}))

    def test_existing_two_plus_three_exchange_quota(self):
        detail = parse_exchange_detail(
            {
                "success": True,
                "data": {"price": 5, "status": 5, "exchangeNum": 0, "exchangeMaxNum": 3},
            }
        )
        self.assertEqual(detail["remaining"], 3)
        self.assertEqual(2 + detail["remaining"], 5)

    def test_zero_chances_still_has_three_exchange_quota(self):
        detail = parse_exchange_detail(
            {"success": True, "data": {"exchangeNum": 0, "exchangeMaxNum": 3}}
        )
        self.assertEqual(detail["remaining"], 3)
        self.assertEqual(0 + detail["remaining"], 3)

    def test_previous_exchange_reduces_only_remaining_quota(self):
        detail = parse_exchange_detail(
            {"success": True, "data": {"exchangeNum": 2, "exchangeMaxNum": 3}}
        )
        self.assertEqual(detail["remaining"], 1)

    def test_lucky_count(self):
        self.assertEqual(
            parse_lucky_count({"success": True, "data": {"activityCode": "LAMD", "count": 5}}),
            5,
        )

    def test_turn_prize_list(self):
        rows = parse_turn_prizes(
            {
                "success": True,
                "data": {
                    "turnCode": "T1",
                    "prizeList": [{"prizeTitle": "55金豆", "prizeCode": "PBO"}],
                },
            },
            4,
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].draw_index, 4)
        self.assertEqual(rows[0].title, "55金豆")
        self.assertEqual(rows[0].turn_code, "T1")

    def test_status_keeps_not_started_as_pending(self):
        self.assertEqual(derive_status("", "活动还未开始", 5), ("pending", False))

    def test_status_completed_only_when_no_error_and_empty(self):
        self.assertEqual(derive_status("", "", 0), ("completed", True))
        self.assertEqual(derive_status("金豆不足", "", 0), ("partial", False))


if __name__ == "__main__":
    unittest.main()

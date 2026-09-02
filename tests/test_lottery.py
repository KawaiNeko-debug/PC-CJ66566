import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from lottery import (
    DEFAULT_ACTIVITY_ACCESS_ID,
    DEFAULT_ACTIVITY_CODE,
    EXCHANGE_CHANCE_PATH,
    VOUCHER_DETAIL_PATH,
    PrizeRecord,
    api_succeeded,
    derive_status,
    is_insufficient_points,
    parse_blind_box_coupons,
    parse_activity_config,
    parse_exchange_detail,
    parse_lucky_count,
    parse_turn_prizes,
    run_draw_exchange_cycle,
    write_result,
)


class LotteryParsingTests(unittest.TestCase):
    def test_current_exchange_api_family(self):
        self.assertEqual(
            VOUCHER_DETAIL_PATH,
            "/api/integral/brand/activity/ns/getVoucherLotteryDetail",
        )
        self.assertEqual(
            EXCHANGE_CHANCE_PATH,
            "/api/integral/brand/activity/exchangeLotteryChance",
        )

    def test_parse_server_rendered_activity_config(self):
        config = parse_activity_config(
            [
                "window.__NUXT__={data:[{activityAccessId:'bf69c3403f094a52a787bfae528da7ea',"
                "activityConfig:{activityName:'2026年第二期会员日',lotteryActivityId:'LAJJT'}}]}"
            ]
        )
        self.assertEqual(config["activity_access_id"], "bf69c3403f094a52a787bfae528da7ea")
        self.assertEqual(config["activity_code"], "LAJJT")
        self.assertEqual(config["activity_name"], "2026年第二期会员日")

    def test_missing_server_rendered_activity_config_is_empty(self):
        self.assertEqual(
            parse_activity_config(["window.__NUXT__={data:[]}"]),
            {"activity_access_id": "", "activity_code": "", "activity_name": ""},
        )

    def test_empty_activity_overrides_fall_back_in_result_file(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(
            os.environ,
            {"ACTIVITY_ACCESS_ID": "", "LOTTERY_ACTIVITY_CODE": ""},
        ):
            output = Path(directory) / "result.json"
            write_result(output, {"activity_access_id": "", "activity_code": ""})
            payload = json.loads(output.read_text(encoding="utf-8"))
        self.assertEqual(payload["activity_access_id"], DEFAULT_ACTIVITY_ACCESS_ID)
        self.assertEqual(payload["activity_code"], DEFAULT_ACTIVITY_CODE)

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
            parse_lucky_count({"success": True, "data": {"activityCode": "LAJJT", "count": 5}}),
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

    def test_blind_box_coupon_list(self):
        rows = parse_blind_box_coupons(
            {
                "success": True,
                "data": {
                    "canPopup": True,
                    "giftConfigList": [
                        {
                            "couponName": "SMT 25元优惠券",
                            "couponAmount": "25",
                            "businessLine": "SMT",
                            "couponType": 1,
                        },
                        {
                            "couponName": "SMT 25元优惠券",
                            "couponAmount": "25",
                            "businessLine": "SMT",
                            "couponType": 1,
                        },
                    ],
                },
            }
        )
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].name, "SMT 25元优惠券")
        self.assertEqual(rows[0].amount, "25")

    def test_status_keeps_not_started_as_pending(self):
        self.assertEqual(derive_status("", "活动还未开始", 5), ("pending", False))

    def test_status_completed_only_when_no_error_and_empty(self):
        self.assertEqual(derive_status("", "", 0), ("completed", True))
        self.assertEqual(derive_status("金豆不足", "", 0), ("completed", True))
        self.assertEqual(derive_status("兑换接口异常", "", 0), ("partial", False))

    def test_insufficient_points_detection(self):
        self.assertTrue(is_insufficient_points("金豆不足"))
        self.assertTrue(is_insufficient_points("可用积分不足"))
        self.assertFalse(is_insufficient_points("活动未开始"))


class FakeCycleClient:
    def __init__(self, points: int, chances: int = 0) -> None:
        self.points = points
        self.chances = chances
        self.exchange_num = 0

    def lucky_count(self) -> int:
        return self.chances

    def exchange_detail(self) -> dict[str, int]:
        return {
            "price": 5,
            "status": 5,
            "exchange_num": self.exchange_num,
            "exchange_max": 3,
            "remaining": 3 - self.exchange_num,
        }

    def exchange_once(self, attempt: int) -> tuple[bool, str]:
        if self.points < 5:
            return False, "金豆不足"
        self.points -= 5
        self.exchange_num += 1
        self.chances += 1
        return True, ""

    def draw_once(self, draw_index: int) -> tuple[bool, list[PrizeRecord], str]:
        if self.chances <= 0:
            return False, [], "抽奖次数不足"
        self.chances -= 1
        self.points += 5
        return True, [PrizeRecord(draw_index=draw_index, title="5金豆")], ""


class LotteryCycleTests(unittest.TestCase):
    @patch("lottery.random_delay")
    def test_draw_reward_funds_the_next_exchange(self, _delay):
        client = FakeCycleClient(points=7)
        result = run_draw_exchange_cycle(client, 0, client.exchange_detail())

        self.assertEqual(result.exchange_attempted, 3)
        self.assertEqual(result.exchange_succeeded, 3)
        self.assertEqual(len(result.prizes), 3)
        self.assertEqual(result.remaining_chances, 0)
        self.assertEqual(client.points, 7)

    @patch("lottery.random_delay")
    def test_no_points_stops_without_forcing_all_quota(self, _delay):
        client = FakeCycleClient(points=0)
        result = run_draw_exchange_cycle(client, 0, client.exchange_detail())

        self.assertEqual(result.exchange_attempted, 1)
        self.assertEqual(result.exchange_succeeded, 0)
        self.assertEqual(result.exchange_message, "金豆不足")
        self.assertEqual(derive_status(result.exchange_message, "", 0), ("completed", True))


if __name__ == "__main__":
    unittest.main()

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_ROOT = Path(__file__).resolve().parents[1]
if str(MODULE_ROOT) not in sys.path:
    sys.path.insert(0, str(MODULE_ROOT))

from mall_lottery import (  # noqa: E402
    DEFAULT_ACTIVITY_UUID,
    MALL_DOMAIN,
    MallPrizeRecord,
    _initialize_mall_session,
    _mall_login_url,
    activity_uuid,
    parse_available_chances,
    parse_draw_prize,
    run_draw_cycle,
)


class MallParsingTests(unittest.TestCase):
    def test_mall_login_uses_sso_callback_parameters(self):
        url = _mall_login_url()
        self.assertIn("/window/login?", url)
        self.assertIn("appId=", url)
        self.assertIn("redirectUrl=", url)
        self.assertNotIn("?redirect=", url)

    @patch("mall_lottery.goto_with_retry")
    def test_initialize_mall_session_visits_campaign_and_only_returns_names(self, goto):
        class FakePage:
            pass

        class FakeContext:
            def cookies(self, url):
                self.url = url
                return [
                    {"name": "activity_seed", "value": "redacted"},
                    {"name": "PRO_NEW_SID", "value": "redacted"},
                ]

        names = _initialize_mall_session(FakePage(), FakeContext())
        goto.assert_called_once()
        self.assertEqual(names, {"activity_seed", "PRO_NEW_SID"})

    def test_domain_is_constructed_at_runtime(self):
        expected = "".join(chr(code) for code in (115, 122, 108, 99, 115, 99)) + ".com"
        self.assertEqual(MALL_DOMAIN, expected)

    def test_activity_uuid_validation(self):
        with patch.dict(os.environ, {"MALL_ACTIVITY_UUID": DEFAULT_ACTIVITY_UUID.lower()}):
            self.assertEqual(activity_uuid(), DEFAULT_ACTIVITY_UUID)
        with patch.dict(os.environ, {"MALL_ACTIVITY_UUID": "bad"}):
            with self.assertRaisesRegex(RuntimeError, "32 位"):
                activity_uuid()

    def test_parse_available_chances(self):
        payload = {
            "code": 200,
            "ok": True,
            "result": {"drawNum": 3, "quantityParticipate": 1},
        }
        self.assertEqual(parse_available_chances(payload), 3)

    def test_parse_draw_prize(self):
        payload = {
            "code": 200,
            "ok": True,
            "result": {
                "lotteryActivityPrizeId": 1655,
                "prizeName": "1000积分",
                "prizeType": "point",
                "prizeValidBeginTime": "2026-08-05 23:59:45",
                "prizeValidEndTime": "2027-12-31 23:59:59",
                "isBindWechat": False,
            },
        }
        prize = parse_draw_prize(payload, 2)
        self.assertEqual(prize.draw_index, 2)
        self.assertEqual(prize.title, "1000积分")
        self.assertEqual(prize.prize_type, "point")
        self.assertEqual(prize.expiry_date, "2027-12-31")


class FakeMallClient:
    def __init__(self):
        self.remaining = [1, 0]
        self.draws: list[int] = []

    def draw_once(self, draw_index: int) -> MallPrizeRecord:
        self.draws.append(draw_index)
        return MallPrizeRecord(draw_index=draw_index, title=f"奖品{draw_index}")

    def available_chances(self) -> int:
        return self.remaining.pop(0)


class MallCycleTests(unittest.TestCase):
    @patch("mall_lottery.random_delay")
    def test_draws_until_state_reaches_zero(self, _delay):
        client = FakeMallClient()
        result = run_draw_cycle(client, 2)
        self.assertEqual(client.draws, [1, 2])
        self.assertEqual(result.draw_count, 2)
        self.assertEqual(result.remaining_chances, 0)
        self.assertEqual([row.title for row in result.prizes], ["奖品1", "奖品2"])

    @patch("mall_lottery.random_delay")
    def test_safety_cap_stops_non_decreasing_state(self, _delay):
        client = FakeMallClient()
        client.remaining = [5]
        with patch.dict(os.environ, {"MALL_DRAW_SAFETY_CAP": "1"}):
            result = run_draw_cycle(client, 5)
        self.assertEqual(result.draw_count, 1)
        self.assertEqual(result.remaining_chances, 5)
        self.assertIn("安全上限", result.message)

    @patch("mall_lottery.random_delay")
    def test_preserves_completed_prizes_when_later_draw_fails(self, _delay):
        client = FakeMallClient()
        original = client.draw_once

        def fail_second(draw_index):
            if draw_index == 2:
                raise RuntimeError("接口暂时失败")
            return original(draw_index)

        client.draw_once = fail_second
        result = run_draw_cycle(client, 2)
        self.assertEqual(result.draw_count, 1)
        self.assertEqual([row.title for row in result.prizes], ["奖品1"])
        self.assertEqual(result.remaining_chances, 1)
        self.assertIn("暂时失败", result.message)


if __name__ == "__main__":
    unittest.main()
import os
import sys
import unittest
from pathlib import Path
from unittest.mock import patch


MODULE_ROOT = Path(__file__).resolve().parents[1]
if str(MODULE_ROOT) not in sys.path:
    sys.path.insert(0, str(MODULE_ROOT))

from mall_lottery import (  # noqa: E402
    DEFAULT_ACTIVITY_UUID,
    MALL_DOMAIN,
    MallPrizeRecord,
    _initialize_mall_session,
    activity_uuid,
    parse_available_chances,
    parse_draw_prize,
    run_draw_cycle,
)


class MallParsingTests(unittest.TestCase):
    @patch("mall_lottery.goto_with_retry")
    def test_initialize_mall_session_visits_campaign_and_only_returns_names(self, goto):
        class FakePage:
            pass

        class FakeContext:
            def cookies(self, url):
                self.url = url
                return [
                    {"name": "activity_seed", "value": "redacted"},
                    {"name": "PRO_NEW_SID", "value": "redacted"},
                ]

        names = _initialize_mall_session(FakePage(), FakeContext())
        goto.assert_called_once()
        self.assertEqual(names, {"activity_seed", "PRO_NEW_SID"})

    def test_domain_is_constructed_at_runtime(self):
        expected = "".join(chr(code) for code in (115, 122, 108, 99, 115, 99)) + ".com"
        self.assertEqual(MALL_DOMAIN, expected)

    def test_activity_uuid_validation(self):
        with patch.dict(os.environ, {"MALL_ACTIVITY_UUID": DEFAULT_ACTIVITY_UUID.lower()}):
            self.assertEqual(activity_uuid(), DEFAULT_ACTIVITY_UUID)
        with patch.dict(os.environ, {"MALL_ACTIVITY_UUID": "bad"}):
            with self.assertRaisesRegex(RuntimeError, "32 位"):
                activity_uuid()

    def test_parse_available_chances(self):
        payload = {
            "code": 200,
            "ok": True,
            "result": {"drawNum": 3, "quantityParticipate": 1},
        }
        self.assertEqual(parse_available_chances(payload), 3)

    def test_parse_draw_prize(self):
        payload = {
            "code": 200,
            "ok": True,
            "result": {
                "lotteryActivityPrizeId": 1655,
                "prizeName": "1000积分",
                "prizeType": "point",
                "prizeValidBeginTime": "2026-08-05 23:59:45",
                "prizeValidEndTime": "2027-12-31 23:59:59",
                "isBindWechat": False,
            },
        }
        prize = parse_draw_prize(payload, 2)
        self.assertEqual(prize.draw_index, 2)
        self.assertEqual(prize.title, "1000积分")
        self.assertEqual(prize.prize_type, "point")
        self.assertEqual(prize.expiry_date, "2027-12-31")


class FakeMallClient:
    def __init__(self):
        self.remaining = [1, 0]
        self.draws: list[int] = []

    def draw_once(self, draw_index: int) -> MallPrizeRecord:
        self.draws.append(draw_index)
        return MallPrizeRecord(draw_index=draw_index, title=f"奖品{draw_index}")

    def available_chances(self) -> int:
        return self.remaining.pop(0)


class MallCycleTests(unittest.TestCase):
    @patch("mall_lottery.random_delay")
    def test_draws_until_state_reaches_zero(self, _delay):
        client = FakeMallClient()
        result = run_draw_cycle(client, 2)
        self.assertEqual(client.draws, [1, 2])
        self.assertEqual(result.draw_count, 2)
        self.assertEqual(result.remaining_chances, 0)
        self.assertEqual([row.title for row in result.prizes], ["奖品1", "奖品2"])

    @patch("mall_lottery.random_delay")
    def test_safety_cap_stops_non_decreasing_state(self, _delay):
        client = FakeMallClient()
        client.remaining = [5]
        with patch.dict(os.environ, {"MALL_DRAW_SAFETY_CAP": "1"}):
            result = run_draw_cycle(client, 5)
        self.assertEqual(result.draw_count, 1)
        self.assertEqual(result.remaining_chances, 5)
        self.assertIn("安全上限", result.message)

    @patch("mall_lottery.random_delay")
    def test_preserves_completed_prizes_when_later_draw_fails(self, _delay):
        client = FakeMallClient()
        original = client.draw_once

        def fail_second(draw_index):
            if draw_index == 2:
                raise RuntimeError("接口暂时失败")
            return original(draw_index)

        client.draw_once = fail_second
        result = run_draw_cycle(client, 2)
        self.assertEqual(result.draw_count, 1)
        self.assertEqual([row.title for row in result.prizes], ["奖品1"])
        self.assertEqual(result.remaining_chances, 1)
        self.assertIn("暂时失败", result.message)


if __name__ == "__main__":
    unittest.main()

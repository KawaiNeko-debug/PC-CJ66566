import os
import unittest
from unittest.mock import MagicMock, patch

import requests

from lottery import (
    BLIND_BOX_PATH,
    DEFAULT_ACTIVITY_CODE,
    LUCKY_COUNT_PATH,
    TURN_PATH,
    USER_INFO_PATH,
    CampaignClient,
    LotteryError,
    PrizeRecord,
    SSOData,
    execute_account,
    extract_access_token,
    run_3d_draw_cycle,
)


class TokenTests(unittest.TestCase):
    def test_headers_are_case_insensitive(self):
        self.assertEqual(
            extract_access_token({"X-Abc-Accesstoken": " runtime-token "}, {}, "x-abc-accesstoken"),
            "runtime-token",
        )

    def test_storage_supports_nested_json_and_plain_token(self):
        for storage in (
            {"access_token": "runtime-token"},
            {"profile": '{"data":{"accessToken":"runtime-token"}}'},
            {"token": '{"access_token":"runtime-token"}'},
        ):
            with self.subTest(storage=storage):
                self.assertEqual(extract_access_token({}, storage, "x-abc-accesstoken"), "runtime-token")
        self.assertEqual(extract_access_token({}, {"refresh_token": "wrong", "password": "wrong"}, ""), "")


class MobileClientTests(unittest.TestCase):
    def setUp(self):
        self.environment = patch.dict(os.environ, {
            "LOTTERY_MODE": "3d", "ACCESS_TOKEN_RETRIES": "3",
            "ACCESS_TOKEN_RETRY_DELAY_SECONDS": "1",
        }, clear=True)
        self.environment.start()
        self.addCleanup(self.environment.stop)
        for name, value in (("BRAND_LOWER", "abc"), ("ROOT_DOMAIN", "abc.com"),
                            ("WWW_ORIGIN", "https://www.abc.com")):
            current = patch(f"lottery.{name}", value)
            current.start()
            self.addCleanup(current.stop)
        self.page = MagicMock()
        self.holder = {"value": "runtime-token"}
        self.client = CampaignClient(self.page, SSOData("A001", "sid", "session", "key"),
                                     {"value": "dynamic-key"}, self.holder)
        self.client.api_session = MagicMock()

    @staticmethod
    def response(status=200, payload=None):
        response = MagicMock()
        response.status_code = status
        response.ok = status < 400
        response.json.return_value = payload if payload is not None else {"success": True, "data": {"count": 1}}
        return response

    def test_count_and_draw_match_mobile_capture(self):
        self.client.api_session.request.return_value = self.response()
        self.assertEqual(self.client.lucky_count(), 1)
        call = self.client.api_session.request.call_args
        self.assertEqual(call.args, ("POST", f"https://m.abc.com{LUCKY_COUNT_PATH}"))
        self.assertEqual(call.kwargs["json"], {"activityCode": "LAJ3T"})
        self.assertEqual(call.kwargs["headers"]["x-abc-clienttype"], "MP-WEIXIN")
        self.assertEqual(call.kwargs["headers"]["x-abc-accesstoken"], "runtime-token")
        self.assertEqual(call.kwargs["headers"]["secretkey"], "dynamic-key")
        self.assertFalse(call.kwargs["allow_redirects"])
        self.client.api_session.request.return_value = self.response(payload={
            "success": True, "data": {"prizeList": [{"prizeTitle": "5金豆"}]},
        })
        with patch("lottery.random_delay"):
            self.assertTrue(self.client.draw_once(1)[0])
        call = self.client.api_session.request.call_args
        self.assertEqual(call.args, ("POST", f"https://m.abc.com{TURN_PATH}"))
        self.assertEqual(call.kwargs["json"], {"clientType": "MP-WEIXIN", "activityCode": "LAJ3T"})

    def test_pc_identity_check_stays_on_page(self):
        self.page.evaluate.return_value = {"ok": True, "data": {"body": {"customerCode": "A001"}}}
        with patch("lottery.goto_with_retry"):
            self.client.open_and_verify()
        self.assertEqual(self.page.evaluate.call_args.args[1]["path"], USER_INFO_PATH)
        self.client.api_session.request.assert_not_called()

    def test_token_waits_and_reloads_when_initially_missing(self):
        self.holder["value"] = ""
        self.page.evaluate.side_effect = [{}, {}, {"accessToken": "late-token"}]
        self.assertEqual(self.client.refresh_access_token(), "late-token")
        self.assertEqual(self.page.reload.call_count, 2)
        self.assertEqual(self.page.wait_for_timeout.call_count, 2)

    def test_header_capture_is_accepted_after_reload(self):
        self.holder["value"] = ""
        self.page.evaluate.return_value = {}
        self.page.reload.side_effect = lambda **kwargs: self.holder.update(value="captured-token")
        self.assertEqual(self.client.refresh_access_token(), "captured-token")

    def test_missing_token_fails_without_any_api_request(self):
        self.holder["value"] = ""
        self.page.evaluate.return_value = {}
        with self.assertRaisesRegex(LotteryError, "未能.*获取活动访问令牌"):
            self.client.lucky_count()
        self.client.api_session.request.assert_not_called()
        self.assertEqual(self.page.reload.call_count, 2)

    def test_page_timeout_during_token_refresh_is_retried(self):
        from playwright.sync_api import TimeoutError as PlaywrightTimeoutError

        self.page.evaluate.side_effect = [{}, {"accessToken": "refreshed-token"}]
        self.page.reload.side_effect = PlaywrightTimeoutError("mock page timeout")
        self.assertEqual(self.client.refresh_access_token(force_reload=True), "refreshed-token")
        self.assertEqual(self.page.reload.call_count, 2)

    def test_authentication_rejection_refreshes_once(self):
        self.page.evaluate.return_value = {"accessToken": "refreshed-token"}
        seen_tokens = []

        def request(*args, **kwargs):
            seen_tokens.append(kwargs["headers"]["x-abc-accesstoken"])
            return self.response(401) if len(seen_tokens) == 1 else self.response()

        self.client.api_session.request.side_effect = request
        self.assertEqual(self.client.lucky_count(), 1)
        self.assertEqual(seen_tokens, ["runtime-token", "refreshed-token"])
        self.page.reload.assert_called_once()

    def test_business_auth_failure_is_retried_but_other_failure_is_not(self):
        self.page.evaluate.return_value = {"accessToken": "refreshed-token"}
        self.client.api_session.request.side_effect = [
            self.response(payload={"success": False, "code": 401}), self.response(),
        ]
        self.assertEqual(self.client.lucky_count(), 1)
        self.assertEqual(self.client.api_session.request.call_count, 2)
        self.client.api_session.request.reset_mock(side_effect=True)
        self.client.api_session.request.return_value = self.response(payload={
            "success": False, "message": "活动未开始",
        })
        self.client.request("POST", TURN_PATH, {})
        self.client.api_session.request.assert_called_once()

    def test_stale_token_is_not_reused_and_auth_retries_are_bounded(self):
        self.page.evaluate.return_value = {"accessToken": "runtime-token"}
        self.client.api_session.request.return_value = self.response(401)
        with self.assertRaisesRegex(LotteryError, "鉴权失败"):
            self.client.lucky_count()
        self.client.api_session.request.assert_called_once()

    def test_second_auth_rejection_stops(self):
        self.page.evaluate.return_value = {"accessToken": "refreshed-token"}
        self.client.api_session.request.return_value = self.response(401)
        with self.assertRaisesRegex(LotteryError, "鉴权失败"):
            self.client.lucky_count()
        self.assertEqual(self.client.api_session.request.call_count, 2)

    def test_draw_timeout_is_never_replayed(self):
        self.client.api_session.request.side_effect = requests.Timeout("mock timeout")
        with self.assertRaises(requests.Timeout):
            self.client.request("POST", TURN_PATH, {"activityCode": "LAJ3T"})
        self.client.api_session.request.assert_called_once()
        self.page.reload.assert_not_called()

    def test_default_campaign_uses_existing_request_and_body(self):
        with patch.dict(os.environ, {"LOTTERY_MODE": "campaign"}):
            client = CampaignClient(self.page, self.client.sso, {"value": "key"})
        self.page.evaluate.return_value = {"ok": True, "data": {"success": True, "data": {"count": 2}}}
        self.assertEqual(client.lucky_count(), 2)
        self.assertEqual(self.page.evaluate.call_args.args[1]["payload"], {"activityCode": DEFAULT_ACTIVITY_CODE})
        with patch.object(client, "request", return_value={"success": True, "data": {}}) as request:
            with patch("lottery.random_delay"):
                client.draw_once(1)
        request.assert_called_once_with("POST", TURN_PATH, {
            "clientType": "WEB", "activityCode": DEFAULT_ACTIVITY_CODE, "luckyKey": "",
        })


class MobileAccountTests(unittest.TestCase):
    def test_zero_chances_skip_draw_and_never_exchange(self):
        client = MagicMock()
        cycle = run_3d_draw_cycle(client, 0)
        self.assertEqual(cycle.prizes, [])
        self.assertEqual(cycle.exchange_attempted, 0)
        self.assertEqual(client.mock_calls, [])

    def test_multiple_chances_still_submit_only_once(self):
        client = MagicMock()
        client.draw_once.return_value = (True, [PrizeRecord(1, "5金豆")], "")
        cycle = run_3d_draw_cycle(client, 5)
        client.draw_once.assert_called_once_with(1)
        client.exchange_detail.assert_not_called()
        client.exchange_once.assert_not_called()
        self.assertEqual(cycle.remaining_chances, 4)

    def test_account_checks_remaining_chances_and_preserves_report_flags(self):
        for initial, remaining in ((0, 0), (1, 0), (5, 4), (1, LotteryError("mock count failure"))):
            with self.subTest(initial=initial), patch.dict(os.environ, {"LOTTERY_MODE": "3d"}), \
                 patch("lottery.require_brand_token"), patch("lottery.sync_playwright"), \
                 patch("lottery.login_passport"), patch("lottery.inject_pc_sso"), \
                 patch("lottery.establish_pc_sso", return_value=SSOData("A001", "sid", "session", "key")), \
                 patch("lottery.CampaignClient") as factory:
                client = factory.return_value
                client.is_3d = True
                client.activity_name = "3D抽奖"
                client.activity_code = "LAJ3T"
                client.activity_access_id = ""
                client.open_and_verify.return_value = {"body": {"customerCode": "A001"}}
                client.lucky_count.side_effect = [initial, remaining]
                client.draw_once.return_value = (True, [PrizeRecord(1, "5金豆")], "")
                result = execute_account("A001", "dummy-password", 1)
                self.assertEqual(client.lucky_count.call_count, 2)
                self.assertEqual(client.draw_once.call_count, 1 if initial else 0)
                client.exchange_detail.assert_not_called()
                client.exchange_once.assert_not_called()
                self.assertFalse(any(call.args[1:2] == (BLIND_BOX_PATH,) for call in client.request.call_args_list))
                if isinstance(remaining, Exception):
                    self.assertEqual(result["draw_count"], 1)
                    self.assertEqual(len(result["prizes"]), 1)
                    self.assertEqual(result["status"], "failed")
                    self.assertIsNone(result["final_chance_check"])
                    self.assertIn("最终抽奖次数校验失败", result["detail"])
                    continue
                self.assertEqual(result["final_chance_check"], remaining)
                self.assertEqual(result["remaining_chances"], remaining)
                self.assertEqual(result["unfinished"], remaining > 0)
                self.assertEqual(result["status"], "partial" if remaining else "completed")
                self.assertEqual(result["blind_box_coupons"], [])


if __name__ == "__main__":
    unittest.main()

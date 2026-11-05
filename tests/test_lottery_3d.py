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
    login_mobile,
    execute_mobile_draw,
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
        self.assertEqual(call.kwargs["headers"]["secretkey"], "defaultKeyId".encode().hex())
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

    def test_mobile_issuer_supplies_initial_token_without_pc_storage(self):
        self.holder["value"] = ""
        issuer = MagicMock(return_value="issued-mobile-token")
        self.client.access_token_refresh = issuer
        self.client.api_session.request.return_value = self.response()
        self.assertEqual(self.client.lucky_count(), 1)
        issuer.assert_called_once_with()
        self.assertEqual(self.client.api_session.request.call_args.kwargs["headers"]["x-abc-accesstoken"], "issued-mobile-token")
        self.page.evaluate.assert_not_called()
        self.page.reload.assert_not_called()

    def test_mobile_auth_refresh_reissues_token_and_caches_it(self):
        issuer = MagicMock(return_value="reissued-mobile-token")
        self.client.access_token_refresh = issuer
        self.client.api_session.request.side_effect = [self.response(401), self.response()]
        self.assertEqual(self.client.lucky_count(), 1)
        issuer.assert_called_once_with()
        self.assertEqual(self.holder["value"], "reissued-mobile-token")
        self.page.evaluate.assert_not_called()
        self.page.reload.assert_not_called()

    @patch("lottery.time.sleep")
    def test_mobile_token_issuance_has_bounded_retries(self, sleep):
        self.holder["value"] = ""
        self.client.access_token_refresh = MagicMock(side_effect=[
            requests.Timeout(), LotteryError("temporary"), "recovered-mobile-token",
        ])
        self.assertEqual(self.client.refresh_access_token(), "recovered-mobile-token")
        self.assertEqual(sleep.call_count, 2)
        self.client.access_token_refresh = MagicMock(side_effect=LotteryError("temporary"))
        with self.assertRaisesRegex(LotteryError, "移动端 token 签发失败"):
            self.client.refresh_access_token(force_reload=True)
        self.assertEqual(self.client.access_token_refresh.call_count, 3)
        self.assertEqual(self.holder["value"], "")

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

    def test_read_only_mode_blocks_mutations_before_network(self):
        with patch.dict(os.environ, {"TOKEN_CHECK_ONLY": "true"}):
            for path in (TURN_PATH, BLIND_BOX_PATH, "/api/integral/member/day/activity/exchangeLotteryChance"):
                with self.assertRaisesRegex(LotteryError, "只读验证禁止"):
                    self.client.request("POST", path, {})
        self.client.api_session.request.assert_not_called()

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
    def test_mobile_login_uses_request_token_and_rejects_identity_mismatch(self):
        for identity in ("A001", "OTHER"):
            with self.subTest(identity=identity):
                page = MagicMock()
                page.url = "https://m.abc.com/"
                events = {}
                page.on.side_effect = lambda event, callback: events.update({event: callback})
                page.evaluate.return_value = {}
                inputs = [MagicMock() for _ in range(5)]

                def submitted(**kwargs):
                    # A passport token and placeholder token must never become the mobile token.
                    events["request"](MagicMock(url="https://passport.abc.com/api/login", headers={"x-abc-accesstoken": "wrong-token"}))
                    events["request"](MagicMock(url="https://m.abc.com/api/login/login-by-code", headers={"x-abc-accesstoken": "NONE"}))
                    response = MagicMock(url="https://m.abc.com/api/login/login-by-code")
                    response.json.return_value = {"success": True, "data": {
                        "customerCode": identity, "accessToken": "mobile-token",
                    }}
                    events["response"](response)

                inputs[4].click.side_effect = submitted
                with patch("lottery.ROOT_DOMAIN", "abc.com"), \
                     patch("lottery.access_token_header_name", return_value="x-abc-accesstoken"), \
                     patch("lottery.goto_with_retry"), \
                     patch("lottery.visible_locator", side_effect=inputs), \
                     patch("lottery.detect_password_error", return_value=False), \
                     patch("lottery._slider_is_visible", return_value=False):
                    if identity == "OTHER":
                        with self.assertRaisesRegex(LotteryError, "客编不一致"):
                            login_mobile(page, "A001", "dummy-password")
                    else:
                        self.assertEqual(login_mobile(page, "A001", "dummy-password"), "mobile-token")
                self.assertEqual(page.remove_listener.call_count, 2)

    def test_form_uses_mobile_login_and_extracts_home_storage_token(self):
        page = MagicMock()
        page.url = "https://m.abc.com/"
        page.evaluate.return_value = {"accessToken": "mobile-token"}
        inputs = [MagicMock() for _ in range(5)]
        with patch("lottery.ROOT_DOMAIN", "abc.com"), \
             patch("lottery.PASSPORT_ORIGIN", "https://passport.abc.com"), \
             patch("lottery.goto_with_retry") as goto, \
             patch("lottery.visible_locator", side_effect=inputs), \
             patch("lottery.detect_password_error", return_value=False), \
             patch("lottery._slider_is_visible", return_value=False):
            token = login_mobile(page, "A001", "dummy-password")
        self.assertEqual(token, "mobile-token")
        self.assertIn("/mobile/login?redirect=https%3A%2F%2Fm.abc.com%2F", goto.call_args.args[1])
        inputs[0].fill.assert_called_once_with("A001")
        inputs[2].click.assert_called_once()
        inputs[3].fill.assert_called_once_with("dummy-password")
        inputs[4].click.assert_called_once()
        self.assertEqual(page.remove_listener.call_count, 2)

    def test_mobile_draw_is_once_without_count_exchange_or_blind_box(self):
        from lottery import result_template
        for succeeded in (True, False):
            with self.subTest(succeeded=succeeded), patch.dict(os.environ, {"TOKEN_CHECK_ONLY": "false"}):
                client = MagicMock()
                client.draw_once.return_value = (succeeded, [PrizeRecord(1, "5金豆")], "无可用机会")
                result = result_template("A001", 1)
                execute_mobile_draw(client, result)
                self.assertEqual(client.mock_calls, [unittest.mock.call.draw_once(1)])
                self.assertEqual(result["success"], succeeded)
                self.assertEqual(result["draw_count"], 1 if succeeded else 0)
                self.assertIsNone(result["initial_chances"])
                self.assertIsNone(result["remaining_chances"])
                self.assertIsNone(result["final_chance_check"])
                self.assertTrue(result["chance_check_skipped"])
                self.assertEqual(result["prizes"][0]["title"] if succeeded else result["prizes"], "5金豆" if succeeded else [])

    def test_timeout_is_not_replayed_by_mobile_flow(self):
        client = MagicMock()
        client.draw_once.side_effect = requests.Timeout()
        with patch.dict(os.environ, {"TOKEN_CHECK_ONLY": "false"}), self.assertRaises(requests.Timeout):
            execute_mobile_draw(client, {})
        client.draw_once.assert_called_once_with(1)
        client.lucky_count.assert_not_called()

    def test_mobile_account_bypasses_all_pc_sso_and_activity_steps(self):
        for read_only in (False, True):
            with self.subTest(read_only=read_only), patch.dict(os.environ, {
                "LOTTERY_MODE": "3d", "TOKEN_CHECK_ONLY": str(read_only),
            }), patch("lottery.require_brand_token"), patch("lottery.sync_playwright") as playwright, \
                 patch("lottery.login_mobile", return_value="mobile-token") as login, \
                 patch("lottery.login_passport") as passport, patch("lottery.inject_pc_sso") as inject, \
                 patch("lottery.establish_pc_sso") as pc_sso, \
                 patch("lottery.CampaignClient") as factory:
                client = factory.return_value
                client.activity_code = "LAJ3T"
                client.lucky_count.return_value = 0
                client.draw_once.return_value = (True, [PrizeRecord(1, "5金豆")], "")
                result = execute_account("A001", "dummy-password", 1)
                self.assertTrue(result["success"])
                login.assert_called_once()
                passport.assert_not_called()
                pc_sso.assert_not_called()
                inject.assert_not_called()
                client.open_and_verify.assert_not_called()
                client.exchange_once.assert_not_called()
                client.request.assert_not_called()
                if read_only:
                    client.lucky_count.assert_called_once()
                    client.draw_once.assert_not_called()
                else:
                    client.lucky_count.assert_not_called()
                    client.draw_once.assert_called_once_with(1)
                    self.assertEqual(result["draw_count"], 1)
                    self.assertEqual(result["status"], "completed")
                context_args = playwright.return_value.__enter__.return_value.chromium.launch.return_value.new_context.call_args.kwargs
                self.assertTrue(context_args["is_mobile"])
                self.assertTrue(context_args["has_touch"])
                self.assertEqual(factory.call_args.args[3], {"value": "mobile-token"})
                self.assertTrue(result["completed_at"])


if __name__ == "__main__":
    unittest.main()

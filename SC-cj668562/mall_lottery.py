#!/usr/bin/env python3
"""Mall campaign lottery automation with an isolated SSO session."""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote, urlparse

from dotenv import load_dotenv
from playwright.sync_api import BrowserContext, Page, sync_playwright


ROOT = Path(__file__).resolve().parent
REPOSITORY_ROOT = ROOT.parent
load_dotenv(ROOT / ".env")
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from lottery import (  # noqa: E402
    ACCOUNT_SELECTORS,
    AGREEMENT_SELECTORS,
    PASSWORD_SELECTORS,
    PROD_CAS_COOKIE,
    SUBMIT_SELECTORS,
    LotteryError,
    click_account_login_tab,
    current_login_cookies,
    detect_password_error,
    env_bool,
    env_float,
    env_int,
    goto_with_retry,
    mask_account,
    random_delay,
    require_brand_token,
    response_message,
    runtime_timeouts,
    safe_int,
    solve_slider,
    visible_locator,
)


BRAND_TOKEN = os.getenv("BRAND_TOKEN", "").strip()
PASSPORT_ORIGIN = f"https://passport.{BRAND_TOKEN.lower()}.com" if BRAND_TOKEN else ""
PASSPORT_LOGIN_URL = f"{PASSPORT_ORIGIN}/login" if PASSPORT_ORIGIN else ""
MALL_DOMAIN = f"{''.join(chr(code) for code in (115, 122, 108, 99, 115, 99))}.com"
MALL_HOME_URL = f"https://www.{MALL_DOMAIN}/"
MALL_ACTIVITY_ORIGIN = f"https://activity.{MALL_DOMAIN}"
CAMPAIGN_PATH = "/landing/6A138460B456690F57EAC056D1CBCD16.html"
CAMPAIGN_URL = f"{MALL_ACTIVITY_ORIGIN}{CAMPAIGN_PATH}"
MALL_SESSION_COOKIE = "PRO_NEW_SID"
USER_INFO_PATH = "/cas/user/info"
USER_STATE_PATH = "/lottery/activity/user/state"
DRAW_PATH = "/lottery/activity/seckill/draw/v2"
DEFAULT_ACTIVITY_UUID = "E7DF97221FFF9802C44D657F2AD2E36A"
UTC_PLUS_8 = timezone(timedelta(hours=8))
DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0.0.0 Safari/537.36"
)


def utc8_now() -> datetime:
    return datetime.now(UTC_PLUS_8)


def log(message: str) -> None:
    print(f"[{utc8_now().strftime('%H:%M:%S')}] {message}", flush=True)


def api_succeeded(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    code_ok = safe_int(payload.get("code"), -1) in {0, 200}
    if "ok" in payload:
        return code_ok and payload.get("ok") is True
    if "success" in payload:
        return code_ok and payload.get("success") is True
    return code_ok


def activity_uuid() -> str:
    value = os.getenv("MALL_ACTIVITY_UUID", DEFAULT_ACTIVITY_UUID).strip().upper()
    if len(value) != 32 or any(character not in "0123456789ABCDEF" for character in value):
        raise LotteryError("MALL_ACTIVITY_UUID 蹇呴』鏄?32 浣嶅崄鍏繘鍒跺瓧绗︿覆")
    return value


def parse_available_chances(payload: Any) -> int:
    if not api_succeeded(payload):
        raise LotteryError(f"鑾峰彇鍟嗗煄鎶藉娆℃暟澶辫触: {response_message(payload)}")
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        raise LotteryError("鍟嗗煄鎶藉娆℃暟鍝嶅簲缂哄皯 result")
    return max(0, safe_int(result.get("drawNum"), 0))


def _text(node: dict[str, Any], *keys: str) -> str:
    for key in keys:
        value = node.get(key)
        if isinstance(value, (str, int, float)) and str(value).strip():
            return str(value).strip()
    return ""


def _date_part(value: str) -> str:
    text = str(value or "").strip()
    return text.split(" ", 1)[0] if text else ""


@dataclass
class MallPrizeRecord:
    draw_index: int
    title: str
    prize_type: str = ""
    prize_id: str = ""
    valid_begin: str = ""
    valid_end: str = ""
    expiry_date: str = ""
    bind_required: bool = False
    won_at: str = field(default_factory=lambda: utc8_now().strftime("%Y-%m-%d %H:%M:%S"))


def parse_draw_prize(payload: Any, draw_index: int) -> MallPrizeRecord:
    if not api_succeeded(payload):
        raise LotteryError(f"鍟嗗煄鎶藉澶辫触: {response_message(payload)}")
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        raise LotteryError("鍟嗗煄鎶藉鎴愬姛鍝嶅簲缂哄皯 result")
    valid_begin = _text(result, "prizeValidBeginTime", "validBeginTime")
    valid_end = _text(result, "prizeValidEndTime", "validEndTime")
    return MallPrizeRecord(
        draw_index=draw_index,
        title=_text(result, "prizeName", "awardName", "title", "name") or "鎶藉鎴愬姛锛堝鍝佸悕涓虹┖锛?,
        prize_type=_text(result, "prizeType", "awardType", "type"),
        prize_id=_text(result, "lotteryActivityPrizeId", "prizeId", "awardId"),
        valid_begin=valid_begin,
        valid_end=valid_end,
        expiry_date=_date_part(valid_end),
        bind_required=bool(result.get("isBindWechat")),
    )


def _find_customer_code(node: Any) -> str:
    if isinstance(node, dict):
        for key in ("customerCode", "customerNo", "customer_code", "userCode"):
            value = node.get(key)
            if isinstance(value, (str, int)) and str(value).strip():
                return str(value).strip().upper()
        for value in node.values():
            found = _find_customer_code(value)
            if found:
                return found
    elif isinstance(node, list):
        for value in node:
            found = _find_customer_code(value)
            if found:
                return found
    return ""


def _fill_login_form(page: Page, account: str, password: str) -> None:
    timeouts = runtime_timeouts()
    form_attempts = max(1, env_int("LOGIN_FORM_LOAD_RETRIES", 3))
    account_input = None
    password_input = None
    for attempt in range(1, form_attempts + 1):
        goto_with_retry(
            page,
            f"{PASSPORT_LOGIN_URL}?redirect={quote(MALL_HOME_URL, safe='')}",
            f"鎵撳紑鍟嗗煄 SSO 鐧诲綍椤碉紙琛ㄥ崟灏濊瘯 {attempt}/{form_attempts}锛?,
        )
        if click_account_login_tab(page, timeouts["component"]):
            account_input = visible_locator(
                page, ACCOUNT_SELECTORS, timeouts["component"], "璐﹀彿杈撳叆妗?
            )
            password_input = visible_locator(
                page, PASSWORD_SELECTORS, timeouts["component"], "瀵嗙爜杈撳叆妗?
            )
        if account_input is not None and password_input is not None:
            break
    if account_input is None or password_input is None:
        raise LotteryError("鍟嗗煄 SSO 鐧诲綍琛ㄥ崟鏈畬鏁村姞杞?)

    account_input.fill(account)
    random_delay(env_float("FORM_DELAY_MIN", 0.8), env_float("FORM_DELAY_MAX", 1.8))
    password_input.fill(password)
    random_delay(env_float("FORM_DELAY_MIN", 0.8), env_float("FORM_DELAY_MAX", 1.8))

    agreement = visible_locator(
        page, AGREEMENT_SELECTORS, min(10_000, timeouts["component"]), "鍚屾剰鍗忚鎺т欢"
    )
    if agreement is not None:
        try:
            if agreement.get_attribute("type") == "checkbox":
                if not agreement.is_checked():
                    agreement.check(force=True)
            else:
                agreement.click(timeout=3_000)
        except Exception:
            pass

    submit = visible_locator(page, SUBMIT_SELECTORS, timeouts["component"], "鐧诲綍鎸夐挳")
    if submit is None:
        raise LotteryError("鍟嗗煄 SSO 鐧诲綍鎸夐挳鏈姞杞?)
    submit.click(timeout=min(30_000, timeouts["component"]))
    log("宸叉彁浜ゅ晢鍩?SSO 鐧诲綍琛ㄥ崟")


def login_mall_sso(
    page: Page, context: BrowserContext, account: str, password: str
) -> dict[str, str]:
    require_brand_token()
    _fill_login_form(page, account, password)
    hosted = env_bool("GITHUB_ACTIONS", False)
    timeout_seconds = max(60, env_int("LOGIN_TIMEOUT_SECONDS", 300 if hosted else 180))
    deadline = time.time() + timeout_seconds
    next_log = time.time() + 15
    slider_attempts = 0
    slider_rounds = 0
    slider_round_limit = max(1, env_int("SLIDER_MAX_ROUNDS", 3))

    while time.time() < deadline:
        if detect_password_error(page):
            raise LotteryError("璐﹀彿鎴栧瘑鐮侀敊璇?)
        cookies = current_login_cookies(context)
        mall_cookies = {
            item["name"]: item["value"]
            for item in context.cookies(MALL_HOME_URL)
            if item.get("value")
        }
        if mall_cookies.get(MALL_SESSION_COOKIE):
            log("鍟嗗煄 SSO 鐧诲綍鎴愬姛锛屽凡鍙栧緱浼氳瘽 Cookie")
            return mall_cookies

        slider = page.locator("#aliyunCaptcha-sliding-slider")
        try:
            slider_visible = slider.is_visible(timeout=700)
        except Exception:
            slider_visible = False
        if slider_visible:
            slider_rounds += 1
            if slider_rounds > slider_round_limit:
                raise LotteryError(
                    f"鍟嗗煄 SSO 婊戝潡閲嶅鍑虹幇锛屽凡澶勭悊 {slider_rounds - 1} 杞紱"
                    "璇锋鏌ヨ处鍙峰瘑鐮佹垨鍦ㄩ潪鑷姩鍖栨祻瑙堝櫒涓畬鎴愪竴娆＄櫥褰?
                )
            slider_attempts += 1
            solve_slider(page, max_attempts=max(1, env_int("SLIDER_MAX_ATTEMPTS", 8)))
            next_log = time.time() + 15
            continue

        if time.time() >= next_log:
            current_host = urlparse(page.url).hostname or ""
            stage = "鍟嗗煄鍥炶皟" if current_host.endswith(MALL_DOMAIN) else "缁熶竴鐧诲綍"
            remaining = max(0, int(deadline - time.time()))
            log(f"绛夊緟{stage}鍐欏叆浼氳瘽锛屾渶澶氬啀绛夊緟 {remaining}s")
            next_log = time.time() + 15
        time.sleep(max(0.5, env_float("LOGIN_POLL_INTERVAL_SECONDS", 1.0)))

    cookies = current_login_cookies(context)
    base_ready = bool(cookies.get("tgc") and cookies.get(PROD_CAS_COOKIE))
    detail = "缁熶竴鐧诲綍宸插畬鎴愶紝浣嗗晢鍩庡洖璋冩湭鍐欏叆浼氳瘽" if base_ready else "缁熶竴鐧诲綍鏈畬鎴?
    raise LotteryError(
        f"绛夊緟鍟嗗煄 SSO 鐧诲綍瓒呮椂锛坽timeout_seconds}s锛寋detail}锛屾粦鍧楀鐞?{slider_attempts} 娆★級"
    )


class MallClientProtocol(Protocol):
    def available_chances(self) -> int: ...

    def draw_once(self, draw_index: int) -> MallPrizeRecord: ...


@dataclass
class MallCycleResult:
    prizes: list[MallPrizeRecord]
    draw_count: int
    remaining_chances: int
    message: str = ""


def run_draw_cycle(client: MallClientProtocol, initial_chances: int) -> MallCycleResult:
    remaining = max(0, initial_chances)
    prizes: list[MallPrizeRecord] = []
    draw_count = 0
    draw_cap = max(1, env_int("MALL_DRAW_SAFETY_CAP", 50))
    message = ""

    while remaining > 0:
        if draw_count >= draw_cap:
            message = f"瑙﹀彂鍟嗗煄鎶藉瀹夊叏涓婇檺 {draw_cap}锛屼粛鍓╀綑 {remaining} 娆?
            break
        draw_index = draw_count + 1
        random_delay(
            env_float("MALL_DRAW_DELAY_MIN", 4.0),
            env_float("MALL_DRAW_DELAY_MAX", 7.0),
            f"绗?{draw_index} 娆″晢鍩庢娊濂栧墠缂撳啿",
        )
        try:
            prizes.append(client.draw_once(draw_index))
            draw_count += 1
        except Exception as exc:
            message = str(exc)[:1000]
            break
        random_delay(
            env_float("MALL_STATE_DELAY_MIN", 1.5),
            env_float("MALL_STATE_DELAY_MAX", 3.0),
            "绛夊緟鍟嗗煄鎶藉娆℃暟鏇存柊",
        )
        try:
            remaining = client.available_chances()
        except Exception as exc:
            message = str(exc)[:1000]
            break

    return MallCycleResult(prizes, draw_count, remaining, message)


class MallCampaignClient:
    def __init__(self, page: Page, uuid: str) -> None:
        self.page = page
        self.uuid = uuid

    def _get(self, path: str, query: dict[str, str] | None = None) -> dict[str, Any]:
        response = self.page.evaluate(
            """async ({path, query}) => {
                const url = new URL(path, window.location.origin);
                for (const [key, value] of Object.entries(query || {})) {
                    url.searchParams.set(key, value);
                }
                const result = await fetch(url.toString(), {
                    method: "GET",
                    credentials: "include",
                    cache: "no-store",
                    headers: {"Accept": "application/json", "X-Requested-With": "XMLHttpRequest"},
                });
                return {status: result.status, text: await result.text()};
            }""",
            {"path": path, "query": query or {}},
        )
        status = safe_int(response.get("status"), 0) if isinstance(response, dict) else 0
        text = str(response.get("text") or "") if isinstance(response, dict) else ""
        if status < 200 or status >= 300:
            raise LotteryError(f"鍟嗗煄鎺ュ彛 HTTP 澶辫触: GET {path} HTTP {status}")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LotteryError(f"鍟嗗煄鎺ュ彛鏈繑鍥?JSON: GET {path}") from exc
        if not isinstance(payload, dict):
            raise LotteryError(f"鍟嗗煄鎺ュ彛鍝嶅簲绫诲瀷閿欒: GET {path}")
        return payload

    def open_and_verify(self, expected_account: str) -> str:
        goto_with_retry(self.page, CAMPAIGN_URL, "鎵撳紑鍟嗗煄娲诲姩椤?)
        info = self._get(USER_INFO_PATH)
        if not api_succeeded(info):
            raise LotteryError(f"鍟嗗煄娲诲姩椤电櫥褰曟牎楠屽け璐? {response_message(info)}")
        customer_code = _find_customer_code(info)
        if customer_code and customer_code != expected_account.strip().upper():
            raise LotteryError(
                f"鍟嗗煄鐧诲綍瀹㈢紪涓嶄竴鑷? 鏈熸湜 {mask_account(expected_account)}锛?
                f"瀹為檯 {mask_account(customer_code)}"
            )
        verified = customer_code or expected_account.strip().upper()
        log(f"鍟嗗煄娲诲姩椤?SSO 鏍￠獙鎴愬姛: {mask_account(verified)}")
        return verified

    def available_chances(self) -> int:
        return parse_available_chances(self._get(USER_STATE_PATH, {"uuid": self.uuid}))

    def draw_once(self, draw_index: int) -> MallPrizeRecord:
        prize = parse_draw_prize(self._get(DRAW_PATH, {"uuid": self.uuid}), draw_index)
        log(f"绗?{draw_index} 娆″晢鍩庢娊濂? {prize.title}")
        return prize


def result_template(account: str, account_index: int) -> dict[str, Any]:
    return {
        "account_index": account_index,
        "group_name": os.getenv("GROUP_NAME", "").strip(),
        "username": account,
        "masked_username": mask_account(account),
        "customer_code": "",
        "status": "failed",
        "success": False,
        "detail": "",
        "initial_chances": 0,
        "draw_count": 0,
        "remaining_chances": 0,
        "unfinished": False,
        "prizes": [],
        "completed_at": "",
    }


def execute_account(account: str, password: str, account_index: int) -> dict[str, Any]:
    result = result_template(account, account_index)
    uuid = activity_uuid()
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(
                headless=env_bool("HEADLESS", True),
                timeout=max(60_000, env_int("BROWSER_LAUNCH_TIMEOUT_MS", 120_000)),
                args=["--disable-dev-shm-usage", "--no-sandbox"],
            )
            context = browser.new_context(user_agent=DESKTOP_USER_AGENT, locale="zh-CN")
            page = context.new_page()
            try:
                login_mall_sso(page, context, account, password)
                client = MallCampaignClient(page, uuid)
                result["customer_code"] = client.open_and_verify(account)
                initial_chances = client.available_chances()
                result["initial_chances"] = initial_chances
                log(f"鍟嗗煄褰撳墠鍙娊濂?{initial_chances} 娆?)
                cycle = run_draw_cycle(client, initial_chances)
                result["draw_count"] = cycle.draw_count
                result["remaining_chances"] = cycle.remaining_chances
                result["unfinished"] = cycle.remaining_chances > 0
                result["prizes"] = [asdict(prize) for prize in cycle.prizes]
                if cycle.message or cycle.remaining_chances > 0:
                    result["status"] = "partial"
                    result["detail"] = cycle.message or f"浠嶅墿浣?{cycle.remaining_chances} 娆℃湭鎶?
                else:
                    result["status"] = "completed"
                    result["success"] = True
                    result["detail"] = (
                        f"鍟嗗煄鎶藉瀹屾垚锛屽叡鎵ц {cycle.draw_count} 娆?
                        if cycle.draw_count
                        else "鍟嗗煄褰撳墠鏃犲彲鐢ㄦ娊濂栨鏁?
                    )
            finally:
                context.close()
                browser.close()
    except Exception as exc:
        result["detail"] = str(exc)[:1000]
        log(f"璐﹀彿 {mask_account(account)} 鍟嗗煄鎶藉澶辫触: {result['detail']}")
    result["completed_at"] = utc8_now().strftime("%Y-%m-%d %H:%M:%S")
    return result


def write_result(path: Path, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": utc8_now().strftime("%Y-%m-%d %H:%M:%S"),
        "activity_uuid": activity_uuid(),
        "results": [result],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="鍟嗗煄娲诲姩鎶藉")
    parser.add_argument("account", nargs="?", default=os.getenv("ACCOUNT_USERNAME", ""))
    parser.add_argument("password", nargs="?", default=os.getenv("ACCOUNT_PASSWORD", ""))
    parser.add_argument("--index", type=int, default=env_int("ACCOUNT_INDEX", 1))
    parser.add_argument("--output", type=Path, default=Path(os.getenv("RESULT_JSON_PATH", "result.json")))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    account = str(args.account or "").strip()
    password = str(args.password or "")
    if not account or not password:
        print("缂哄皯鍟嗗煄璐﹀彿瀵嗙爜锛岃閫氳繃鍙傛暟鎴栫幆澧冨彉閲忔彁渚?, file=sys.stderr)
        return 2
    result = execute_account(account, password, args.index)
    write_result(args.output, result)
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())


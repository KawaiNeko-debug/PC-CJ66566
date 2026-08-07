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
        raise LotteryError("MALL_ACTIVITY_UUID 必须是 32 位十六进制字符串")
    return value


def parse_available_chances(payload: Any) -> int:
    if not api_succeeded(payload):
        raise LotteryError(f"获取商城抽奖次数失败: {response_message(payload)}")
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        raise LotteryError("商城抽奖次数响应缺少 result")
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
        raise LotteryError(f"商城抽奖失败: {response_message(payload)}")
    result = payload.get("result") if isinstance(payload, dict) else None
    if not isinstance(result, dict):
        raise LotteryError("商城抽奖成功响应缺少 result")
    valid_begin = _text(result, "prizeValidBeginTime", "validBeginTime")
    valid_end = _text(result, "prizeValidEndTime", "validEndTime")
    return MallPrizeRecord(
        draw_index=draw_index,
        title=_text(result, "prizeName", "awardName", "title", "name") or "抽奖成功（奖品名为空）",
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
            f"打开商城 SSO 登录页（表单尝试 {attempt}/{form_attempts}）",
        )
        if click_account_login_tab(page, timeouts["component"]):
            account_input = visible_locator(
                page, ACCOUNT_SELECTORS, timeouts["component"], "账号输入框"
            )
            password_input = visible_locator(
                page, PASSWORD_SELECTORS, timeouts["component"], "密码输入框"
            )
        if account_input is not None and password_input is not None:
            break
    if account_input is None or password_input is None:
        raise LotteryError("商城 SSO 登录表单未完整加载")

    account_input.fill(account)
    random_delay(env_float("FORM_DELAY_MIN", 0.8), env_float("FORM_DELAY_MAX", 1.8))
    password_input.fill(password)
    random_delay(env_float("FORM_DELAY_MIN", 0.8), env_float("FORM_DELAY_MAX", 1.8))

    agreement = visible_locator(
        page, AGREEMENT_SELECTORS, min(10_000, timeouts["component"]), "同意协议控件"
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

    submit = visible_locator(page, SUBMIT_SELECTORS, timeouts["component"], "登录按钮")
    if submit is None:
        raise LotteryError("商城 SSO 登录按钮未加载")
    submit.click(timeout=min(30_000, timeouts["component"]))
    log("已提交商城 SSO 登录表单")


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
            raise LotteryError("账号或密码错误")
        cookies = current_login_cookies(context)
        mall_cookies = {
            item["name"]: item["value"]
            for item in context.cookies(MALL_HOME_URL)
            if item.get("value")
        }
        if mall_cookies.get(MALL_SESSION_COOKIE):
            log("商城 SSO 登录成功，已取得会话 Cookie")
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
                    f"商城 SSO 滑块重复出现，已处理 {slider_rounds - 1} 轮；"
                    "请检查账号密码或在非自动化浏览器中完成一次登录"
                )
            slider_attempts += 1
            solve_slider(page, max_attempts=max(1, env_int("SLIDER_MAX_ATTEMPTS", 8)))
            next_log = time.time() + 15
            continue

        if time.time() >= next_log:
            current_host = urlparse(page.url).hostname or ""
            stage = "商城回调" if current_host.endswith(MALL_DOMAIN) else "统一登录"
            remaining = max(0, int(deadline - time.time()))
            log(f"等待{stage}写入会话，最多再等待 {remaining}s")
            next_log = time.time() + 15
        time.sleep(max(0.5, env_float("LOGIN_POLL_INTERVAL_SECONDS", 1.0)))

    cookies = current_login_cookies(context)
    base_ready = bool(cookies.get("tgc") and cookies.get(PROD_CAS_COOKIE))
    detail = "统一登录已完成，但商城回调未写入会话" if base_ready else "统一登录未完成"
    raise LotteryError(
        f"等待商城 SSO 登录超时（{timeout_seconds}s，{detail}，滑块处理 {slider_attempts} 次）"
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
            message = f"触发商城抽奖安全上限 {draw_cap}，仍剩余 {remaining} 次"
            break
        draw_index = draw_count + 1
        random_delay(
            env_float("MALL_DRAW_DELAY_MIN", 4.0),
            env_float("MALL_DRAW_DELAY_MAX", 7.0),
            f"第 {draw_index} 次商城抽奖前缓冲",
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
            "等待商城抽奖次数更新",
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
            raise LotteryError(f"商城接口 HTTP 失败: GET {path} HTTP {status}")
        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise LotteryError(f"商城接口未返回 JSON: GET {path}") from exc
        if not isinstance(payload, dict):
            raise LotteryError(f"商城接口响应类型错误: GET {path}")
        return payload

    def open_and_verify(self, expected_account: str) -> str:
        goto_with_retry(self.page, CAMPAIGN_URL, "打开商城活动页")
        info = self._get(USER_INFO_PATH)
        if not api_succeeded(info):
            raise LotteryError(f"商城活动页登录校验失败: {response_message(info)}")
        customer_code = _find_customer_code(info)
        if customer_code and customer_code != expected_account.strip().upper():
            raise LotteryError(
                f"商城登录客编不一致: 期望 {mask_account(expected_account)}，"
                f"实际 {mask_account(customer_code)}"
            )
        verified = customer_code or expected_account.strip().upper()
        log(f"商城活动页 SSO 校验成功: {mask_account(verified)}")
        return verified

    def available_chances(self) -> int:
        return parse_available_chances(self._get(USER_STATE_PATH, {"uuid": self.uuid}))

    def draw_once(self, draw_index: int) -> MallPrizeRecord:
        prize = parse_draw_prize(self._get(DRAW_PATH, {"uuid": self.uuid}), draw_index)
        log(f"第 {draw_index} 次商城抽奖: {prize.title}")
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
                log(f"商城当前可抽奖 {initial_chances} 次")
                cycle = run_draw_cycle(client, initial_chances)
                result["draw_count"] = cycle.draw_count
                result["remaining_chances"] = cycle.remaining_chances
                result["unfinished"] = cycle.remaining_chances > 0
                result["prizes"] = [asdict(prize) for prize in cycle.prizes]
                if cycle.message or cycle.remaining_chances > 0:
                    result["status"] = "partial"
                    result["detail"] = cycle.message or f"仍剩余 {cycle.remaining_chances} 次未抽"
                else:
                    result["status"] = "completed"
                    result["success"] = True
                    result["detail"] = (
                        f"商城抽奖完成，共执行 {cycle.draw_count} 次"
                        if cycle.draw_count
                        else "商城当前无可用抽奖次数"
                    )
            finally:
                context.close()
                browser.close()
    except Exception as exc:
        result["detail"] = str(exc)[:1000]
        log(f"账号 {mask_account(account)} 商城抽奖失败: {result['detail']}")
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
    parser = argparse.ArgumentParser(description="商城活动抽奖")
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
        print("缺少商城账号密码，请通过参数或环境变量提供", file=sys.stderr)
        return 2
    result = execute_account(account, password, args.index)
    write_result(args.output, result)
    return 0 if result.get("success") else 1


if __name__ == "__main__":
    raise SystemExit(main())


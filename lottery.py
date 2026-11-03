#!/usr/bin/env python3
"""PC brand-campaign lottery automation."""

from __future__ import annotations

import argparse
import json
import math
import os
import random
import re
import sys
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import requests
from dotenv import load_dotenv
from playwright.sync_api import BrowserContext, Page, TimeoutError as PlaywrightTimeoutError, sync_playwright


ROOT = Path(__file__).resolve().parent
load_dotenv(ROOT / ".env")

BRAND_TOKEN = os.getenv("BRAND_TOKEN", "").strip()
BRAND_UPPER = BRAND_TOKEN.upper()
BRAND_LOWER = BRAND_TOKEN.lower()
ROOT_DOMAIN = f"{BRAND_LOWER}.com" if BRAND_LOWER else ""
PASSPORT_ORIGIN = f"https://passport.{ROOT_DOMAIN}" if ROOT_DOMAIN else ""
WWW_ORIGIN = f"https://www.{ROOT_DOMAIN}" if ROOT_DOMAIN else ""
PASSPORT_LOGIN_URL = f"{PASSPORT_ORIGIN}/login" if PASSPORT_ORIGIN else ""
PASSWORD_LOGIN_PATH = "/api/cas/login/with-password"
CHECK_LOGIN_URL = f"{PASSPORT_ORIGIN}/api/cas/sso/check-login" if PASSPORT_ORIGIN else ""
SECRET_UPDATE_URL = f"{WWW_ORIGIN}/api/portal/v1/secret/update" if WWW_ORIGIN else ""
LOGIN_BY_CODE_URL = f"{WWW_ORIGIN}/login/login-by-code" if WWW_ORIGIN else ""
CAMPAIGN_URL = f"{WWW_ORIGIN}/portal/brand-campaign" if WWW_ORIGIN else ""
PORTAL_APP_ID = f"{BRAND_UPPER}_PORTAL_PC" if BRAND_UPPER else ""
PROD_CAS_COOKIE = f"PROD-{BRAND_UPPER}-CAS-SID" if BRAND_UPPER else ""
GROUP_SESSION_COOKIE = f"{BRAND_UPPER}GROUP_SESSIONID" if BRAND_UPPER else ""
CUSTOMER_CODE_COOKIE = f"{BRAND_UPPER}_CUSTOMER_CODE" if BRAND_UPPER else ""
USER_INFO_PATH = "/api/integral/user/getUserInfo"
VOUCHER_DETAIL_PATH = "/api/integral/member/day/activity/ns/getVoucherLotteryDetail"
EXCHANGE_CHANCE_PATH = "/api/integral/member/day/activity/exchangeLotteryChance"
LUCKY_COUNT_PATH = "/api/cgi/operationService/front/lottery/getLuckyKeyCount"
TURN_PATH = "/api/cgi/operationService/front/lottery/turn"
BLIND_BOX_PATH = "/api/integral/member/day/activity/selectBlindBoxGiftPopup"

DEFAULT_ACTIVITY_ACCESS_ID = "bf69c3403f094a52a787bfae528da7ea"
DEFAULT_ACTIVITY_CODE = "LAJJT"
DESKTOP_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/138.0.0.0 Safari/537.36"
)
ACCOUNT_SELECTORS = (
    'input[placeholder*="手机号"]',
    'input[placeholder*="账号"]',
    'input[placeholder*="客户编号"]',
    'input[name="username"]',
    'input[type="text"]',
)
PASSWORD_SELECTORS = (
    'input[placeholder*="密码"]',
    'input[name="password"]',
    'input[type="password"]',
)
AGREEMENT_SELECTORS = ('label .el-checkbox__inner', 'input[type="checkbox"]')
SUBMIT_SELECTORS = ('#submitLogin', 'button[type="submit"]', 'button.el-button--primary')
PASSWORD_ERROR_RE = re.compile(r"账号或密码不正确|用户名或密码错误|密码错误|登录失败")
PENDING_HINT_RE = re.compile(r"活动还未开始|活动未开始|不在活动时间")
INSUFFICIENT_POINTS_RE = re.compile(r"金豆不足|可用金豆不足|积分不足|余额不足")
ACTIVITY_ACCESS_ID_RE = re.compile(r"activityAccessId\s*:\s*['\"]([A-Fa-f0-9]{32})['\"]")
LOTTERY_ACTIVITY_ID_RE = re.compile(r"lotteryActivityId\s*:\s*['\"]([A-Za-z0-9_-]+)['\"]")
ACTIVITY_NAME_RE = re.compile(r"activityName\s*:\s*['\"]([^'\"]+)['\"]")
UTC_PLUS_8 = timezone(timedelta(hours=8))


class LotteryError(RuntimeError):
    pass


def utc8_now() -> datetime:
    return datetime.now(UTC_PLUS_8)


def require_brand_token() -> None:
    if not re.fullmatch(r"[A-Za-z]{3}", BRAND_TOKEN):
        raise LotteryError("BRAND_TOKEN 必须通过环境变量提供 3 个英文字母")


@dataclass
class SSOData:
    customer_code: str
    prod_cas_sid: str
    group_session_id: str
    secret_key: str


@dataclass
class PrizeRecord:
    draw_index: int
    title: str
    prize_code: str = ""
    win_code: str = ""
    turn_code: str = ""
    won_at: str = field(default_factory=lambda: utc8_now().strftime("%Y-%m-%d %H:%M:%S"))
    expiry_date: str = field(
        default_factory=lambda: (utc8_now() + timedelta(days=7)).strftime("%Y-%m-%d")
    )


@dataclass
class CouponRecord:
    name: str
    amount: str = ""
    business_line: str = ""
    coupon_type: int = 0


@dataclass
class LotteryCycleResult:
    prizes: list[PrizeRecord]
    remaining_chances: int
    exchange_attempted: int
    exchange_succeeded: int
    exchange_message: str = ""
    draw_message: str = ""


def log(message: str) -> None:
    print(f"[{utc8_now().strftime('%H:%M:%S')}] {message}", flush=True)


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "y", "on"}


def env_int(name: str, default: int) -> int:
    try:
        return int(str(os.getenv(name, default)).strip())
    except (TypeError, ValueError):
        return default


def env_float(name: str, default: float) -> float:
    try:
        return float(str(os.getenv(name, default)).strip())
    except (TypeError, ValueError):
        return default


def random_delay(minimum: float, maximum: float, reason: str = "") -> None:
    low = max(0.0, minimum)
    high = max(low, maximum)
    delay = random.uniform(low, high)
    if reason:
        log(f"{reason}，等待 {delay:.1f}s")
    time.sleep(delay)


def safe_int(value: Any, default: int = 0) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def mask_account(account: str) -> str:
    text = str(account or "").strip()
    if len(text) <= 4:
        return "*" * len(text)
    return f"{text[:3]}****{text[-4:]}"


def response_message(payload: Any, default: str = "请求失败") -> str:
    if isinstance(payload, dict):
        for key in ("message", "msg", "errorMessage", "detail", "error"):
            value = payload.get(key)
            if isinstance(value, str) and value.strip():
                return value.strip()
        for value in payload.values():
            nested = response_message(value, "")
            if nested:
                return nested
    return default


def api_succeeded(payload: Any) -> bool:
    if not isinstance(payload, dict):
        return False
    if "success" in payload:
        return payload.get("success") is True and safe_int(payload.get("code"), 200) in {0, 200}
    return safe_int(payload.get("code"), -1) in {0, 200}


def parse_exchange_detail(payload: Any) -> dict[str, int]:
    data = payload.get("data") if isinstance(payload, dict) else None
    data = data if isinstance(data, dict) else {}
    exchange_num = max(0, safe_int(data.get("exchangeNum"), 0))
    exchange_max = max(exchange_num, safe_int(data.get("exchangeMaxNum"), 3))
    return {
        "price": max(0, safe_int(data.get("price"), 5)),
        "status": safe_int(data.get("status"), 0),
        "exchange_num": exchange_num,
        "exchange_max": exchange_max,
        "remaining": max(0, exchange_max - exchange_num),
    }


def parse_lucky_count(payload: Any) -> int:
    data = payload.get("data") if isinstance(payload, dict) else None
    if not isinstance(data, dict):
        return 0
    return max(0, safe_int(data.get("count"), 0))


def parse_activity_config(script_texts: list[str]) -> dict[str, str]:
    config = {"activity_access_id": "", "activity_code": "", "activity_name": ""}
    for text in script_texts:
        if "activityAccessId" not in text or "lotteryActivityId" not in text:
            continue
        access_match = ACTIVITY_ACCESS_ID_RE.search(text)
        code_match = LOTTERY_ACTIVITY_ID_RE.search(text)
        if not access_match or not code_match:
            continue
        name_match = ACTIVITY_NAME_RE.search(text)
        config.update(
            {
                "activity_access_id": access_match.group(1).lower(),
                "activity_code": code_match.group(1),
                "activity_name": name_match.group(1) if name_match else "",
            }
        )
        break
    return config


def brand_header_name(suffix: str) -> str:
    if not BRAND_LOWER:
        raise LotteryError("BRAND_TOKEN 未配置，无法生成活动请求头")
    return f"x-{BRAND_LOWER}-{suffix}"


def access_token_header_name() -> str:
    return os.getenv("HEADER_ACCESS_TOKEN", "").strip() or brand_header_name("accesstoken")


def is_auth_failure(status: int, payload: Any) -> bool:
    if status in {401, 403}:
        return True
    if isinstance(payload, dict) and str(payload.get("code")) in {"401", "403"}:
        return True
    if not isinstance(payload, dict) or payload.get("success") is not False:
        return False
    message = response_message(payload, "")
    return bool(
        re.search(r"token|令牌|登录失效|登录.*过期|请.*登录|未登录|未授权|认证失败|身份验证|鉴权", message, re.I)
    )


def extract_access_token(headers: Any, storage: Any, header_name: str) -> str:
    target = header_name.lower()
    if isinstance(headers, dict):
        token = next(
            (str(value).strip() for name, value in headers.items() if str(name).lower() == target),
            "",
        )
        if token:
            return token
    if isinstance(storage, str):
        try:
            storage = json.loads(storage)
        except (ValueError, TypeError):
            return ""
    if isinstance(storage, dict):
        for name, value in storage.items():
            key = re.sub(r"[^a-z]", "", str(name).lower())
            if key in {"accesstoken", "token", re.sub(r"[^a-z]", "", target)}:
                if isinstance(value, str) and value.strip():
                    # Some pages store a JSON object under the token key.
                    try:
                        parsed = json.loads(value)
                    except ValueError:
                        return value.strip()
                    if isinstance(parsed, str):
                        return parsed.strip()
                    nested = extract_access_token({}, parsed, header_name)
                    if nested:
                        return nested
            nested = extract_access_token({}, value, header_name)
            if nested:
                return nested
    return ""


def is_3d_mode() -> bool:
    return os.getenv("LOTTERY_MODE", "campaign").strip().lower() == "3d"


def _first_text(node: dict[str, Any], keys: tuple[str, ...]) -> str:
    for key in keys:
        value = node.get(key)
        if isinstance(value, (str, int, float)) and str(value).strip():
            return str(value).strip()
    return ""


def parse_turn_prizes(payload: Any, draw_index: int) -> list[PrizeRecord]:
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    if not isinstance(data, dict):
        return []
    rows = data.get("prizeList")
    if not isinstance(rows, list):
        rows = [data]

    records: list[PrizeRecord] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        title = _first_text(
            row,
            ("prizeTitle", "prizeName", "goodsName", "skuTitle", "awardName", "title", "name"),
        )
        if not title:
            continue
        records.append(
            PrizeRecord(
                draw_index=draw_index,
                title=title,
                prize_code=_first_text(row, ("prizeCode", "awardCode")),
                win_code=_first_text(row, ("winCode",)),
                turn_code=_first_text(row, ("turnCode",)) or _first_text(data, ("turnCode",)),
            )
        )
    return records


def parse_blind_box_coupons(payload: Any) -> list[CouponRecord]:
    data = payload.get("data") if isinstance(payload, dict) else None
    rows = data.get("giftConfigList") if isinstance(data, dict) else None
    if not isinstance(rows, list):
        return []
    coupons: list[CouponRecord] = []
    seen: set[tuple[str, str]] = set()
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = str(row.get("couponName") or row.get("giftName") or "").strip()
        if not name:
            continue
        business_line = str(row.get("businessLine") or "").strip()
        key = (name, business_line)
        if key in seen:
            continue
        seen.add(key)
        coupons.append(
            CouponRecord(
                name=name,
                amount=str(row.get("couponAmount") or "").strip(),
                business_line=business_line,
                coupon_type=safe_int(row.get("couponType"), 0),
            )
        )
    return coupons


def is_insufficient_points(message: str) -> bool:
    return bool(INSUFFICIENT_POINTS_RE.search(str(message or "")))


def derive_status(exchange_message: str, draw_message: str, remaining: int) -> tuple[str, bool]:
    messages = [item for item in (exchange_message, draw_message) if item]
    if any(PENDING_HINT_RE.search(item) for item in messages):
        return "pending", False
    blocking_exchange_message = exchange_message and not is_insufficient_points(exchange_message)
    if blocking_exchange_message or draw_message or remaining > 0:
        return "partial", False
    return "completed", True


def run_draw_exchange_cycle(
    client: "CampaignClient", initial_count: int, initial_detail: dict[str, int]
) -> LotteryCycleResult:
    prizes: list[PrizeRecord] = []
    count = max(0, initial_count)
    draw_index = 0
    exchange_attempted = 0
    exchange_succeeded = 0
    exchange_message = ""
    draw_message = ""
    drew_since_exchange = False
    exchange_cap = min(
        initial_detail["remaining"], max(0, env_int("LOTTERY_EXCHANGE_SAFETY_CAP", 3))
    )
    draw_cap = max(1, env_int("LOTTERY_DRAW_SAFETY_CAP", 50))

    while True:
        while count > 0:
            if draw_index >= draw_cap:
                draw_message = f"触发安全上限 {draw_cap}，仍剩余 {count} 次"
                return LotteryCycleResult(
                    prizes,
                    count,
                    exchange_attempted,
                    exchange_succeeded,
                    exchange_message,
                    draw_message,
                )
            draw_index += 1
            succeeded, rows, message = client.draw_once(draw_index)
            if not succeeded:
                draw_message = message
                return LotteryCycleResult(
                    prizes,
                    count,
                    exchange_attempted,
                    exchange_succeeded,
                    exchange_message,
                    draw_message,
                )
            prizes.extend(rows)
            drew_since_exchange = True
            count = client.lucky_count()

        if exchange_succeeded >= exchange_cap:
            break

        if drew_since_exchange:
            random_delay(
                env_float("POST_DRAW_SETTLE_DELAY_MIN", 2.0),
                env_float("POST_DRAW_SETTLE_DELAY_MAX", 4.0),
                "等待抽奖奖励结算",
            )
            drew_since_exchange = False

        detail = client.exchange_detail()
        if detail["remaining"] <= 0:
            break

        exchange_attempted += 1
        succeeded, message = client.exchange_once(exchange_attempted)
        if not succeeded:
            exchange_message = message
            if is_insufficient_points(message):
                log("当前金豆不足，已抽完可用次数，不再强制兑换")
            break

        exchange_succeeded += 1
        exchange_message = ""
        arrival_retries = max(0, env_int("EXCHANGE_ARRIVAL_RETRIES", 3))
        for retry in range(arrival_retries + 1):
            count = client.lucky_count()
            if count > 0:
                break
            if retry < arrival_retries:
                random_delay(
                    env_float("EXCHANGE_ARRIVAL_DELAY_MIN", 2.0),
                    env_float("EXCHANGE_ARRIVAL_DELAY_MAX", 4.0),
                    f"等待兑换次数到账 {retry + 1}/{arrival_retries}",
                )
        if count <= 0:
            draw_message = "兑换成功但抽奖次数未到账"
            break

    return LotteryCycleResult(
        prizes,
        count,
        exchange_attempted,
        exchange_succeeded,
        exchange_message,
        draw_message,
    )


def run_3d_draw_cycle(client: "CampaignClient", initial_count: int) -> LotteryCycleResult:
    if initial_count <= 0:
        return LotteryCycleResult([], 0, 0, 0)
    succeeded, prizes, message = client.draw_once(1)
    return LotteryCycleResult(
        prizes if succeeded else [],
        max(0, initial_count - (1 if succeeded else 0)),
        0,
        0,
        draw_message="" if succeeded else message,
    )


def runtime_timeouts() -> dict[str, int]:
    hosted = env_bool("GITHUB_ACTIONS", False)
    return {
        "navigation": env_int("PAGE_NAVIGATION_TIMEOUT_MS", 180_000 if hosted else 120_000),
        "networkidle": env_int("PAGE_NETWORKIDLE_TIMEOUT_MS", 90_000 if hosted else 60_000),
        "component": env_int("PAGE_COMPONENT_TIMEOUT_MS", 120_000 if hosted else 60_000),
        "slider": env_int("SLIDER_APPEAR_TIMEOUT_MS", 45_000 if hosted else 25_000),
    }


def wait_for_network_idle(page: Page, timeout_ms: int, label: str) -> None:
    try:
        page.wait_for_load_state("networkidle", timeout=timeout_ms)
        log(f"{label}网络请求已稳定")
    except PlaywrightTimeoutError:
        log(f"{label}在 {timeout_ms // 1000}s 内未达到 networkidle，继续等待页面组件")


def goto_with_retry(page: Page, url: str, label: str) -> None:
    timeouts = runtime_timeouts()
    attempts = max(1, env_int("PAGE_LOAD_RETRIES", 3))
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            log(f"{label}，加载尝试 {attempt}/{attempts}")
            page.goto(url, wait_until="domcontentloaded", timeout=timeouts["navigation"])
            wait_for_network_idle(page, timeouts["networkidle"], label)
            random_delay(
                env_float("PAGE_SETTLE_DELAY_MIN", 3.0 if env_bool("GITHUB_ACTIONS") else 1.5),
                env_float("PAGE_SETTLE_DELAY_MAX", 7.0 if env_bool("GITHUB_ACTIONS") else 3.0),
                f"{label}加载后缓冲",
            )
            return
        except Exception as exc:
            last_error = exc
            log(f"{label}加载失败 {attempt}/{attempts}: {str(exc)[:300]}")
            if attempt < attempts:
                random_delay(
                    env_float("PAGE_RETRY_DELAY_MIN", 4.0),
                    env_float("PAGE_RETRY_DELAY_MAX", 8.0),
                    f"{label}重试前缓冲",
                )
    raise LotteryError(f"{label}连续 {attempts} 次加载失败: {last_error}")


def visible_locator(page: Page, selectors: tuple[str, ...], timeout_ms: int = 0, label: str = "组件"):
    deadline = time.time() + max(0, timeout_ms) / 1000
    next_log = time.time() + 15
    while True:
        for selector in selectors:
            locator = page.locator(selector)
            try:
                count = min(locator.count(), 12)
            except Exception:
                count = 0
            for index in range(count):
                candidate = locator.nth(index)
                try:
                    if candidate.is_visible(timeout=500):
                        return candidate
                except Exception:
                    continue
        if time.time() >= deadline:
            return None
        if time.time() >= next_log:
            remaining = max(0, int(deadline - time.time()))
            log(f"等待{label}加载，最多再等待 {remaining}s")
            next_log = time.time() + 15
        time.sleep(0.5)


def click_account_login_tab(page: Page, timeout_ms: int) -> bool:
    deadline = time.time() + max(0, timeout_ms) / 1000
    next_log = time.time() + 15
    while True:
        if visible_locator(page, PASSWORD_SELECTORS) is not None:
            return True
        for text in ("账号登录", "密码登录"):
            for selector in ("button", '[role="tab"]', ".el-tabs__item"):
                locator = page.locator(selector)
                try:
                    count = min(locator.count(), 30)
                except Exception:
                    count = 0
                for index in range(count):
                    candidate = locator.nth(index)
                    try:
                        if candidate.is_visible(timeout=500) and candidate.inner_text().strip() == text:
                            candidate.click(timeout=10_000)
                            log(f"已点击{text}，等待登录表单")
                            return True
                    except Exception:
                        continue
        if time.time() >= deadline:
            return False
        if time.time() >= next_log:
            remaining = max(0, int(deadline - time.time()))
            log(f"登录组件仍在初始化，最多再等待 {remaining}s")
            next_log = time.time() + 15
        time.sleep(0.5)


def _slider_is_visible(slider, timeout_ms: int = 700) -> bool:
    try:
        return slider.is_visible(timeout=max(1, timeout_ms))
    except Exception:
        return False


def _wait_for_slider_result(slider, timeout_ms: int) -> bool:
    deadline = time.time() + max(0, timeout_ms) / 1000
    while time.time() < deadline:
        if not _slider_is_visible(slider):
            return True
        time.sleep(0.25)
    return not _slider_is_visible(slider)


def _drag_slider_with_mouse(page: Page, slider, wrapper) -> bool:
    try:
        slider.scroll_into_view_if_needed(timeout=5_000)
        slider_box = slider.bounding_box(timeout=5_000)
        wrapper_box = wrapper.bounding_box(timeout=5_000)
    except Exception as exc:
        log(f"读取滑块位置失败: {exc}")
        return False
    if not slider_box or not wrapper_box:
        log("滑块位置尚未就绪")
        return False

    start_x = slider_box["x"] + slider_box["width"] / 2
    start_y = slider_box["y"] + slider_box["height"] / 2
    end_x = (
        wrapper_box["x"]
        + wrapper_box["width"]
        - slider_box["width"] / 2
        + random.uniform(10, 16)
    )
    end_y = start_y + random.uniform(-2.5, 2.5)
    distance = end_x - start_x
    if distance <= 20:
        log(f"滑块轨道宽度异常: {distance:.1f}px")
        return False

    control1_x = start_x + distance * random.uniform(0.24, 0.34)
    control2_x = start_x + distance * random.uniform(0.68, 0.82)
    control1_y = start_y + random.uniform(-8, 8)
    control2_y = end_y + random.uniform(-6, 6)
    steps = random.randint(95, 135)

    try:
        page.mouse.move(start_x, start_y)
        time.sleep(random.uniform(0.15, 0.35))
        page.mouse.down()
        for step in range(1, steps + 1):
            progress = step / steps
            inverse = 1 - progress
            x = (
                inverse**3 * start_x
                + 3 * inverse**2 * progress * control1_x
                + 3 * inverse * progress**2 * control2_x
                + progress**3 * end_x
            )
            y = (
                inverse**3 * start_y
                + 3 * inverse**2 * progress * control1_y
                + 3 * inverse * progress**2 * control2_y
                + progress**3 * end_y
            )
            y += math.sin(progress * math.pi * 3) * random.uniform(0.0, 0.45)
            page.mouse.move(x, y)
            time.sleep(random.uniform(0.009, 0.021))
        time.sleep(random.uniform(0.18, 0.32))
        page.mouse.up()
        return True
    except Exception as exc:
        try:
            page.mouse.up()
        except Exception:
            pass
        log(f"滑块拖动异常: {exc}")
        return False


def solve_slider(page: Page, max_attempts: int = 8) -> None:
    slider_selector = "#aliyunCaptcha-sliding-slider"
    wrapper_id = os.getenv("SLIDER_WRAPPER_ID", "aliyunCaptcha-sliding-wrapper")
    slider = page.locator(slider_selector)
    if not _slider_is_visible(slider, 5_000):
        return

    wrapper = page.locator(f'[id="{wrapper_id}"]')
    hosted = env_bool("GITHUB_ACTIONS", False)
    result_timeout_ms = max(
        3_000,
        env_int("SLIDER_RESULT_TIMEOUT_MS", 10_000 if hosted else 8_000),
    )
    final_grace_seconds = max(0.0, env_float("SLIDER_FINAL_GRACE_SECONDS", 15.0))
    retry_delay_min = env_float("SLIDER_RETRY_DELAY_MIN", 3.0)
    retry_delay_max = env_float("SLIDER_RETRY_DELAY_MAX", 6.0)

    for attempt in range(1, max_attempts + 1):
        log(f"检测到滑块，执行第 {attempt}/{max_attempts} 次验证")
        if _drag_slider_with_mouse(page, slider, wrapper) and _wait_for_slider_result(
            slider, result_timeout_ms
        ):
            log(f"滑块第 {attempt} 次验证通过")
            return
        if attempt < max_attempts:
            random_delay(retry_delay_min, retry_delay_max, "等待滑块挑战刷新")

    if final_grace_seconds > 0:
        log(f"最后一次拖动已完成，额外等待 {final_grace_seconds:.0f}s 确认验证结果")
        if _wait_for_slider_result(slider, int(final_grace_seconds * 1000)):
            log("滑块在最终宽限期内验证通过")
            return
    raise LotteryError("滑块验证未通过")


def current_login_cookies(context: BrowserContext) -> dict[str, str]:
    return {item["name"]: item["value"] for item in context.cookies() if item.get("value")}


def detect_password_error(page: Page) -> bool:
    try:
        body_text = page.locator("body").inner_text(timeout=2_000)
        return bool(PASSWORD_ERROR_RE.search(body_text))
    except Exception:
        return False


def login_passport(page: Page, context: BrowserContext, account: str, password: str) -> dict[str, str]:
    timeouts = runtime_timeouts()
    redirect = requests.utils.quote(CAMPAIGN_URL, safe="")
    login_url = f"{PASSPORT_LOGIN_URL}?redirect={redirect}"
    form_attempts = max(1, env_int("LOGIN_FORM_LOAD_RETRIES", 3))
    account_input = None
    password_input = None
    for form_attempt in range(1, form_attempts + 1):
        goto_with_retry(
            page,
            login_url,
            f"打开 Passport 登录页（表单尝试 {form_attempt}/{form_attempts}）",
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
        log(f"登录表单第 {form_attempt}/{form_attempts} 次未完整加载")
        if form_attempt < form_attempts:
            random_delay(
                env_float("PAGE_RETRY_DELAY_MIN", 4.0),
                env_float("PAGE_RETRY_DELAY_MAX", 8.0),
                "重新加载登录表单前缓冲",
            )
    if account_input is None or password_input is None:
        raise LotteryError(
            f"登录表单连续 {form_attempts} 次未加载完成，"
            f"每次组件等待 {timeouts['component'] // 1000}s"
        )
    account_input.fill(account)
    random_delay(
        env_float("FORM_DELAY_MIN", 0.8),
        env_float("FORM_DELAY_MAX", 1.8),
        "账号填写完成",
    )
    password_input.fill(password)
    random_delay(
        env_float("FORM_DELAY_MIN", 0.8),
        env_float("FORM_DELAY_MAX", 1.8),
        "密码填写完成",
    )

    agreement = visible_locator(page, AGREEMENT_SELECTORS, min(10_000, timeouts["component"]), "同意协议控件")
    if agreement is not None:
        try:
            if agreement.get_attribute("type") == "checkbox":
                if not agreement.is_checked():
                    agreement.check(force=True)
            else:
                agreement.click(timeout=3000)
        except Exception:
            pass

    submit = visible_locator(page, SUBMIT_SELECTORS, timeouts["component"], "登录按钮")
    if submit is None:
        raise LotteryError(f"等待登录按钮超时（{timeouts['component'] // 1000}s）")
    submit.click(timeout=min(30_000, timeouts["component"]))
    log("已提交登录表单，等待登录结果或滑块")

    hosted = env_bool("GITHUB_ACTIONS", False)
    login_timeout = max(60, env_int("LOGIN_TIMEOUT_SECONDS", 240 if hosted else 180))
    deadline = time.time() + login_timeout
    slider_deadline = time.time() + max(5, timeouts["slider"] / 1000)
    slider_attempts = 0
    next_log = time.time() + 15
    poll_interval = max(0.5, env_float("LOGIN_POLL_INTERVAL_SECONDS", 1.0))
    while time.time() < deadline:
        if detect_password_error(page):
            raise LotteryError("账号或密码错误")
        cookies = current_login_cookies(context)
        if cookies.get("tgc") and cookies.get(PROD_CAS_COOKIE):
            log("Passport 登录成功，已取得 SSO Cookie")
            return cookies

        slider = page.locator("#aliyunCaptcha-sliding-slider")
        try:
            slider_visible = slider.is_visible(timeout=700)
        except Exception:
            slider_visible = False
        if slider_visible:
            slider_attempts += 1
            solve_slider(page, max_attempts=max(1, env_int("SLIDER_MAX_ATTEMPTS", 8)))
            next_log = time.time() + 15
            continue

        if time.time() >= next_log:
            remaining = max(0, int(deadline - time.time()))
            gate = "，滑块仍可能加载" if time.time() < slider_deadline else ""
            log(f"登录尚未完成{gate}，最多再等待 {remaining}s")
            next_log = time.time() + 15
        time.sleep(poll_interval)
    raise LotteryError(
        f"等待 Passport 登录完成超时（{login_timeout}s，滑块处理 {slider_attempts} 次）"
    )


def _browser_headers(origin: str, referer: str) -> dict[str, str]:
    return {
        "Accept": "application/json, text/plain, */*",
        "Origin": origin,
        "Referer": referer,
        "User-Agent": DESKTOP_USER_AGENT,
    }


def establish_pc_sso(cookies: dict[str, str], account: str) -> SSOData:
    tgc = cookies.get("tgc", "")
    prod_sid = cookies.get(PROD_CAS_COOKIE, "")
    if not tgc or not prod_sid:
        raise LotteryError("Passport Cookie 不完整")

    session = requests.Session()
    timeout = max(
        15,
        env_int("HTTP_TIMEOUT_SECONDS", 90 if env_bool("GITHUB_ACTIONS", False) else 45),
    )
    passport_headers = _browser_headers(WWW_ORIGIN, f"{WWW_ORIGIN}/")
    response = session.post(
        CHECK_LOGIN_URL,
        json={"appId": PORTAL_APP_ID},
        headers=passport_headers,
        cookies={"tgc": tgc, PROD_CAS_COOKIE: prod_sid},
        timeout=timeout,
    )
    response.raise_for_status()
    check_payload = response.json()
    data = check_payload.get("data") if isinstance(check_payload, dict) else None
    if not api_succeeded(check_payload) or not isinstance(data, dict) or not data.get("isLogin"):
        raise LotteryError(f"SSO check-login 失败: {response_message(check_payload)}")
    auth_code = str(data.get("code") or "").strip()
    customer_code = str(data.get("customerCode") or "").strip().upper()
    if not auth_code or not customer_code:
        raise LotteryError("SSO check-login 未返回 code/customerCode")
    if customer_code != account.strip().upper():
        raise LotteryError(f"登录客编不一致: 期望 {account.strip().upper()}，实际 {customer_code}")

    www_headers = _browser_headers(WWW_ORIGIN, f"{WWW_ORIGIN}/")
    www_headers["Content-Type"] = "application/json"
    response = session.post(
        SECRET_UPDATE_URL,
        headers=www_headers,
        cookies={PROD_CAS_COOKIE: prod_sid},
        timeout=timeout,
    )
    response.raise_for_status()
    secret_payload = response.json()
    secret_data = secret_payload.get("data") if isinstance(secret_payload, dict) else None
    secret_key = str(secret_data.get("keyId") or "").strip() if isinstance(secret_data, dict) else ""
    group_session_id = next(
        (cookie.value for cookie in response.cookies if cookie.name == GROUP_SESSION_COOKIE and cookie.value),
        "",
    )
    if not api_succeeded(secret_payload) or not secret_key or not group_session_id:
        raise LotteryError(f"SSO secret/update 失败: {response_message(secret_payload)}")

    login_headers = _browser_headers(WWW_ORIGIN, f"{WWW_ORIGIN}/")
    login_headers.update(
        {
            "Content-Type": "application/x-www-form-urlencoded",
            "secretkey": secret_key,
            "Cookie": "; ".join(
                (
                    f"{GROUP_SESSION_COOKIE}={group_session_id}",
                    f"{PROD_CAS_COOKIE}={prod_sid}",
                    f"customerCode={customer_code}",
                    f"{CUSTOMER_CODE_COOKIE}={customer_code}",
                )
            ),
        }
    )
    response = session.post(
        LOGIN_BY_CODE_URL,
        data={"code": auth_code},
        headers=login_headers,
        timeout=timeout,
    )
    response.raise_for_status()
    login_payload = response.json()
    if not api_succeeded(login_payload):
        raise LotteryError(f"SSO login-by-code 失败: {response_message(login_payload)}")

    log("PC SSO 会话建立成功")
    return SSOData(customer_code, prod_sid, group_session_id, secret_key)


def inject_pc_sso(context: BrowserContext, sso: SSOData) -> None:
    context.add_cookies(
        [
            {"name": PROD_CAS_COOKIE, "value": sso.prod_cas_sid, "domain": f".{ROOT_DOMAIN}", "path": "/"},
            {"name": GROUP_SESSION_COOKIE, "value": sso.group_session_id, "domain": f"www.{ROOT_DOMAIN}", "path": "/"},
            {"name": "customerCode", "value": sso.customer_code, "domain": f"www.{ROOT_DOMAIN}", "path": "/"},
            {"name": CUSTOMER_CODE_COOKIE, "value": sso.customer_code, "domain": f"www.{ROOT_DOMAIN}", "path": "/"},
        ]
    )


class CampaignClient:
    def __init__(
        self,
        page: Page,
        sso: SSOData,
        secret_holder: dict[str, str],
        access_token_holder: dict[str, str] | None = None,
        browser_cookies: list[dict[str, Any]] | None = None,
    ) -> None:
        self.page = page
        self.sso = sso
        self.secret_holder = secret_holder
        access_override = os.getenv("ACTIVITY_ACCESS_ID", "").strip()
        code_override = os.getenv("LOTTERY_ACTIVITY_CODE", "").strip()
        self.activity_access_id_overridden = bool(access_override)
        self.activity_code_overridden = bool(code_override)
        self.activity_access_id = access_override or DEFAULT_ACTIVITY_ACCESS_ID
        self.activity_code = code_override or DEFAULT_ACTIVITY_CODE
        self.activity_name = ""

        self.is_3d = is_3d_mode()
        if self.is_3d and not os.getenv("LOTTERY_ACTIVITY_CODE", "").strip():
            self.activity_code = "LAJ3T"
        self.access_token_holder = access_token_holder if access_token_holder is not None else {}
        self.api_session = requests.Session()
        for cookie in browser_cookies or []:
            if cookie.get("name") and cookie.get("value"):
                self.api_session.cookies.set(
                    str(cookie["name"]), str(cookie["value"]), domain=cookie.get("domain")
                )
        self.api_origin = f"https://m.{ROOT_DOMAIN}" if self.is_3d else WWW_ORIGIN

    def _storage_values(self) -> dict[str, str]:
        return self.page.evaluate(
            """() => {
                const values = {};
                for (const storage of [localStorage, sessionStorage]) {
                    for (let index = 0; index < storage.length; index += 1) {
                        const key = storage.key(index);
                        try { values[key] = storage.getItem(key); } catch (_) {}
                    }
                }
                return values;
            }"""
        )

    def refresh_access_token(self, force_reload: bool = False) -> str:
        header_name = access_token_header_name()
        attempts = max(1, env_int("ACCESS_TOKEN_RETRIES", 5))
        delay_seconds = max(1, env_int("ACCESS_TOKEN_RETRY_DELAY_SECONDS", 5))
        rejected_token = self.access_token_holder.get("value", "") if force_reload else ""
        if force_reload:
            self.access_token_holder["value"] = ""
            try:
                self.page.reload(wait_until="domcontentloaded", timeout=runtime_timeouts()["navigation"])
                wait_for_network_idle(self.page, runtime_timeouts()["networkidle"], "刷新活动页面")
            except PlaywrightTimeoutError:
                pass
        for attempt in range(attempts):
            token = self.access_token_holder.get("value", "").strip()
            if token and token != rejected_token:
                return token
            if not token or token == rejected_token:
                try:
                    token = extract_access_token({}, self._storage_values(), header_name)
                except Exception:
                    token = ""
                if not token or token == rejected_token:
                    token = self.access_token_holder.get("value", "").strip()
                if token and token != rejected_token:
                    self.access_token_holder["value"] = token
                    return token
            if attempt + 1 < attempts:
                log(f"等待活动会话令牌 {attempt + 1}/{attempts - 1}")
                self.page.wait_for_timeout(delay_seconds * 1000)
                try:
                    self.page.reload(wait_until="domcontentloaded", timeout=runtime_timeouts()["navigation"])
                    wait_for_network_idle(self.page, runtime_timeouts()["networkidle"], "刷新活动页面")
                except Exception:
                    pass
        return ""

    def _request_3d(self, method: str, path: str, payload: dict[str, Any] | None) -> dict[str, Any]:
        token = self.refresh_access_token()
        if not token:
            raise LotteryError("未能从 SSO 页面会话获取活动访问令牌")
        url = f"{self.api_origin}{path}"
        timeout = max(15, env_int("HTTP_TIMEOUT_SECONDS", 90))
        headers = {
            "accept": "application/json, text/plain, */*",
            "content-type": "application/json",
            "origin": self.api_origin,
            "referer": os.getenv(
                "LOTTERY_3D_REFERER",
                "https://servicewechat.com/wx14221813f53bee5d/29/page-frame.html",
            ),
            "user-agent": DESKTOP_USER_AGENT,
            "secretkey": self.secret_holder.get("value") or self.sso.secret_key,
            access_token_header_name(): token,
            brand_header_name("clienttype"): "MP-WEIXIN",
            brand_header_name("mp-version"): os.getenv("LOTTERY_3D_MP_VERSION", "1.2.5"),
            brand_header_name("mp-appid"): os.getenv("LOTTERY_3D_MP_APPID", "wx14221813f53bee5d"),
            brand_header_name("mp-env"): os.getenv("LOTTERY_3D_MP_ENV", "release"),
            "xweb_xhr": "1",
        }
        for attempt in range(2):
            response = self.api_session.request(
                method.upper(),
                url,
                json=payload if method.upper() != "GET" else None,
                headers=headers,
                timeout=timeout,
                allow_redirects=False,
            )
            try:
                data = response.json()
            except ValueError:
                data = None
            if is_auth_failure(response.status_code, data):
                if attempt == 0:
                    log("活动接口拒绝当前会话，刷新令牌后重试")
                    token = self.refresh_access_token(force_reload=True)
                    if not token:
                        break
                    headers[access_token_header_name()] = token
                    headers["secretkey"] = self.secret_holder.get("value") or self.sso.secret_key
                    continue
                raise LotteryError("活动接口鉴权失败，刷新会话后仍未通过")
            if not response.ok:
                raise LotteryError(f"活动接口 HTTP 失败: {method} {path} HTTP {response.status_code}")
            if not isinstance(data, dict):
                raise LotteryError(f"活动接口未返回 JSON: {method} {path}")
            return data
        raise LotteryError("活动接口鉴权失败：未能刷新活动访问令牌")

    def refresh_activity_config(self) -> None:
        script_texts = self.page.locator("script:not([src])").all_text_contents()
        config = parse_activity_config(script_texts)
        if config["activity_access_id"] and not self.activity_access_id_overridden:
            self.activity_access_id = config["activity_access_id"]
        if config["activity_code"] and not self.activity_code_overridden:
            self.activity_code = config["activity_code"]
        self.activity_name = config["activity_name"]
        source = "页面配置" if config["activity_access_id"] and config["activity_code"] else "默认配置"
        label = f"（{self.activity_name}）" if self.activity_name else ""
        log(f"活动配置{label}: {self.activity_code} / {self.activity_access_id}，来源={source}")

    def open_and_verify(self) -> dict[str, Any]:
        goto_with_retry(self.page, CAMPAIGN_URL, "打开品牌活动页")
        if not self.is_3d:
            self.refresh_activity_config()
        payload = self._request_page("GET", USER_INFO_PATH)
        body = payload.get("body") if isinstance(payload, dict) else None
        code = str(body.get("customerCode") or "").strip().upper() if isinstance(body, dict) else ""
        if code != self.sso.customer_code:
            raise LotteryError(f"活动页 SSO 校验失败: 期望 {self.sso.customer_code}，实际 {code or '未登录'}")
        log(f"活动页 SSO 校验成功: {mask_account(code)}")
        return payload

    def request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if self.is_3d:
            return self._request_3d(method, path, payload)
        return self._request_page(method, path, payload)

    def _request_page(
        self, method: str, path: str, payload: dict[str, Any] | None = None
    ) -> dict[str, Any]:
        result = self.page.evaluate(
            """async ({method, path, payload, secretKey}) => {
                const headers = {
                    accept: 'application/json, text/plain, */*',
                    secretkey: secretKey,
                    'cache-control': 'no-cache',
                };
                const options = {method, credentials: 'include', headers, cache: 'no-store'};
                if (method !== 'GET') {
                    headers['content-type'] = 'application/json';
                    options.body = JSON.stringify(payload || {});
                }
                const response = await fetch(path, options);
                const text = await response.text();
                let data = null;
                try { data = JSON.parse(text); } catch (_) {}
                return {status: response.status, ok: response.ok, data, text: text.slice(0, 1200)};
            }""",
            {
                "method": method.upper(),
                "path": path,
                "payload": payload or {},
                "secretKey": self.secret_holder.get("value") or self.sso.secret_key,
            },
        )
        if not isinstance(result, dict) or not result.get("ok"):
            status = safe_int(result.get("status"), 0) if isinstance(result, dict) else 0
            raise LotteryError(f"活动接口 HTTP 失败: {method} {path} HTTP {status}")
        data = result.get("data")
        if not isinstance(data, dict):
            raise LotteryError(f"活动接口未返回 JSON: {method} {path}")
        return data

    def lucky_count(self) -> int:
        payload = self.request("POST", LUCKY_COUNT_PATH, {"activityCode": self.activity_code})
        if not api_succeeded(payload):
            raise LotteryError(f"获取抽奖次数失败: {response_message(payload)}")
        count = parse_lucky_count(payload)
        log(f"当前抽奖次数: {count}")
        return count

    def exchange_detail(self) -> dict[str, int]:
        payload = self.request(
            "POST", VOUCHER_DETAIL_PATH, {"activityAccessId": self.activity_access_id}
        )
        if not api_succeeded(payload):
            raise LotteryError(f"获取兑换状态失败: {response_message(payload)}")
        detail = parse_exchange_detail(payload)
        log(
            f"金豆兑换状态: {detail['exchange_num']}/{detail['exchange_max']}，"
            f"单次 {detail['price']} 金豆，status={detail['status']}"
        )
        return detail

    def exchange_once(self, attempt: int) -> tuple[bool, str]:
        random_delay(
            env_float("EXCHANGE_DELAY_MIN", 2.0),
            env_float("EXCHANGE_DELAY_MAX", 4.0),
            f"第 {attempt} 次兑换前缓冲",
        )
        payload = self.request(
            "POST", EXCHANGE_CHANCE_PATH, {"activityAccessId": self.activity_access_id}
        )
        if not api_succeeded(payload):
            message = response_message(payload)
            log(f"第 {attempt} 次兑换未完成: {message}")
            return False, message
        log(f"第 {attempt} 次兑换成功")
        return True, ""

    def draw_once(self, draw_index: int) -> tuple[bool, list[PrizeRecord], str]:
        random_delay(
            env_float("DRAW_DELAY_MIN", 5.0),
            env_float("DRAW_DELAY_MAX", 8.0),
            f"第 {draw_index} 次抽奖前缓冲",
        )
        payload = self.request(
            "POST",
            TURN_PATH,
            {
                "clientType": "MP-WEIXIN" if self.is_3d else "WEB",
                "activityCode": self.activity_code,
                **({} if self.is_3d else {"luckyKey": ""}),
            },
        )
        if not api_succeeded(payload):
            message = response_message(payload)
            log(f"第 {draw_index} 次抽奖未完成: {message}")
            return False, [], message
        prizes = parse_turn_prizes(payload, draw_index)
        if prizes:
            log(f"第 {draw_index} 次抽奖: {'、'.join(item.title for item in prizes)}")
        else:
            prizes = [PrizeRecord(draw_index=draw_index, title="已中奖（奖品字段待活动开启后确认）")]
            log(f"第 {draw_index} 次抽奖成功，但响应中未识别到奖品字段")
        return True, prizes, ""


def result_template(account: str, account_index: int) -> dict[str, Any]:
    return {
        "account_index": account_index,
        "group_name": os.getenv("GROUP_NAME", "").strip(),
        "username": account,
        "masked_username": mask_account(account),
        "customer_code": "",
        "activity_name": "",
        "activity_code": "",
        "activity_access_id": "",
        "status": "failed",
        "success": False,
        "detail": "",
        "initial_points": 0,
        "final_points": 0,
        "initial_chances": 0,
        "exchange_used_before": 0,
        "exchange_max": 3,
        "exchange_attempted": 0,
        "exchange_succeeded": 0,
        "chances_before_draw": 0,
        "draw_count": 0,
        "remaining_chances": 0,
        "final_chance_check": 0,
        "unfinished": False,
        "prizes": [],
        "blind_box_coupons": [],
        "blind_box_can_popup": False,
        "blind_box_detail": "",
        "started_at": utc8_now().strftime("%Y-%m-%d %H:%M:%S"),
        "completed_at": "",
    }


def execute_account(account: str, password: str, account_index: int) -> dict[str, Any]:
    require_brand_token()
    result = result_template(account, account_index)
    headless = env_bool("HEADLESS", True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=headless,
            timeout=max(60_000, env_int("BROWSER_LAUNCH_TIMEOUT_MS", 120_000)),
            args=["--disable-blink-features=AutomationControlled", "--disable-dev-shm-usage", "--no-sandbox"],
        )
        context = browser.new_context(
            user_agent=DESKTOP_USER_AGENT,
            viewport={"width": 1365, "height": 900},
            locale="zh-CN",
            timezone_id="Asia/Shanghai",
        )
        context.add_init_script(
            "Object.defineProperty(navigator, 'webdriver', {get: () => undefined});"
        )
        page = context.new_page()
        timeouts = runtime_timeouts()
        page.set_default_timeout(timeouts["component"])
        page.set_default_navigation_timeout(timeouts["navigation"])
        try:
            cookies = login_passport(page, context, account, password)
            sso = establish_pc_sso(cookies, account)
            inject_pc_sso(context, sso)
            secret_holder = {"value": sso.secret_key}
            access_token_holder = {"value": ""}

            def capture_runtime_secret(request) -> None:
                try:
                    headers = request.headers
                    if request.url.startswith(f"{WWW_ORIGIN}/"):
                        current = str(headers.get("secretkey") or "").strip()
                        if current:
                            secret_holder["value"] = current
                    hostname = urlsplit(request.url).hostname or ""
                    if ROOT_DOMAIN and (
                        hostname == ROOT_DOMAIN or hostname.endswith(f".{ROOT_DOMAIN}")
                    ):
                        current_token = extract_access_token(
                            headers, {}, access_token_header_name()
                        )
                        if current_token:
                            access_token_holder["value"] = current_token
                except Exception:
                    return

            page.on("request", capture_runtime_secret)
            blind_box_holder: dict[str, Any] = {"payload": None}

            def capture_blind_box(response) -> None:
                if not response.url.split("?", 1)[0].endswith(BLIND_BOX_PATH):
                    return
                try:
                    payload = response.json()
                except Exception:
                    return
                if isinstance(payload, dict):
                    blind_box_holder["payload"] = payload

            if not is_3d_mode():
                page.on("response", capture_blind_box)
            client = CampaignClient(
                page,
                sso,
                secret_holder,
                access_token_holder,
                context.cookies(),
            )
            user_info = client.open_and_verify()
            user_body = user_info.get("body") if isinstance(user_info, dict) else {}
            user_body = user_body if isinstance(user_body, dict) else {}
            result["customer_code"] = sso.customer_code
            result["activity_name"] = client.activity_name
            result["activity_code"] = client.activity_code
            result["activity_access_id"] = client.activity_access_id
            result["initial_points"] = safe_int(user_body.get("canUseVoucher"), 0)

            if not client.is_3d:
                try:
                    blind_payload = blind_box_holder.get("payload")
                    if not isinstance(blind_payload, dict):
                        blind_payload = client.request(
                            "POST",
                            BLIND_BOX_PATH,
                            {"activityAccessId": client.activity_access_id},
                        )
                    if not api_succeeded(blind_payload):
                        raise LotteryError(
                            f"盲盒礼包查询失败: {response_message(blind_payload)}"
                        )
                    blind_data = blind_payload.get("data")
                    blind_data = blind_data if isinstance(blind_data, dict) else {}
                    coupons = parse_blind_box_coupons(blind_payload)
                    result["blind_box_coupons"] = [asdict(item) for item in coupons]
                    result["blind_box_can_popup"] = bool(blind_data.get("canPopup"))
                    result["blind_box_detail"] = (
                        f"获取 {len(coupons)} 张优惠券"
                        if coupons
                        else "未返回优惠券列表（可能已领取）"
                    )
                    if coupons:
                        log(f"盲盒礼包: {'、'.join(item.name for item in coupons)}")
                    else:
                        log(result["blind_box_detail"])
                except Exception as exc:
                    result["blind_box_detail"] = re.sub(r"[\r\n]+", " ", str(exc))[:500]
                    log(f"盲盒礼包记录失败，继续抽奖: {result['blind_box_detail']}")
            else:
                result["blind_box_detail"] = "3D 模式不查询普通活动盲盒"
                result["exchange_max"] = 0
                result["final_points"] = result["initial_points"]

            initial_count = client.lucky_count()
            result["initial_chances"] = initial_count
            if client.is_3d:
                cycle = run_3d_draw_cycle(client, initial_count)
            else:
                detail = client.exchange_detail()
                result.update(
                    {
                        "exchange_used_before": detail["exchange_num"],
                        "exchange_max": detail["exchange_max"],
                        "exchange_attempted": 0,
                    }
                )
                cycle = run_draw_exchange_cycle(client, initial_count, detail)
            result["exchange_attempted"] = cycle.exchange_attempted
            result["exchange_succeeded"] = cycle.exchange_succeeded
            result["chances_before_draw"] = initial_count + cycle.exchange_succeeded
            result["prizes"] = [asdict(item) for item in cycle.prizes]
            result["draw_count"] = max(
                (item.draw_index for item in cycle.prizes), default=0
            )
            result["remaining_chances"] = cycle.remaining_chances
            result["unfinished"] = cycle.remaining_chances > 0
            result["final_chance_check"] = None
            try:
                final_remaining = client.lucky_count()
            except Exception as exc:
                raise LotteryError(f"最终抽奖次数校验失败: {exc}") from exc
            if final_remaining > 0:
                final_message = f"最终校验仍剩余 {final_remaining} 次未抽完"
                cycle.draw_message = "；".join(
                    item for item in (cycle.draw_message, final_message) if item
                )
                log(final_message)
            else:
                log("最终校验通过: 剩余抽奖次数为 0")

            result["remaining_chances"] = final_remaining
            result["final_chance_check"] = final_remaining
            result["unfinished"] = final_remaining > 0

            if not client.is_3d:
                final_info = client.request("GET", USER_INFO_PATH)
                final_body = final_info.get("body") if isinstance(final_info, dict) else {}
                result["final_points"] = safe_int(
                    final_body.get("canUseVoucher") if isinstance(final_body, dict) else None,
                    result["initial_points"],
                )
            messages = [
                item for item in (cycle.exchange_message, cycle.draw_message) if item
            ]
            result["status"], result["success"] = derive_status(
                cycle.exchange_message, cycle.draw_message, final_remaining
            )
            result["detail"] = "；".join(messages) or "所有可用抽奖次数已执行完毕"
        except Exception as exc:
            result["detail"] = re.sub(r"[\r\n]+", " ", str(exc))[:1000]
            log(f"账号 {mask_account(account)} 执行失败: {result['detail']}")
        finally:
            result["completed_at"] = utc8_now().strftime("%Y-%m-%d %H:%M:%S")
            context.close()
            browser.close()
    return result


def write_result(path: Path, result: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": utc8_now().strftime("%Y-%m-%d %H:%M:%S"),
        "activity_code": result.get("activity_code")
        or os.getenv("LOTTERY_ACTIVITY_CODE", "").strip()
        or ("LAJ3T" if is_3d_mode() else DEFAULT_ACTIVITY_CODE),
        "activity_access_id": result.get("activity_access_id")
        or os.getenv("ACTIVITY_ACCESS_ID", "").strip()
        or ("" if is_3d_mode() else DEFAULT_ACTIVITY_ACCESS_ID),
        "results": [result],
    }
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    log(f"结果已写入 {path}")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="PC 品牌活动抽奖")
    parser.add_argument("account", nargs="?", default=os.getenv("ACCOUNT_USERNAME", ""))
    parser.add_argument("password", nargs="?", default=os.getenv("ACCOUNT_PASSWORD", ""))
    parser.add_argument("--index", type=int, default=env_int("ACCOUNT_INDEX", 1))
    parser.add_argument("--output", default=os.getenv("RESULT_JSON_PATH", "result.json"))
    return parser.parse_args()


def main() -> int:
    load_dotenv(ROOT / ".env")
    args = parse_args()
    account = str(args.account or "").strip()
    password = str(args.password or "").strip()
    if not account or not password:
        print("缺少账号密码：请传入 account password 或设置 ACCOUNT_USERNAME/ACCOUNT_PASSWORD", file=sys.stderr)
        return 2
    try:
        result = execute_account(account, password, args.index)
    except Exception as exc:
        result = result_template(account, args.index)
        result["detail"] = re.sub(r"[\r\n]+", " ", str(exc))[:1000]
        result["completed_at"] = utc8_now().strftime("%Y-%m-%d %H:%M:%S")
        log(f"账号 {mask_account(account)} 初始化失败: {result['detail']}")
    write_result(Path(args.output).resolve(), result)
    return 0 if result["status"] in {"completed", "pending"} else 1


if __name__ == "__main__":
    raise SystemExit(main())

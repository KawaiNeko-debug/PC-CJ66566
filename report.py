#!/usr/bin/env python3
"""Merge per-account result JSON files and create an XLSX summary."""

from __future__ import annotations

import argparse
import json
import os
import re
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests
from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter


BASE_HEADERS = [
    "序号",
    "账户",
    "组别",
    "抽奖情况",
    "详细原因",
    "抽奖时间",
    "初始金豆",
    "结束金豆",
    "原有次数",
    "兑换次数",
    "实际抽奖",
    "剩余次数",
    "未完成",
    "盲盒优惠券",
]
UTC_PLUS_8 = timezone(timedelta(hours=8))
TIMESTAMPED_XLSX_RE = re.compile(r"-\d{4}-\d{2}-\d{2}_\d{2}-\d{2}$")


def utc8_now() -> datetime:
    return datetime.now(UTC_PLUS_8)


def timestamped_xlsx_path(path: Path, now: datetime | None = None) -> Path:
    if TIMESTAMPED_XLSX_RE.search(path.stem):
        return path
    stamp = (now or utc8_now()).strftime("%Y-%m-%d_%H-%M")
    return path.with_name(f"{path.stem}-{stamp}{path.suffix}")


def load_result_files(root: Path) -> list[dict[str, Any]]:
    attempts: dict[int, list[dict[str, Any]]] = {}
    for path in sorted(root.rglob("*.json")):
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        rows = payload.get("results") if isinstance(payload, dict) else None
        if not isinstance(rows, list):
            continue
        for row in rows:
            if not isinstance(row, dict):
                continue
            index = int(row.get("account_index") or 0)
            if index <= 0:
                continue
            attempts.setdefault(index, []).append(row)
    return [merge_account_attempts(attempts[index]) for index in sorted(attempts)]


def merge_account_attempts(attempts: list[dict[str, Any]]) -> dict[str, Any]:
    ordered = sorted(attempts, key=lambda row: str(row.get("completed_at") or ""))
    latest = dict(ordered[-1])
    earliest = ordered[0]
    latest["initial_points"] = int(earliest.get("initial_points") or 0)
    latest["initial_chances"] = (
        None if earliest.get("chance_check_skipped") else int(earliest.get("initial_chances") or 0)
    )
    latest["exchange_attempted"] = sum(
        int(row.get("exchange_attempted") or 0) for row in ordered
    )
    latest["exchange_succeeded"] = sum(
        int(row.get("exchange_succeeded") or 0) for row in ordered
    )

    prizes: list[dict[str, Any]] = []
    draw_offset = 0
    seen_prizes: set[tuple[str, ...]] = set()
    for row in ordered:
        local_draw_count = int(row.get("draw_count") or 0)
        local_max_index = 0
        for prize in row.get("prizes") or []:
            if not isinstance(prize, dict):
                continue
            local_index = max(1, int(prize.get("draw_index") or 0))
            local_max_index = max(local_max_index, local_index)
            key = (
                str(prize.get("win_code") or ""),
                str(prize.get("prize_code") or ""),
                str(prize.get("won_at") or ""),
                str(prize.get("title") or ""),
            )
            if key in seen_prizes:
                continue
            seen_prizes.add(key)
            merged_prize = dict(prize)
            merged_prize["draw_index"] = draw_offset + local_index
            prizes.append(merged_prize)
        draw_offset += max(local_draw_count, local_max_index)
    latest["prizes"] = prizes
    latest["draw_count"] = draw_offset

    coupons: list[dict[str, Any]] = []
    seen_coupons: set[tuple[str, str]] = set()
    for row in ordered:
        for coupon in row.get("blind_box_coupons") or []:
            if not isinstance(coupon, dict):
                continue
            key = (
                str(coupon.get("name") or coupon.get("couponName") or "").strip(),
                str(coupon.get("business_line") or coupon.get("businessLine") or "").strip(),
            )
            if not key[0] or key in seen_coupons:
                continue
            seen_coupons.add(key)
            coupons.append(dict(coupon))
    latest["blind_box_coupons"] = coupons
    return latest


def prize_text(record: dict[str, Any]) -> str:
    prizes = record.get("prizes")
    if not isinstance(prizes, list):
        return ""
    values: list[str] = []
    for prize in prizes:
        if not isinstance(prize, dict):
            continue
        title = str(prize.get("title") or "").strip()
        if title:
            values.append(f"第{prize.get('draw_index') or '?'}抽 {title}")
    return "；".join(values)


def coupon_text(record: dict[str, Any]) -> str:
    coupons = record.get("blind_box_coupons")
    if not isinstance(coupons, list):
        return ""
    names: list[str] = []
    for coupon in coupons:
        if not isinstance(coupon, dict):
            continue
        name = str(coupon.get("name") or coupon.get("couponName") or "").strip()
        if name and name not in names:
            names.append(name)
    return "；".join(names)


def final_remaining(record: dict[str, Any]) -> int:
    if "final_chance_check" in record:
        return int(record.get("final_chance_check") or 0)
    return int(record.get("remaining_chances") or 0)


def is_unfinished(record: dict[str, Any]) -> bool:
    return (
        bool(record.get("unfinished"))
        or final_remaining(record) > 0
        or not bool(record.get("success"))
        or str(record.get("status") or "") != "completed"
    )


def coupon_assignments(records: list[dict[str, Any]]) -> dict[str, list[str]]:
    assignments: dict[str, list[str]] = {}
    for record in records:
        customer_code = str(
            record.get("customer_code") or record.get("username") or ""
        ).strip()
        if not customer_code:
            continue
        coupons = record.get("blind_box_coupons")
        if not isinstance(coupons, list):
            continue
        for coupon in coupons:
            if not isinstance(coupon, dict):
                continue
            name = str(coupon.get("name") or coupon.get("couponName") or "").strip()
            if not name:
                continue
            accounts = assignments.setdefault(name, [])
            if customer_code not in accounts:
                accounts.append(customer_code)
    return assignments


def safe_sheet_title(name: str, used: set[str]) -> str:
    base = re.sub(r"[\\/*?:\[\]]", "_", str(name or "").strip())[:31] or "未命名优惠券"
    title = base
    suffix = 2
    while title in used:
        marker = f"-{suffix}"
        title = f"{base[: 31 - len(marker)]}{marker}"
        suffix += 1
    used.add(title)
    return title


def summary_payload(records: list[dict[str, Any]]) -> dict[str, Any]:
    coupons = coupon_assignments(records)
    return {
        "generated_at": utc8_now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_accounts": len(records),
        "completed_accounts": sum(1 for row in records if row.get("status") == "completed"),
        "pending_accounts": sum(1 for row in records if row.get("status") == "pending"),
        "failed_accounts": sum(1 for row in records if row.get("status") in {"failed", "partial"}),
        "total_initial_chances": sum(int(row.get("initial_chances") or 0) for row in records),
        "total_exchanged": sum(int(row.get("exchange_succeeded") or 0) for row in records),
        "total_draws": sum(int(row.get("draw_count") or 0) for row in records),
        "unfinished_accounts": sum(1 for row in records if is_unfinished(row)),
        "remaining_accounts": sum(1 for row in records if final_remaining(row) > 0),
        "chance_check_skipped_accounts": sum(1 for row in records if row.get("chance_check_skipped")),
        "coupon_types": len(coupons),
        "coupon_assignments": sum(len(accounts) for accounts in coupons.values()),
        "results": records,
    }


def write_merged_json(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary_payload(records), ensure_ascii=False, indent=2), encoding="utf-8")


def report_account(record: dict[str, Any]) -> str:
    return str(
        record.get("customer_code")
        or record.get("masked_username")
        or record.get("username")
        or ""
    )


def report_group(record: dict[str, Any]) -> str:
    group = str(record.get("group_name") or "").strip()
    index = int(record.get("account_index") or 0)
    matched = re.fullmatch(r"group(\d+)", group, flags=re.IGNORECASE)
    if matched:
        return f"{matched.group(1)}组账号{index}"
    if group.lower() == "test":
        return f"测试账号{index}"
    return f"{group + '账号' if group else '账号'}{index}"


def report_status(record: dict[str, Any]) -> str:
    return {
        "completed": "抽奖成功",
        "pending": "待处理",
        "partial": "未抽完",
        "failed": "执行失败",
    }.get(str(record.get("status") or ""), str(record.get("status") or "未知"))


def first_draw_time(record: dict[str, Any]) -> str:
    def to_minute(value: Any) -> str:
        text = str(value or "").strip()
        if not text:
            return ""
        try:
            return datetime.strptime(text, "%Y-%m-%d %H:%M:%S").strftime("%Y-%m-%d %H:%M")
        except ValueError:
            return text

    prizes = record.get("prizes")
    if isinstance(prizes, list):
        ordered = sorted(
            (item for item in prizes if isinstance(item, dict)),
            key=lambda item: int(item.get("draw_index") or 0),
        )
        for prize in ordered:
            won_at = str(prize.get("won_at") or "").strip()
            if won_at:
                return to_minute(won_at)
    completed_at = str(record.get("completed_at") or "").strip()
    return to_minute(completed_at)


def draw_slots(records: list[dict[str, Any]]) -> int:
    maximum = 0
    for record in records:
        maximum = max(maximum, int(record.get("draw_count") or 0))
        prizes = record.get("prizes")
        if not isinstance(prizes, list):
            continue
        for prize in prizes:
            if isinstance(prize, dict):
                maximum = max(maximum, int(prize.get("draw_index") or 0))
    return max(3, maximum)


def report_headers(slot_count: int) -> list[str]:
    headers = list(BASE_HEADERS)
    for draw_index in range(1, slot_count + 1):
        headers.extend([f"抽奖{draw_index}", "过期时间"])
    return headers


def _excel_date(value: Any) -> Any:
    text = str(value or "").strip()
    try:
        return datetime.strptime(text, "%Y-%m-%d").date()
    except ValueError:
        return text


def row_values(record: dict[str, Any], slot_count: int) -> list[Any]:
    values: list[Any] = [
        int(record.get("account_index") or 0),
        report_account(record),
        report_group(record),
        report_status(record),
        str(record.get("detail") or ""),
        first_draw_time(record),
        int(record.get("initial_points") or 0),
        int(record.get("final_points") or 0),
        "未查询" if record.get("chance_check_skipped") else int(record.get("initial_chances") or 0),
        int(record.get("exchange_succeeded") or 0),
        int(record.get("draw_count") or 0),
        "未查询" if record.get("chance_check_skipped") else final_remaining(record),
        "是" if is_unfinished(record) else "否",
        coupon_text(record),
    ]
    prizes_by_draw: dict[int, list[dict[str, Any]]] = {}
    for prize in record.get("prizes") or []:
        if not isinstance(prize, dict):
            continue
        index = int(prize.get("draw_index") or 0)
        if index > 0:
            prizes_by_draw.setdefault(index, []).append(prize)
    for draw_index in range(1, slot_count + 1):
        prizes = prizes_by_draw.get(draw_index, [])
        titles = [str(item.get("title") or "").strip() for item in prizes]
        titles = [title for title in titles if title]
        expiry = next(
            (str(item.get("expiry_date") or "").strip() for item in prizes if item.get("expiry_date")),
            "",
        )
        values.extend(["；".join(titles), _excel_date(expiry)])
    return values


def write_xlsx(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "抽奖汇总"
    slot_count = draw_slots(records)
    headers = report_headers(slot_count)
    sheet.freeze_panes = "G2"
    sheet.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{max(1, len(records) + 1)}"
    sheet.sheet_view.showGridLines = False
    sheet.sheet_view.zoomScale = 90

    header_fill = PatternFill("solid", fgColor="D9E5F6")
    header_font = Font(name="Microsoft YaHei", color="1F1F1F", bold=True, size=10)
    body_font = Font(name="Microsoft YaHei", color="333333", size=10)
    thin = Side(style="thin", color="D9D9D9")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    status_fills = {
        "pending": PatternFill("solid", fgColor="FFF2CC"),
        "partial": PatternFill("solid", fgColor="FCE4D6"),
        "failed": PatternFill("solid", fgColor="F4CCCC"),
    }
    status_fonts = {
        "completed": Font(name="Microsoft YaHei", color="00A933", size=10),
        "pending": Font(name="Microsoft YaHei", color="9C6500", size=10),
        "partial": Font(name="Microsoft YaHei", color="C65911", bold=True, size=10),
        "failed": Font(name="Microsoft YaHei", color="C00000", bold=True, size=10),
    }

    sheet.append(headers)
    for cell in sheet[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = border

    for record in records:
        sheet.append(row_values(record, slot_count))
        row_number = sheet.max_row
        status = str(record.get("status") or "")
        for cell in sheet[row_number]:
            cell.border = border
            cell.font = body_font
            cell.alignment = Alignment(horizontal="center", vertical="center")
        sheet.cell(row_number, 2).alignment = Alignment(horizontal="left", vertical="center")
        sheet.cell(row_number, 5).alignment = Alignment(horizontal="left", vertical="center")
        sheet.cell(row_number, 14).alignment = Alignment(horizontal="left", vertical="center")
        if status in status_fonts:
            sheet.cell(row_number, 4).font = status_fonts[status]
        if status in status_fills:
            sheet.cell(row_number, 4).fill = status_fills[status]
        if is_unfinished(record):
            sheet.cell(row_number, 12).fill = status_fills["failed"]
            sheet.cell(row_number, 13).fill = status_fills["failed"]
            sheet.cell(row_number, 13).font = status_fonts["failed"]
        for expiry_column in range(len(BASE_HEADERS) + 2, len(headers) + 1, 2):
            expiry_cell = sheet.cell(row_number, expiry_column)
            expiry_cell.font = Font(name="Microsoft YaHei", color="E53935", size=10)
            if isinstance(expiry_cell.value, datetime) or hasattr(expiry_cell.value, "year"):
                expiry_cell.number_format = "yyyy-mm-dd"

    widths = [7, 18, 15, 12, 32, 19, 11, 11, 11, 11, 11, 11, 10, 30]
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    for index in range(len(BASE_HEADERS) + 1, len(headers) + 1, 2):
        sheet.column_dimensions[get_column_letter(index)].width = 18
        sheet.column_dimensions[get_column_letter(index + 1)].width = 14
    sheet.row_dimensions[1].height = 25
    for row_number in range(2, sheet.max_row + 1):
        sheet.row_dimensions[row_number].height = 22

    stats = workbook.create_sheet("统计")
    stats.sheet_view.showGridLines = False
    summary = summary_payload(records)
    stats_rows = [
        ("账号总数", summary["total_accounts"]),
        ("已完成账号", summary["completed_accounts"]),
        ("活动未开始/待处理", summary["pending_accounts"]),
        ("失败或部分完成", summary["failed_accounts"]),
        ("原有抽奖次数合计", "未查询" if summary["chance_check_skipped_accounts"] else summary["total_initial_chances"]),
        ("本次兑换次数合计", summary["total_exchanged"]),
        ("本次实际抽奖合计", summary["total_draws"]),
        ("未完成账号", summary["unfinished_accounts"]),
        ("仍有剩余次数账号", "未查询" if summary["chance_check_skipped_accounts"] else summary["remaining_accounts"]),
        ("盲盒优惠券种类", summary["coupon_types"]),
        ("盲盒优惠券发放记录", summary["coupon_assignments"]),
        ("生成时间", summary["generated_at"]),
    ]
    stats.append(["统计项", "数值"])
    for label, value in stats_rows:
        stats.append([label, value])
    stats.freeze_panes = "A2"
    stats.auto_filter.ref = f"A1:B{stats.max_row}"
    stats.column_dimensions["A"].width = 26
    stats.column_dimensions["B"].width = 24
    for cell in stats[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = border
    stats.row_dimensions[1].height = 25
    for row in stats.iter_rows(min_row=2):
        row[0].font = Font(name="Microsoft YaHei", bold=True, color="333333", size=10)
        row[1].font = body_font
        for cell in row:
            cell.border = border
            cell.alignment = Alignment(vertical="center")

    used_titles = set(workbook.sheetnames)
    for coupon_name, customer_codes in coupon_assignments(records).items():
        coupon_sheet = workbook.create_sheet(safe_sheet_title(coupon_name, used_titles))
        coupon_sheet.sheet_view.showGridLines = False
        coupon_sheet.append(["客编"])
        header = coupon_sheet[1][0]
        header.fill = header_fill
        header.font = header_font
        header.alignment = Alignment(horizontal="center", vertical="center")
        header.border = border
        for customer_code in customer_codes:
            coupon_sheet.append([customer_code])
            cell = coupon_sheet.cell(coupon_sheet.max_row, 1)
            cell.font = body_font
            cell.alignment = Alignment(horizontal="left", vertical="center")
            cell.border = border
        coupon_sheet.freeze_panes = "A2"
        coupon_sheet.auto_filter.ref = f"A1:A{max(1, coupon_sheet.max_row)}"
        coupon_sheet.column_dimensions["A"].width = 24
        coupon_sheet.row_dimensions[1].height = 24

    workbook.save(path)


def build_telegram_summary(records: list[dict[str, Any]]) -> str:
    total = len(records)
    succeeded = sum(
        1
        for row in records
        if bool(row.get("success")) or str(row.get("status") or "") == "completed"
    )
    winners = sum(1 for row in records if isinstance(row.get("prizes"), list) and row["prizes"])
    prize_counts: Counter[str] = Counter()
    unfinished: list[tuple[str, int, str]] = []
    coupons = coupon_assignments(records)

    for row in records:
        for prize in row.get("prizes") or []:
            if not isinstance(prize, dict):
                continue
            title = str(prize.get("title") or "").strip() or "未识别奖品"
            prize_counts[title] += 1
        remaining = final_remaining(row)
        if is_unfinished(row):
            account = str(row.get("masked_username") or row.get("username") or "未知账号")
            unfinished.append((account, remaining, str(row.get("detail") or "").strip()))

    prize_total = sum(prize_counts.values())
    success_rate = (succeeded / total * 100) if total else 0.0
    headline = (
        "喵喵~今天一切正常捏"
        if total > 0 and succeeded == total and not unfinished
        else "喵喵~今天有账号需要关注捏"
    )
    lines = [
        headline,
        "📈 总体统计",
        f"  ├── 总账号数: {total}",
        f"  ├── 抽奖成功: {succeeded}/{total}",
        f"  ├── 有中奖记录账号: {winners}",
        f"  ├── 抽奖结果总数: {prize_total}",
        f"  ├── 盲盒优惠券种类: {len(coupons)}",
        f"  ├── 盲盒优惠券发放: {sum(len(items) for items in coupons.values())}",
        f"  ├── 未完成账号: {len(unfinished)}",
        (
            "  ├── 剩余次数: 未查询（单次抽奖模式）"
            if any(row.get("chance_check_skipped") for row in records)
            else f"  ├── 仍有剩余次数账号: {sum(1 for row in records if final_remaining(row) > 0)}"
        ),
        f"  └── 抽奖成功率: {success_rate:.0f}%",
        "🎁 奖品分布",
    ]
    if prize_counts:
        distribution = sorted(prize_counts.items(), key=lambda item: (-item[1], item[0]))
        for index, (title, count) in enumerate(distribution):
            branch = "└──" if index == len(distribution) - 1 else "├──"
            lines.append(f"  {branch} {title}: {count}")
    else:
        lines.append("  └── 暂无奖品记录")

    lines.append("🎟️ 盲盒优惠券")
    if coupons:
        coupon_rows = list(coupons.items())
        for index, (name, customer_codes) in enumerate(coupon_rows):
            branch = "└──" if index == len(coupon_rows) - 1 else "├──"
            lines.append(f"  {branch} {name}: {len(customer_codes)} 个账号")
    else:
        lines.append("  └── 暂无盲盒优惠券记录")

    if unfinished:
        lines.append("⚠️ 未完成账号")
        for index, (account, remaining, detail) in enumerate(unfinished):
            branch = "└──" if index == len(unfinished) - 1 else "├──"
            reason = f"最终校验剩余 {remaining} 次" if remaining > 0 else (detail or "执行未完成")
            lines.append(f"  {branch} {account}: {reason}")
    return "\n".join(lines)


def telegram_credentials() -> tuple[str, str]:
    token = (
        os.getenv("TELEGRAM_BOT_TOKEN")
        or os.getenv("TG_BOT_TOKEN")
        or os.getenv("TELEGRAM_TOKEN")
        or ""
    ).strip()
    chat_id = (
        os.getenv("TELEGRAM_CHAT_ID")
        or os.getenv("TG_CHAT_ID")
        or os.getenv("TELEGRAM_TO")
        or ""
    ).strip()
    return token, chat_id


def split_telegram_text(text: str, limit: int = 3500) -> list[str]:
    chunks: list[str] = []
    current = ""
    for line in text.splitlines():
        candidate = f"{current}\n{line}" if current else line
        if len(candidate) <= limit:
            current = candidate
            continue
        if current:
            chunks.append(current)
        while len(line) > limit:
            chunks.append(line[:limit])
            line = line[limit:]
        current = line
    if current:
        chunks.append(current)
    return chunks or [""]


def send_telegram_message(text: str) -> bool:
    token, chat_id = telegram_credentials()
    if not token or not chat_id:
        print("[telegram] 跳过文本汇总: 未配置 Bot Token 或 Chat ID", flush=True)
        return False
    ok = True
    for part in split_telegram_text(text):
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{token}/sendMessage",
                json={"chat_id": chat_id, "text": part},
                timeout=30,
            )
            if response.status_code != 200:
                print(
                    f"[telegram] sendMessage 失败: HTTP {response.status_code} {response.text[:500]}",
                    flush=True,
                )
                ok = False
        except requests.RequestException as exc:
            print(f"[telegram] sendMessage 异常: {type(exc).__name__}", flush=True)
            ok = False
    return ok


def send_telegram_document(path: Path) -> bool:
    token, chat_id = telegram_credentials()
    if not token or not chat_id:
        print("[telegram] 跳过 XLSX: 未配置 Bot Token 或 Chat ID", flush=True)
        return False
    if not path.is_file():
        print(f"[telegram] 跳过 XLSX: 文件不存在 {path}", flush=True)
        return False
    try:
        with path.open("rb") as stream:
            response = requests.post(
                f"https://api.telegram.org/bot{token}/sendDocument",
                data={"chat_id": chat_id},
                files={
                    "document": (
                        path.name,
                        stream,
                        "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    )
                },
                timeout=60,
            )
        if response.status_code != 200:
            print(
                f"[telegram] sendDocument 失败: HTTP {response.status_code} {response.text[:500]}",
                flush=True,
            )
            return False
        return True
    except requests.RequestException as exc:
        print(f"[telegram] sendDocument 异常: {type(exc).__name__}", flush=True)
        return False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="合并抽奖 JSON 并生成 XLSX")
    parser.add_argument("results_dir", type=Path)
    parser.add_argument("--merged-json", type=Path, default=Path("merged/result.json"))
    parser.add_argument("--xlsx", type=Path, default=Path("merged/lottery-summary.xlsx"))
    parser.add_argument("--telegram", action="store_true", help="发送 Telegram 文本汇总和 XLSX")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    args.xlsx = timestamped_xlsx_path(args.xlsx)
    records = load_result_files(args.results_dir)
    write_merged_json(args.merged_json, records)
    write_xlsx(args.xlsx, records)
    telegram_sent = False
    if args.telegram:
        message_sent = send_telegram_message(build_telegram_summary(records))
        document_sent = send_telegram_document(args.xlsx)
        telegram_sent = message_sent and document_sent
    print(
        f"汇总完成: accounts={len(records)} json={args.merged_json} xlsx={args.xlsx} "
        f"telegram={'sent' if telegram_sent else 'skipped_or_failed'}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

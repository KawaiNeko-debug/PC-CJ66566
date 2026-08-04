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


HEADERS = [
    "序号",
    "账号",
    "状态",
    "初始金豆",
    "结束金豆",
    "原有抽奖次数",
    "兑换前已用额度",
    "兑换额度上限",
    "本次兑换成功",
    "累计可抽次数",
    "实际抽奖次数",
    "最终校验剩余次数",
    "是否未抽完",
    "盲盒优惠券",
    "奖品",
    "详情",
    "完成时间",
]
UTC_PLUS_8 = timezone(timedelta(hours=8))


def utc8_now() -> datetime:
    return datetime.now(UTC_PLUS_8)


def load_result_files(root: Path) -> list[dict[str, Any]]:
    records: dict[int, dict[str, Any]] = {}
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
            current = records.get(index)
            if current is None or str(row.get("completed_at") or "") >= str(current.get("completed_at") or ""):
                records[index] = row
    return [records[index] for index in sorted(records)]


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
    return bool(record.get("unfinished")) or final_remaining(record) > 0


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
        "coupon_types": len(coupons),
        "coupon_assignments": sum(len(accounts) for accounts in coupons.values()),
        "results": records,
    }


def write_merged_json(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary_payload(records), ensure_ascii=False, indent=2), encoding="utf-8")


def row_values(record: dict[str, Any]) -> list[Any]:
    return [
        int(record.get("account_index") or 0),
        str(record.get("masked_username") or record.get("username") or ""),
        str(record.get("status") or ""),
        int(record.get("initial_points") or 0),
        int(record.get("final_points") or 0),
        int(record.get("initial_chances") or 0),
        int(record.get("exchange_used_before") or 0),
        int(record.get("exchange_max") or 0),
        int(record.get("exchange_succeeded") or 0),
        int(record.get("chances_before_draw") or 0),
        int(record.get("draw_count") or 0),
        final_remaining(record),
        "是" if is_unfinished(record) else "否",
        coupon_text(record),
        prize_text(record),
        str(record.get("detail") or ""),
        str(record.get("completed_at") or ""),
    ]


def write_xlsx(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "抽奖汇总"
    sheet.freeze_panes = "A2"
    sheet.auto_filter.ref = f"A1:{get_column_letter(len(HEADERS))}{max(1, len(records) + 1)}"

    header_fill = PatternFill("solid", fgColor="1F4E78")
    header_font = Font(color="FFFFFF", bold=True)
    thin = Side(style="thin", color="D9E2F3")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    status_fills = {
        "completed": PatternFill("solid", fgColor="C6EFCE"),
        "pending": PatternFill("solid", fgColor="FFEB9C"),
        "partial": PatternFill("solid", fgColor="FCE4D6"),
        "failed": PatternFill("solid", fgColor="FFC7CE"),
    }

    sheet.append(HEADERS)
    for cell in sheet[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = border

    for record in records:
        sheet.append(row_values(record))
        row_number = sheet.max_row
        status = str(record.get("status") or "")
        for cell in sheet[row_number]:
            cell.border = border
            cell.alignment = Alignment(vertical="top", wrap_text=True)
        if status in status_fills:
            sheet.cell(row_number, 3).fill = status_fills[status]
        if is_unfinished(record):
            sheet.cell(row_number, 12).fill = status_fills["failed"]
            sheet.cell(row_number, 13).fill = status_fills["failed"]
            sheet.cell(row_number, 13).font = Font(color="9C0006", bold=True)

    widths = [8, 18, 12, 12, 12, 14, 16, 16, 16, 16, 16, 20, 14, 48, 45, 48, 22]
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    sheet.row_dimensions[1].height = 24

    stats = workbook.create_sheet("统计")
    summary = summary_payload(records)
    stats_rows = [
        ("账号总数", summary["total_accounts"]),
        ("已完成账号", summary["completed_accounts"]),
        ("活动未开始/待处理", summary["pending_accounts"]),
        ("失败或部分完成", summary["failed_accounts"]),
        ("原有抽奖次数合计", summary["total_initial_chances"]),
        ("本次兑换次数合计", summary["total_exchanged"]),
        ("本次实际抽奖合计", summary["total_draws"]),
        ("未抽完账号", summary["unfinished_accounts"]),
        ("盲盒优惠券种类", summary["coupon_types"]),
        ("盲盒优惠券发放记录", summary["coupon_assignments"]),
        ("生成时间", summary["generated_at"]),
    ]
    for label, value in stats_rows:
        stats.append([label, value])
    stats.column_dimensions["A"].width = 25
    stats.column_dimensions["B"].width = 25
    for row in stats.iter_rows():
        row[0].font = Font(bold=True)
        for cell in row:
            cell.border = border

    used_titles = set(workbook.sheetnames)
    for coupon_name, customer_codes in coupon_assignments(records).items():
        coupon_sheet = workbook.create_sheet(safe_sheet_title(coupon_name, used_titles))
        coupon_sheet.append(["客编"])
        header = coupon_sheet[1][0]
        header.fill = header_fill
        header.font = header_font
        header.alignment = Alignment(horizontal="center", vertical="center")
        header.border = border
        for customer_code in customer_codes:
            coupon_sheet.append([customer_code])
            cell = coupon_sheet.cell(coupon_sheet.max_row, 1)
            cell.alignment = Alignment(vertical="center")
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
    unfinished: list[tuple[str, int]] = []
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
            unfinished.append((account, remaining))

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
        f"  ├── 未抽完账号: {len(unfinished)}",
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
        lines.append("⚠️ 未抽完账号")
        for index, (account, remaining) in enumerate(unfinished):
            branch = "└──" if index == len(unfinished) - 1 else "├──"
            lines.append(f"  {branch} {account}: 最终校验剩余 {remaining} 次")
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

#!/usr/bin/env python3
"""Merge mall lottery results and generate the Telegram/XLSX summary."""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter


ROOT = Path(__file__).resolve().parent
REPOSITORY_ROOT = ROOT.parent
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from report import send_telegram_document, send_telegram_message  # noqa: E402


UTC_PLUS_8 = timezone(timedelta(hours=8))
BASE_HEADERS = [
    "序号",
    "账户",
    "组别",
    "抽奖情况",
    "详细原因",
    "初始次数",
    "实际抽奖",
    "剩余次数",
    "未抽完",
    "完成时间",
]


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
            if current is None or str(row.get("completed_at") or "") >= str(
                current.get("completed_at") or ""
            ):
                records[index] = row
    return [records[index] for index in sorted(records)]


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
        return f"商城{matched.group(1)}组账号{index}"
    if group.lower() == "test":
        return f"商城测试账号{index}"
    return f"{group + '账号' if group else '商城账号'}{index}"


def report_status(record: dict[str, Any]) -> str:
    return {
        "completed": "抽奖完成",
        "partial": "未抽完",
        "failed": "执行失败",
    }.get(str(record.get("status") or ""), str(record.get("status") or "未知"))


def is_unfinished(record: dict[str, Any]) -> bool:
    return bool(record.get("unfinished")) or int(record.get("remaining_chances") or 0) > 0


def draw_slots(records: list[dict[str, Any]]) -> int:
    maximum = max((int(row.get("draw_count") or 0) for row in records), default=0)
    for record in records:
        for prize in record.get("prizes") or []:
            if isinstance(prize, dict):
                maximum = max(maximum, int(prize.get("draw_index") or 0))
    return max(1, maximum)


def report_headers(slot_count: int) -> list[str]:
    headers = list(BASE_HEADERS)
    for index in range(1, slot_count + 1):
        headers.extend([f"抽奖{index}", "奖品类型", "有效期", "需绑定"])
    return headers


def row_values(record: dict[str, Any], slot_count: int) -> list[Any]:
    values: list[Any] = [
        int(record.get("account_index") or 0),
        report_account(record),
        report_group(record),
        report_status(record),
        str(record.get("detail") or ""),
        int(record.get("initial_chances") or 0),
        int(record.get("draw_count") or 0),
        int(record.get("remaining_chances") or 0),
        "是" if is_unfinished(record) else "否",
        str(record.get("completed_at") or ""),
    ]
    prizes_by_draw: dict[int, dict[str, Any]] = {}
    for prize in record.get("prizes") or []:
        if isinstance(prize, dict):
            index = int(prize.get("draw_index") or 0)
            if index > 0:
                prizes_by_draw[index] = prize
    for index in range(1, slot_count + 1):
        prize = prizes_by_draw.get(index, {})
        values.extend(
            [
                str(prize.get("title") or ""),
                str(prize.get("prize_type") or ""),
                str(prize.get("valid_end") or prize.get("expiry_date") or ""),
                "是" if prize.get("bind_required") else "否" if prize else "",
            ]
        )
    return values


def summary_payload(records: list[dict[str, Any]]) -> dict[str, Any]:
    prize_counts: Counter[str] = Counter()
    for record in records:
        for prize in record.get("prizes") or []:
            if isinstance(prize, dict):
                title = str(prize.get("title") or "").strip()
                if title:
                    prize_counts[title] += 1
    return {
        "generated_at": utc8_now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_accounts": len(records),
        "completed_accounts": sum(1 for row in records if row.get("status") == "completed"),
        "failed_accounts": sum(1 for row in records if row.get("status") in {"failed", "partial"}),
        "total_initial_chances": sum(int(row.get("initial_chances") or 0) for row in records),
        "total_draws": sum(int(row.get("draw_count") or 0) for row in records),
        "unfinished_accounts": sum(1 for row in records if is_unfinished(row)),
        "prize_counts": dict(prize_counts),
        "results": records,
    }


def write_merged_json(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(summary_payload(records), ensure_ascii=False, indent=2), encoding="utf-8")


def write_xlsx(path: Path, records: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    workbook = Workbook()
    sheet = workbook.active
    sheet.title = "商城抽奖汇总"
    slot_count = draw_slots(records)
    headers = report_headers(slot_count)
    sheet.append(headers)
    sheet.freeze_panes = "F2"
    sheet.auto_filter.ref = f"A1:{get_column_letter(len(headers))}{max(1, len(records) + 1)}"
    sheet.sheet_view.showGridLines = False

    header_fill = PatternFill("solid", fgColor="D9E5F6")
    header_font = Font(name="Microsoft YaHei", color="1F1F1F", bold=True, size=10)
    body_font = Font(name="Microsoft YaHei", color="333333", size=10)
    thin = Side(style="thin", color="D9D9D9")
    border = Border(left=thin, right=thin, top=thin, bottom=thin)
    status_fills = {
        "partial": PatternFill("solid", fgColor="FCE4D6"),
        "failed": PatternFill("solid", fgColor="F4CCCC"),
    }

    for cell in sheet[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.alignment = Alignment(horizontal="center", vertical="center")
        cell.border = border

    for record in records:
        sheet.append(row_values(record, slot_count))
        row_number = sheet.max_row
        for cell in sheet[row_number]:
            cell.font = body_font
            cell.border = border
            cell.alignment = Alignment(horizontal="center", vertical="center")
        sheet.cell(row_number, 2).alignment = Alignment(horizontal="left", vertical="center")
        sheet.cell(row_number, 5).alignment = Alignment(horizontal="left", vertical="center")
        status = str(record.get("status") or "")
        if status in status_fills:
            sheet.cell(row_number, 4).fill = status_fills[status]
        if is_unfinished(record):
            sheet.cell(row_number, 8).fill = status_fills["failed"]
            sheet.cell(row_number, 9).fill = status_fills["failed"]

    widths = [7, 18, 18, 12, 34, 11, 11, 11, 10, 20]
    for index, width in enumerate(widths, start=1):
        sheet.column_dimensions[get_column_letter(index)].width = width
    for index in range(len(BASE_HEADERS) + 1, len(headers) + 1, 4):
        sheet.column_dimensions[get_column_letter(index)].width = 20
        sheet.column_dimensions[get_column_letter(index + 1)].width = 14
        sheet.column_dimensions[get_column_letter(index + 2)].width = 20
        sheet.column_dimensions[get_column_letter(index + 3)].width = 11

    stats = workbook.create_sheet("统计")
    summary = summary_payload(records)
    stats.append(["统计项", "数值"])
    stats_rows = [
        ("账号总数", summary["total_accounts"]),
        ("已完成账号", summary["completed_accounts"]),
        ("失败或部分完成", summary["failed_accounts"]),
        ("初始次数合计", summary["total_initial_chances"]),
        ("实际抽奖合计", summary["total_draws"]),
        ("未抽完账号", summary["unfinished_accounts"]),
        ("生成时间", summary["generated_at"]),
    ]
    for label, value in stats_rows:
        stats.append([label, value])
    for cell in stats[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.border = border
    for row in stats.iter_rows(min_row=2):
        for cell in row:
            cell.font = body_font
            cell.border = border
    stats.column_dimensions["A"].width = 24
    stats.column_dimensions["B"].width = 24

    distribution = workbook.create_sheet("奖品分布")
    distribution.append(["奖品", "数量"])
    for title, count in sorted(
        summary["prize_counts"].items(), key=lambda item: (-item[1], item[0])
    ):
        distribution.append([title, count])
    for cell in distribution[1]:
        cell.fill = header_fill
        cell.font = header_font
        cell.border = border
    for row in distribution.iter_rows(min_row=2):
        for cell in row:
            cell.font = body_font
            cell.border = border
    distribution.column_dimensions["A"].width = 32
    distribution.column_dimensions["B"].width = 12
    workbook.save(path)


def build_telegram_summary(records: list[dict[str, Any]]) -> str:
    summary = summary_payload(records)
    prize_counts = Counter(summary["prize_counts"])
    unfinished = [
        (report_account(row), int(row.get("remaining_chances") or 0))
        for row in records
        if is_unfinished(row)
    ]
    lines = [
        "商城抽奖执行汇总",
        f"账号总数: {summary['total_accounts']}",
        f"完成账号: {summary['completed_accounts']}",
        f"实际抽奖: {summary['total_draws']}",
        f"未抽完账号: {summary['unfinished_accounts']}",
        "奖品分布:",
    ]
    if prize_counts:
        lines.extend(
            f"- {title}: {count}"
            for title, count in sorted(prize_counts.items(), key=lambda item: (-item[1], item[0]))
        )
    else:
        lines.append("- 暂无奖品记录")
    if unfinished:
        lines.append("未抽完明细:")
        lines.extend(f"- {account}: 剩余 {remaining} 次" for account, remaining in unfinished)
    return "\n".join(lines)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="生成商城抽奖 JSON/XLSX 汇总")
    parser.add_argument("results_dir", type=Path)
    parser.add_argument("--merged-json", type=Path, default=Path("merged/mall-result.json"))
    parser.add_argument("--xlsx", type=Path, default=Path("merged/mall-lottery-summary.xlsx"))
    parser.add_argument("--telegram", action="store_true")
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
        f"商城汇总完成: accounts={len(records)} json={args.merged_json} "
        f"xlsx={args.xlsx} telegram={'sent' if telegram_sent else 'skipped_or_failed'}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

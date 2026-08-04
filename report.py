#!/usr/bin/env python3
"""Merge per-account result JSON files and create an XLSX summary."""

from __future__ import annotations

import argparse
import json
from datetime import datetime
from pathlib import Path
from typing import Any

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
    "抽奖前总次数",
    "实际抽奖次数",
    "剩余次数",
    "奖品",
    "详情",
    "完成时间",
]


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


def summary_payload(records: list[dict[str, Any]]) -> dict[str, Any]:
    return {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "total_accounts": len(records),
        "completed_accounts": sum(1 for row in records if row.get("status") == "completed"),
        "pending_accounts": sum(1 for row in records if row.get("status") == "pending"),
        "failed_accounts": sum(1 for row in records if row.get("status") in {"failed", "partial"}),
        "total_initial_chances": sum(int(row.get("initial_chances") or 0) for row in records),
        "total_exchanged": sum(int(row.get("exchange_succeeded") or 0) for row in records),
        "total_draws": sum(int(row.get("draw_count") or 0) for row in records),
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
        int(record.get("remaining_chances") or 0),
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

    widths = [8, 18, 12, 12, 12, 14, 16, 16, 16, 16, 16, 12, 45, 48, 22]
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

    workbook.save(path)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="合并抽奖 JSON 并生成 XLSX")
    parser.add_argument("results_dir", type=Path)
    parser.add_argument("--merged-json", type=Path, default=Path("merged/result.json"))
    parser.add_argument("--xlsx", type=Path, default=Path("merged/lottery-summary.xlsx"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    records = load_result_files(args.results_dir)
    write_merged_json(args.merged_json, records)
    write_xlsx(args.xlsx, records)
    print(
        f"汇总完成: accounts={len(records)} json={args.merged_json} xlsx={args.xlsx}",
        flush=True,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

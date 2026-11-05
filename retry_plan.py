#!/usr/bin/env python3
"""Build a fresh-runner matrix for every account that did not complete."""

from __future__ import annotations

import argparse
import json
import os
import re
from pathlib import Path
from typing import Any


SLIDER_FAILURE = "滑块验证未通过"


def attempt_number(record: dict[str, Any], path: Path) -> int:
    match = re.search(r"-retry(\d+)(?:[/\\]|$)", str(path.parent))
    return max(int(record.get("attempt_number") or 1), int(match[1]) + 1 if match else 1)


def is_slider_failure(record: dict[str, Any]) -> bool:
    detail = str(record.get("detail") or "")
    return not bool(record.get("success")) and SLIDER_FAILURE in detail


def is_retryable_failure(record: dict[str, Any]) -> bool:
    return not bool(record.get("success")) or str(record.get("status") or "") != "completed"


def is_mobile_retryable(record: dict[str, Any]) -> bool:
    # A confirmed draw must not be replayed, including a cleanup/check failure afterwards.
    if record.get("success") or record.get("draw_count") or record.get("prizes"):
        return False
    return is_retryable_failure(record)


def retry_indices(root: Path, expected: list[int] | None = None) -> list[int]:
    attempts: dict[int, list[dict[str, Any]]] = {}
    slider_only = os.getenv("RETRY_SLIDER_ONLY", "").strip().lower() == "true"
    mobile = os.getenv("RETRY_MODE", "").strip().lower() == "3d"
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
            if index > 0:
                row = dict(row, attempt_number=attempt_number(row, path))
                attempts.setdefault(index, []).append(row)
    indices: set[int] = set()
    for index, history in attempts.items():
        if expected is not None and index not in expected:
            continue
        latest = max(history, key=lambda row: (row["attempt_number"], str(row.get("completed_at") or "")))
        if mobile and any(not is_mobile_retryable(row) for row in history):
            continue
        retryable = is_mobile_retryable(latest) if mobile else (
            is_slider_failure(latest) if slider_only else is_retryable_failure(latest)
        )
        if retryable:
            indices.add(index)
    indices.update(set(expected or []) - set(attempts))
    return sorted(indices)


def build_retry_matrix(root: Path, expected: list[int] | None = None) -> dict[str, list[dict[str, int]]]:
    return {"include": [{"index": index} for index in retry_indices(root, expected)]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--github-output", default=os.getenv("GITHUB_OUTPUT", ""))
    args = parser.parse_args()

    expected_text = os.getenv("EXPECTED_MATRIX", "").strip()
    expected = [int(item["index"]) for item in json.loads(expected_text)["include"]] if expected_text else None
    matrix = build_retry_matrix(args.root, expected)
    has_retry = bool(matrix["include"])
    output_lines = [
        "matrix=" + json.dumps(matrix, ensure_ascii=False, separators=(",", ":")),
        f"has_retry={'true' if has_retry else 'false'}",
    ]
    if args.github_output:
        with Path(args.github_output).open("a", encoding="utf-8") as stream:
            stream.write("\n".join(output_lines) + "\n")
    print(f"需在新 Runner 重试 {len(matrix['include'])} 个未成功账号")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

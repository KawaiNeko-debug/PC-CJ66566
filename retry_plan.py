#!/usr/bin/env python3
"""Build a fresh-runner matrix for every account that did not complete."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from typing import Any


SLIDER_FAILURE = "滑块验证未通过"


def is_slider_failure(record: dict[str, Any]) -> bool:
    detail = str(record.get("detail") or "")
    return not bool(record.get("success")) and SLIDER_FAILURE in detail


def is_retryable_failure(record: dict[str, Any]) -> bool:
    return not bool(record.get("success")) or str(record.get("status") or "") != "completed"


def retry_indices(root: Path) -> list[int]:
    indices: set[int] = set()
    slider_only = os.getenv("RETRY_SLIDER_ONLY", "").strip().lower() == "true"
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
            if not (is_slider_failure(row) if slider_only else is_retryable_failure(row)):
                continue
            index = int(row.get("account_index") or 0)
            if index > 0:
                indices.add(index)
    return sorted(indices)


def build_retry_matrix(root: Path) -> dict[str, list[dict[str, int]]]:
    return {"include": [{"index": index} for index in retry_indices(root)]}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("root", type=Path)
    parser.add_argument("--github-output", default=os.getenv("GITHUB_OUTPUT", ""))
    args = parser.parse_args()

    matrix = build_retry_matrix(args.root)
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

#!/usr/bin/env python3
"""Bound an account process and keep its result available for a fresh runner."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def stop_process_tree(process: subprocess.Popen) -> None:
    if os.name == "nt":
        subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                       capture_output=True, check=False)
    else:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    process.wait()


def save_failure(output: Path, index: int, *, timed_out: bool, seconds: float) -> None:
    now = datetime.now(timezone(timedelta(hours=8))).strftime("%Y-%m-%d %H:%M:%S")
    detail = (
        f"账号执行超过 {seconds:g} 秒，已终止进程并转交新 Runner 重试"
        if timed_out else "账号进程结束但未生成有效结果，转交新 Runner 重试"
    )
    if timed_out:
        detail += "；若抽奖请求已发出但响应未返回，服务端结果尚不确定"
    row = {
        "account_index": index, "masked_username": f"账号{index}",
        "group_name": os.getenv("GROUP_NAME", ""),
        "status": "failed", "success": False, "detail": detail,
        "timed_out": timed_out, "retry_required": True,
        "prizes": [], "draw_count": 0, "completed_at": now,
        "initial_chances": None, "remaining_chances": None,
        "final_chance_check": None,
        "chance_check_skipped": os.getenv("LOTTERY_MODE") == "3d",
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    temporary = output.with_suffix(output.suffix + ".tmp")
    temporary.write_text(json.dumps({"generated_at": now, "results": [row]},
                                    ensure_ascii=False, indent=2), encoding="utf-8")
    temporary.replace(output)


def valid_result(output: Path, index: int) -> bool:
    try:
        payload = json.loads(output.read_text(encoding="utf-8"))
        rows = payload.get("results") if isinstance(payload, dict) else None
        return (isinstance(rows, list) and len(rows) == 1
                and isinstance(rows[0], dict) and rows[0].get("account_index") == index
                and rows[0].get("status") in {"completed", "failed", "partial", "pending"})
    except (OSError, ValueError):
        return False


def run_account(index: int, output: Path, seconds: float, script: Path = ROOT / "lottery.py") -> int:
    output = output.resolve()
    output.unlink(missing_ok=True)
    environment = dict(os.environ, LOTTERY_CHECKPOINT_PATH=str(output), PYTHONUNBUFFERED="1")
    process = subprocess.Popen(
        [sys.executable, str(script), "--index", str(index), "--output", str(output)],
        env=environment, start_new_session=os.name != "nt",
    )
    timed_out = False
    try:
        code = process.wait(timeout=seconds)
    except subprocess.TimeoutExpired:
        timed_out = True
        stop_process_tree(process)
        code = 124
    if not valid_result(output, index):
        save_failure(output, index, timed_out=timed_out, seconds=seconds)
        if not timed_out:
            code = code or 1
    elif timed_out:
        # The draw may have finished before browser cleanup stalled. Keep its checkpoint.
        row = json.loads(output.read_text(encoding="utf-8"))["results"][0]
        if row.get("success"):
            print("抽奖结果已保存，仅清理进程超时，保留成功结果", flush=True)
            return 0
        row.update({"timed_out": True, "retry_required": True})
        row["detail"] = f"账号执行超过 {seconds:g} 秒，已结束进程，保留已记录结果并转交新 Runner 重试"
        payload = {"results": [row]}
        temporary = output.with_suffix(output.suffix + ".tmp")
        temporary.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(output)
    if timed_out:
        print(f"账号 {index} 超过 {seconds:g} 秒，结果已保存，等待新 Runner 重试", flush=True)
    return code


def main() -> int:
    parser = argparse.ArgumentParser(description="单账号超时保护")
    parser.add_argument("--index", type=int, required=True)
    parser.add_argument("--output", type=Path, default=Path("result.json"))
    parser.add_argument("--timeout-seconds", type=float, default=300)
    args = parser.parse_args()
    if args.timeout_seconds <= 0:
        parser.error("timeout must be positive")
    return run_account(args.index, args.output, args.timeout_seconds)


if __name__ == "__main__":
    raise SystemExit(main())

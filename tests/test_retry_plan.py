import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from retry_plan import build_retry_matrix, is_retryable_failure, is_slider_failure


class RetryPlanTests(unittest.TestCase):
    def test_later_rounds_only_select_accounts_still_unsuccessful(self):
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"RETRY_MODE": "3d"}):
            root = Path(directory)
            def write(folder, rows):
                path = root / folder / "result.json"
                path.parent.mkdir(parents=True)
                path.write_text(json.dumps({"results": rows}), encoding="utf-8")

            write("result-3d-1", [{"account_index": 1, "success": False}])
            write("result-3d-1-retry1", [{"account_index": 1, "success": True, "draw_count": 1}])
            write("result-3d-2", [{"account_index": 2, "success": False}])
            write("result-3d-2-retry1", [{"account_index": 2, "success": False}])
            write("result-3d-2-retry2", [{"account_index": 2, "success": True, "draw_count": 1}])
            write("result-3d-3", [{"account_index": 3, "success": False}])
            write("result-3d-3-retry1", [{"account_index": 3, "success": False}])
            # Missing retry2 artifact must keep account 3 eligible for retry3.
            (root / "broken.json").write_text("{", encoding="utf-8")
            self.assertEqual(build_retry_matrix(root, [1, 2, 3]), {"include": [{"index": 3}]})
            write("result-3d-3-retry3", [{"account_index": 3, "success": True}])
            self.assertEqual(build_retry_matrix(root, [1, 2, 3]), {"include": []})

    def test_mobile_timeouts_and_missing_results_get_fresh_runners(self):
        rows = [
            {"account_index": 1, "success": False, "detail": "移动端登录超时（300s）"},
            {"account_index": 2, "success": False, "timed_out": True},
            {"account_index": 3, "success": True, "draw_count": 1},
            {"account_index": 4, "success": False, "draw_count": 1, "prizes": [{"title": "5金豆"}]},
            {"account_index": 5, "success": False, "detail": "脚本未生成 result.json"},
        ]
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"RETRY_MODE": "3d"}):
            root = Path(directory)
            (root / "result.json").write_text(json.dumps({"results": rows}), encoding="utf-8")
            self.assertEqual(build_retry_matrix(root, [1, 2, 3, 4, 5, 6]), {
                "include": [{"index": 1}, {"index": 2}, {"index": 5}, {"index": 6}],
            })

    def test_3d_retries_only_slider_failures_without_replaying_draws(self):
        rows = [
            {"account_index": 1, "success": False, "detail": "滑块验证未通过"},
            {"account_index": 2, "success": False, "status": "partial", "draw_count": 1,
             "remaining_chances": 4, "detail": "最终校验仍剩余 4 次未抽完"},
            {"account_index": 3, "success": False, "status": "failed", "detail": "请求超时"},
        ]
        with tempfile.TemporaryDirectory() as directory, patch.dict(os.environ, {"RETRY_SLIDER_ONLY": "true"}):
            root = Path(directory)
            (root / "result.json").write_text(json.dumps({"results": rows}), encoding="utf-8")
            self.assertEqual(build_retry_matrix(root), {"include": [{"index": 1}]})

    def test_all_unsuccessful_accounts_are_retried(self):
        rows = [
            {
                "account_index": 1,
                "success": False,
                "status": "failed",
                "detail": "滑块验证未通过",
            },
            {
                "account_index": 2,
                "success": True,
                "status": "completed",
                "detail": "",
            },
            {
                "account_index": 3,
                "success": False,
                "status": "failed",
                "detail": "账号或密码错误",
            },
            {
                "account_index": 4,
                "success": True,
                "status": "completed",
                "detail": "历史日志: 滑块验证未通过",
            },
        ]
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            (root / "result.json").write_text(
                json.dumps({"results": rows}, ensure_ascii=False), encoding="utf-8"
            )
            self.assertEqual(
                build_retry_matrix(root),
                {"include": [{"index": 1}, {"index": 3}]},
            )

    def test_slider_failure_requires_unsuccessful_result(self):
        self.assertTrue(is_slider_failure({"success": False, "detail": "滑块验证未通过"}))
        self.assertFalse(is_slider_failure({"success": True, "detail": "滑块验证未通过"}))

    def test_retryable_failure_includes_pending_and_partial(self):
        self.assertTrue(is_retryable_failure({"success": False, "status": "pending"}))
        self.assertTrue(is_retryable_failure({"success": False, "status": "partial"}))
        self.assertTrue(is_retryable_failure({"success": False, "status": "failed"}))
        self.assertFalse(is_retryable_failure({"success": True, "status": "completed"}))


if __name__ == "__main__":
    unittest.main()

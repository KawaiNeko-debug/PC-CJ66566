import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from account_runner import run_account
from retry_plan import build_retry_matrix


class AccountDeadlineTests(unittest.TestCase):
    def test_hung_process_is_killed_and_enters_retry_matrix(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "worker.py"
            script.write_text("import time\ntime.sleep(30)\n", encoding="utf-8")
            output = root / "result.json"
            start = time.monotonic()
            code = run_account(7, output, 0.5, script)
            self.assertEqual(code, 124)
            self.assertLess(time.monotonic() - start, 10)
            row = json.loads(output.read_text(encoding="utf-8"))["results"][0]
            self.assertTrue(row["timed_out"])
            self.assertTrue(row["retry_required"])
            self.assertFalse(row["success"])
            with patch.dict(os.environ, {"RETRY_MODE": "3d"}):
                self.assertEqual(build_retry_matrix(root), {"include": [{"index": 7}]})

    def test_successful_checkpoint_survives_hung_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "worker.py"
            script.write_text(
                'import json,os,time\nfrom pathlib import Path\n'
                'Path(os.environ["LOTTERY_CHECKPOINT_PATH"]).write_text(json.dumps({"results": ['
                '{"account_index": 1, "status": "completed", "success": True, '
                '"draw_count": 1, "prizes": [{"title": "reward"}]}]}), encoding="utf-8")\n'
                'time.sleep(30)\n', encoding="utf-8",
            )
            output = root / "result.json"
            self.assertEqual(run_account(1, output, 2, script), 0)
            row = json.loads(output.read_text(encoding="utf-8"))["results"][0]
            self.assertEqual(row["prizes"], [{"title": "reward"}])
            with patch.dict(os.environ, {"RETRY_MODE": "3d"}):
                self.assertEqual(build_retry_matrix(root), {"include": []})

    def test_exit_without_valid_result_discards_stale_success_and_retries(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            script = root / "worker.py"
            script.write_text("pass\n", encoding="utf-8")
            output = root / "result.json"
            output.write_text('{"results":[{"account_index":1,"status":"completed","success":true}]}')
            self.assertEqual(run_account(1, output, 5, script), 1)
            row = json.loads(output.read_text(encoding="utf-8"))["results"][0]
            self.assertFalse(row["success"])
            self.assertTrue(row["retry_required"])


if __name__ == "__main__":
    unittest.main()

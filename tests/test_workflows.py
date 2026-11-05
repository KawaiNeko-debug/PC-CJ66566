import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def workflow_text(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


def account_workflows() -> str:
    return workflow_text("lottery.yml") + workflow_text("lottery-retry.yml")


class WorkflowTests(unittest.TestCase):
    def test_browser_jobs_use_supported_ubuntu_version(self):
        for file, name in (("lottery.yml", "lottery"), ("lottery-retry.yml", "accounts")):
            text = workflow_text(file)
            block = re.search(rf"(?ms)^  {name}:.*?(?=^  [a-z_]+:|\Z)", text)
            self.assertIsNotNone(block)
            self.assertIn("runs-on: ubuntu-24.04", block.group(0))

    def test_split_credentials_are_masked_before_export(self):
        text = account_workflows()
        self.assertEqual(text.count('print(f"::add-mask::{username.strip()}")'), 2)
        self.assertEqual(text.count('print(f"::add-mask::{password.strip()}")'), 2)
        for block in text.split('with open(os.environ["GITHUB_ENV"]')[:-1]:
            self.assertIn('print(f"::add-mask::{password.strip()}")', block)

    def test_mobile_token_probe_is_opt_in_and_skips_lottery_report(self):
        entry = workflow_text("lottery-3d.yml")
        worker = workflow_text("lottery.yml")
        self.assertIn("token_check_only: ${{ inputs.token_check_only }}", entry)
        self.assertIn("default: false", entry)
        self.assertEqual(account_workflows().count("TOKEN_CHECK_ONLY: ${{ inputs.token_check_only }}"), 3)
        self.assertIn('account_count = 1 if os.environ.get("TOKEN_CHECK_ONLY") == "true"', worker)
        self.assertIn("needs.prepare.result == 'success' && !inputs.token_check_only", worker)

    def test_all_user_entrypoints_are_manual(self):
        names = ["lottery-all.yml", "lottery-test.yml", "lottery-3d.yml", "mall-lottery-all.yml", "mall-lottery-test.yml"]
        names.extend(f"lottery-group{index}.yml" for index in range(1, 6))
        names.extend(f"mall-lottery-group{index}.yml" for index in range(1, 6))
        for name in names:
            text = workflow_text(name)
            self.assertIn("  workflow_dispatch:", text, name)
            self.assertNotRegex(text, r"(?m)^  (schedule|push|pull_request):", name)

    def test_reusable_workers_are_not_direct_entrypoints(self):
        for name in ("lottery.yml", "lottery-retry.yml", "mall-lottery.yml"):
            text = workflow_text(name)
            self.assertIn("  workflow_call:", text, name)
            self.assertNotIn("  workflow_dispatch:", text, name)

    def test_mall_accounts_are_isolated_from_pc_groups(self):
        for index in range(1, 6):
            mall = workflow_text(f"mall-lottery-group{index}.yml")
            pc = workflow_text(f"lottery-group{index}.yml")
            self.assertIn(f"secrets.MALL_ACCOUNTS_BATCH{index}", mall)
            self.assertNotIn("MALL_ACCOUNTS", pc)

    def test_all_workflows_chain_five_groups_in_order(self):
        for name in ("lottery-all.yml", "mall-lottery-all.yml"):
            text = workflow_text(name)
            for index in range(2, 6):
                block = re.search(rf"(?ms)^  group{index}:.*?(?=^  group\d+:|\Z)", text)
                self.assertIsNotNone(block, f"{name}: missing group{index}")
                self.assertIn(f"needs: group{index - 1}", block.group(0))
                self.assertIn("if: always()", block.group(0))

    def test_group_names_are_distinct(self):
        self.assertTrue(workflow_text("lottery-all.yml").startswith("name: PC普通抽奖组\n"))
        self.assertTrue(workflow_text("mall-lottery-all.yml").startswith("name: 商城抽奖组\n"))

    def test_3d_is_independent_and_passes_its_own_accounts_and_activity(self):
        text = workflow_text("lottery-3d.yml")
        self.assertTrue(text.startswith("name: 3D抽奖\n"))
        self.assertIn("uses: ./.github/workflows/lottery.yml", text)
        self.assertIn("ACCOUNTS: ${{ secrets.ACCOUNTS_3D }}", text)
        self.assertIn("lottery_mode: 3d", text)
        self.assertIn("lottery_activity_code: LAJ3T", text)
        self.assertNotIn("workflow_call:", text)
        self.assertNotIn("lottery-3d.yml", workflow_text("lottery-all.yml"))
        self.assertNotIn("ACCOUNTS_BATCH", text)

    def test_both_initial_and_new_runner_attempts_receive_activity_configuration(self):
        text = account_workflows()
        self.assertEqual(text.count("LOTTERY_MODE: ${{ inputs.lottery_mode }}"), 2)
        self.assertEqual(text.count("LOTTERY_ACTIVITY_CODE: ${{ inputs.lottery_activity_code }}"), 2)
        self.assertEqual(text.count("ACTIVITY_ACCESS_ID: ${{ inputs.lottery_activity_access_id }}"), 2)
        self.assertIn("default: campaign", text)
        self.assertEqual(text.count('default: ""'), 4)
        self.assertIn("RETRY_MODE: ${{ inputs.lottery_mode }}", text)
        self.assertIn("EXPECTED_MATRIX: ${{ inputs.expected_matrix }}", text)
        for index in range(1, 6):
            group = workflow_text(f"lottery-group{index}.yml")
            self.assertNotIn("lottery_mode:", group)
            self.assertNotIn("lottery_activity_code:", group)

    def test_account_deadline_does_not_cancel_result_upload_or_retry(self):
        text = account_workflows()
        self.assertEqual(text.count('python account_runner.py --index "$ACCOUNT_INDEX" --output result.json --timeout-seconds 300'), 2)
        self.assertEqual(text.count("timeout-minutes: 6"), 2)
        self.assertEqual(text.count("timeout-minutes: 10"), 2)
        self.assertIn("  verify_results:", text)
        report = text.split("  report:\n", 1)[1].split("  verify_results:\n", 1)[0]
        self.assertIn("生成 JSON 和 XLSX 汇总", report)
        self.assertNotIn("raise SystemExit(1)", report)

    def test_read_only_validation_uses_final_retry_result(self):
        text = workflow_text("lottery.yml")
        self.assertNotIn("name: 校验只读测试结果", text)
        self.assertNotIn("name: 校验只读重试结果", text)
        verification = text.split("  verify_results:\n", 1)[1]
        self.assertIn("retry_lottery_3, report]", verification)
        self.assertIn("name: 校验重试后的只读结果", verification)
        self.assertIn("latest = max(rows", verification)

    def test_three_retry_rounds_chain_before_final_report(self):
        text = workflow_text("lottery.yml")
        for number, previous in ((1, "lottery"), (2, "retry_lottery"), (3, "retry_lottery_2")):
            name = "retry_lottery" if number == 1 else f"retry_lottery_{number}"
            block = re.search(rf"(?ms)^  {name}:.*?(?=^  [a-z_0-9]+:|\Z)", text).group(0)
            self.assertIn(f"needs: [prepare, {previous}]", block)
            self.assertIn(f"retry_round: {number}", block)
            self.assertIn("uses: ./.github/workflows/lottery-retry.yml", block)
            self.assertIn("always()", block)
            if number > 1:
                self.assertIn("inputs.lottery_mode == '3d'", block)
        report = text.split("  report:\n", 1)[1].split("  verify_results:\n", 1)[0]
        self.assertIn("retry_lottery_3]", report)
        retry = workflow_text("lottery-retry.yml")
        self.assertIn("-retry${{ inputs.retry_round }}", retry)
        self.assertIn("previous-results", retry)
        self.assertIn("DEPENDENCIES_OUTCOME", retry)


if __name__ == "__main__":
    unittest.main()

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def workflow_text(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


class WorkflowTests(unittest.TestCase):
    def test_split_credentials_are_masked_before_export(self):
        text = workflow_text("lottery.yml")
        self.assertEqual(text.count('print(f"::add-mask::{username.strip()}")'), 2)
        self.assertEqual(text.count('print(f"::add-mask::{password.strip()}")'), 2)
        for block in text.split('with open(os.environ["GITHUB_ENV"]')[:-1]:
            self.assertIn('print(f"::add-mask::{password.strip()}")', block)

    def test_mobile_token_probe_is_opt_in_and_skips_lottery_report(self):
        entry = workflow_text("lottery-3d.yml")
        worker = workflow_text("lottery.yml")
        self.assertIn("token_check_only: ${{ inputs.token_check_only }}", entry)
        self.assertIn("default: false", entry)
        self.assertEqual(worker.count("TOKEN_CHECK_ONLY: ${{ inputs.token_check_only }}"), 3)
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
        for name in ("lottery.yml", "mall-lottery.yml"):
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
        text = workflow_text("lottery.yml")
        self.assertEqual(text.count("LOTTERY_MODE: ${{ inputs.lottery_mode }}"), 2)
        self.assertEqual(text.count("LOTTERY_ACTIVITY_CODE: ${{ inputs.lottery_activity_code }}"), 2)
        self.assertEqual(text.count("ACTIVITY_ACCESS_ID: ${{ inputs.lottery_activity_access_id }}"), 2)
        self.assertIn("default: campaign", text)
        self.assertEqual(text.count('default: ""'), 2)
        self.assertIn("RETRY_SLIDER_ONLY: ${{ inputs.lottery_mode == '3d' }}", text)
        for index in range(1, 6):
            group = workflow_text(f"lottery-group{index}.yml")
            self.assertNotIn("lottery_mode:", group)
            self.assertNotIn("lottery_activity_code:", group)


if __name__ == "__main__":
    unittest.main()

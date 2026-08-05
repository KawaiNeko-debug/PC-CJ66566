import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOWS = ROOT / ".github" / "workflows"


def workflow_text(name: str) -> str:
    return (WORKFLOWS / name).read_text(encoding="utf-8")


class WorkflowTests(unittest.TestCase):
    def test_all_user_entrypoints_are_manual(self):
        names = ["lottery-all.yml", "lottery-test.yml", "mall-lottery-all.yml", "mall-lottery-test.yml"]
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


if __name__ == "__main__":
    unittest.main()

import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FORBIDDEN = (
    "".join(chr(code) for code in (106, 108, 99)),
    "".join(chr(code) for code in (115, 122, 108, 99, 115, 99)),
)
TEXT_SUFFIXES = {".py", ".md", ".yml", ".yaml", ".example", ".gitignore", ".txt"}


class RepositoryLiteralTests(unittest.TestCase):
    def test_forbidden_brand_token_is_not_committed(self):
        matches = []
        for path in ROOT.rglob("*"):
            if not path.is_file() or path.suffix.lower() not in TEXT_SUFFIXES:
                continue
            if any(part in {"__pycache__", ".ruff_cache", ".git"} for part in path.parts):
                continue
            text = path.read_text(encoding="utf-8", errors="ignore").lower()
            if any(token in text for token in FORBIDDEN):
                matches.append(str(path.relative_to(ROOT)))
        self.assertEqual(matches, [], f"发现禁止的品牌明文: {matches}")


if __name__ == "__main__":
    unittest.main()

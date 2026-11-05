"""Static contract tests for the public KSK-Nodes repository.

These tests do not require a running ComfyUI. They verify the published
registration keys and privacy guardrails that must survive future edits.
"""

from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


class NodeRegistrationTests(unittest.TestCase):
    def test_old_node_key_is_preserved(self):
        text = (ROOT / "__init__.py").read_text(encoding="utf-8")
        self.assertIn('"KSK_MatrixPairBatch"', text)
        self.assertIn("KSK_MatrixPairBatch", (ROOT / "nodes.py").read_text(encoding="utf-8"))

    def test_new_node_key_is_registered(self):
        text = (ROOT / "__init__.py").read_text(encoding="utf-8")
        self.assertIn('"KSK_PoseCostumeMatrixBatch"', text)
        self.assertIn("KSK_PoseCostumeMatrixBatch", (ROOT / "nodes_pose_costume.py").read_text(encoding="utf-8"))

    def test_both_nodes_have_distinct_keys(self):
        text = (ROOT / "__init__.py").read_text(encoding="utf-8")
        self.assertIn("KSK_MatrixPairBatch", text)
        self.assertIn("KSK_PoseCostumeMatrixBatch", text)

    def test_new_frontend_uses_new_node_type(self):
        core = (ROOT / "web" / "aiksk_pc_core.js").read_text(encoding="utf-8")
        match = re.search(r'export const NODE_TYPE = "([^"]+)"', core)
        self.assertIsNotNone(match)
        self.assertEqual(match.group(1), "KSK_PoseCostumeMatrixBatch")


class ModuleSyntaxTests(unittest.TestCase):
    def test_python_files_compile(self):
        for path in ROOT.rglob("*.py"):
            if ".git" in path.parts or "__pycache__" in path.parts:
                continue
            with self.subTest(path=str(path.relative_to(ROOT))):
                ast.parse(path.read_text(encoding="utf-8"), filename=str(path))


class PrivacyTests(unittest.TestCase):
    PATTERNS = (
        re.compile(r"C:\\Users\\KSK", re.I),
        re.compile(r"D:\\AIKSK", re.I),
        re.compile(r"hf_[A-Za-z0-9]{20,}"),
        re.compile(r"ghp_[A-Za-z0-9]{20,}"),
        re.compile(r"sk-[A-Za-z0-9]{20,}"),
        re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}"),
    )
    TEXT_EXT = {".py", ".js", ".json", ".md", ".txt", ".css", ".html", ".yaml", ".yml", ".toml"}
    SKIP_DIRS = {".git", "__pycache__", ".pytest_cache", "node_modules"}

    def test_no_personal_paths_tokens_or_emails(self):
        hits = []
        for path in ROOT.rglob("*"):
            if not path.is_file() or any(part in self.SKIP_DIRS for part in path.parts):
                continue
            if path.suffix.lower() not in self.TEXT_EXT:
                continue
            text = path.read_text(encoding="utf-8", errors="ignore")
            for pattern in self.PATTERNS:
                match = pattern.search(text)
                if match:
                    hits.append((str(path.relative_to(ROOT)), pattern.pattern, match.group(0)))
        self.assertEqual(hits, [], hits)

    def test_no_secret_or_database_files(self):
        offenders = []
        for path in ROOT.rglob("*"):
            if not path.is_file():
                continue
            if any(part in self.SKIP_DIRS for part in path.parts):
                continue
            if path.suffix.lower() in {".pem", ".key", ".p12", ".sqlite", ".sqlite3", ".db"}:
                offenders.append(str(path.relative_to(ROOT)))
            if path.name in {".env", "credentials.json", "secrets.json"}:
                offenders.append(str(path.relative_to(ROOT)))
        self.assertEqual(offenders, [], offenders)


if __name__ == "__main__":
    unittest.main()

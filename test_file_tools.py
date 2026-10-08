"""文件工具的离线边界检查，只使用项目内专门创建的测试文件。"""

import os
import tempfile
import unittest
from pathlib import Path

from file_tools import EXPECTED_CONTENT, INITIAL_CONTENT, MAX_TEXT_BYTES, FileAccess, prepare_practice_file


class FileToolTests(unittest.TestCase):
    def setUp(self):
        workspace = tempfile.TemporaryDirectory(prefix=".file-tools-unit-", dir=Path(__file__).parent)
        self.addCleanup(workspace.cleanup)
        self.workspace = Path(workspace.name).resolve()
        self.root = self.workspace / "project"
        self.root.mkdir()
        self.file = prepare_practice_file(self.root)
        self.access = FileAccess(self.root, self.file)
        self.path = self.file.name

    def test_read_then_write_changes_only_one_line(self):
        read = self.access.read_file(self.path)
        self.assertEqual(read["content"], INITIAL_CONTENT)
        written = self.access.write_file(self.path, EXPECTED_CONTENT, read["content"])
        self.assertTrue(written["ok"])
        self.assertEqual(written["content"], EXPECTED_CONTENT)
        self.assertEqual(self.file.read_text(), EXPECTED_CONTENT)
        before, after = INITIAL_CONTENT.splitlines(), self.file.read_text().splitlines()
        self.assertEqual([i for i, pair in enumerate(zip(before, after)) if pair[0] != pair[1]], [1])

    def test_each_run_creates_a_different_file_without_overwriting(self):
        self.file.write_text("保留之前的结果", encoding="utf-8")
        new = prepare_practice_file(self.root)
        self.assertNotEqual(new, self.file)
        self.assertEqual(self.file.read_text(), "保留之前的结果")
        self.assertEqual(new.read_text(), INITIAL_CONTENT)

    def test_project_escape_and_important_files_are_rejected(self):
        important = self.root / "agent.py"
        important.write_text("important sentinel", encoding="utf-8")
        for path in ("../outside.txt", "agent.py", ".env", str(self.file), ""):
            with self.subTest(path=path):
                self.assertFalse(self.access.read_file(path)["ok"])
                self.assertFalse(self.access.write_file(path, "overwrite", INITIAL_CONTENT)["ok"])
        self.assertEqual(important.read_text(), "important sentinel")
        self.assertEqual(self.file.read_text(), INITIAL_CONTENT)

    def test_write_before_read_and_incorrect_original_content_are_rejected(self):
        self.assertFalse(self.access.write_file(self.path, EXPECTED_CONTENT, INITIAL_CONTENT)["ok"])
        self.access.read_file(self.path)
        self.assertFalse(self.access.write_file(self.path, EXPECTED_CONTENT, "guessed content")["ok"])
        self.assertEqual(self.file.read_text(), INITIAL_CONTENT)

    def test_changed_file_is_not_overwritten(self):
        self.access.read_file(self.path)
        self.file.write_text("用户在读取后改了文件", encoding="utf-8")
        self.assertFalse(self.access.write_file(self.path, EXPECTED_CONTENT, INITIAL_CONTENT)["ok"])
        self.assertEqual(self.file.read_text(), "用户在读取后改了文件")

    def test_symlink_cannot_read_or_overwrite_another_file(self):
        outside = self.workspace / "outside.txt"
        outside.write_text("outside sentinel", encoding="utf-8")
        self.access.read_file(self.path)
        self.file.unlink()
        self.file.symlink_to(outside)
        self.assertFalse(self.access.read_file(self.path)["ok"])
        self.assertFalse(self.access.write_file(self.path, EXPECTED_CONTENT, INITIAL_CONTENT)["ok"])
        self.assertEqual(outside.read_text(), "outside sentinel")

    def test_hard_link_cannot_overwrite_another_file(self):
        outside = self.workspace / "outside.txt"
        outside.write_text(INITIAL_CONTENT, encoding="utf-8")
        self.access.read_file(self.path)
        self.file.unlink()
        os.link(outside, self.file)
        self.assertFalse(self.access.write_file(self.path, EXPECTED_CONTENT, INITIAL_CONTENT)["ok"])
        self.assertEqual(outside.read_text(), INITIAL_CONTENT)

    def test_replaced_regular_file_is_rejected(self):
        self.access.read_file(self.path)
        replacement = self.root / "replacement.txt"
        replacement.write_text(INITIAL_CONTENT, encoding="utf-8")
        replacement.replace(self.file)
        self.assertFalse(self.access.read_file(self.path)["ok"])
        self.assertFalse(self.access.write_file(self.path, EXPECTED_CONTENT, INITIAL_CONTENT)["ok"])
        self.assertEqual(self.file.read_text(), INITIAL_CONTENT)

    def test_non_utf8_and_oversized_content_are_rejected(self):
        self.file.write_bytes(b"\xff\xfe")
        self.assertFalse(self.access.read_file(self.path)["ok"])
        self.file.write_text(INITIAL_CONTENT, encoding="utf-8")
        self.access.read_file(self.path)
        self.assertFalse(self.access.write_file(self.path, "x" * (MAX_TEXT_BYTES + 1), INITIAL_CONTENT)["ok"])
        self.assertEqual(self.file.read_text(), INITIAL_CONTENT)
        self.file.write_bytes(b"x" * (MAX_TEXT_BYTES + 1))
        self.assertFalse(self.access.read_file(self.path)["ok"])


if __name__ == "__main__":
    unittest.main()

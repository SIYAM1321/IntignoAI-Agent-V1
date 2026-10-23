import os
import tempfile
import unittest
from pathlib import Path

from tools import files


class FileToolTests(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="intignoai-files-"))
        self.ws = files.resolve_workspace(str(self.tmp / "workspace"))
        self.ws.mkdir(parents=True, exist_ok=True)
        self.outside = self.tmp / "outside.txt"
        self.outside.write_text("outside", encoding="utf-8")

    def tearDown(self):
        os.environ.pop("FILE_SCOPE", None)

    def test_create_read_update_move_copy_rename_delete(self):
        created = files.create_file("hello.py", "print('hello')\n", self.ws)
        self.assertTrue(created["created"])
        read = files.read_file("hello.py", self.ws)
        self.assertIn("print", read["content"])
        files.update_file("hello.py", "print('updated')\n", self.ws)
        files.copy_file("hello.py", "copy.py", self.ws)
        files.move_file("copy.py", "nested/copy.py", self.ws)
        files.rename_file("nested/copy.py", "renamed.py", self.ws)
        files.delete_file("nested/renamed.py", self.ws)
        self.assertTrue((self.ws / "hello.py").exists())

    def test_absolute_paths_allowed_by_default(self):
        read = files.read_file(str(self.outside), self.ws)
        self.assertEqual(read["content"], "outside")

    def test_workspace_scope_can_be_enabled(self):
        os.environ["FILE_SCOPE"] = "workspace"
        with self.assertRaises(PermissionError):
            files.read_file(str(self.outside), self.ws)


if __name__ == "__main__":
    unittest.main()

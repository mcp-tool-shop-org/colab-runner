import hashlib
import re
import unittest
from pathlib import Path

LIB = Path(__file__).parents[1] / "colab_runner" / "window_lib"


class WindowLibProvenanceTest(unittest.TestCase):
    def table(self):
        rows = re.findall(r"\| `([a-z_.]+)` \| `([0-9a-f]{64})` \|", (LIB / "PROVENANCE.md").read_text(encoding="utf-8"))
        return dict(rows)

    def test_the_vendored_files_match_the_provenance_table_byte_for_byte(self):
        table = self.table()
        self.assertEqual(sorted(table), ["colab_window.sh", "launch_guards.py", "remote_runner.py"])
        for name, want in table.items():
            self.assertEqual(hashlib.sha256((LIB / name).read_bytes()).hexdigest(), want, name)

    def test_no_other_file_sits_in_the_vendored_directory(self):
        names = sorted(p.name for p in LIB.iterdir() if p.name != "__pycache__")
        self.assertEqual(names, ["PROVENANCE.md", "colab_window.sh", "launch_guards.py", "remote_runner.py"])

    def test_the_vendored_files_have_unix_line_endings(self):
        for name in ("colab_window.sh", "launch_guards.py", "remote_runner.py"):
            self.assertNotIn(b"\r\n", (LIB / name).read_bytes(), name)


if __name__ == "__main__":
    unittest.main()

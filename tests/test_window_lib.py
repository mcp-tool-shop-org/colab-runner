import hashlib
import re
import unittest
from pathlib import Path

LIB = Path(__file__).parents[1] / "colab_runner" / "window_lib"
EMPTY_SHA256 = "e3b0c44298fc1c149afbf4c8996fb92427ae41e4649b934ca495991b7852b855"


def parse_table(text: str) -> dict[str, str]:
    rows = re.findall(r"\| `([a-z_.]+)` \| `([0-9a-f]{64})` \|", text)
    table: dict[str, str] = {}
    for name, digest in rows:
        if name in table:
            raise ValueError("duplicate provenance row: %s" % name)
        table[name] = digest
    return table


class WindowLibProvenanceTest(unittest.TestCase):
    def table(self):
        return parse_table((LIB / "PROVENANCE.md").read_text(encoding="utf-8"))

    def test_the_vendored_files_match_the_provenance_table_byte_for_byte(self):
        table = self.table()
        self.assertEqual(sorted(table), ["launch_guards.py"])
        for name, want in table.items():
            self.assertEqual(hashlib.sha256((LIB / name).read_bytes()).hexdigest(), want, name)
        self.assertEqual(hashlib.sha256((LIB / "__init__.py").read_bytes()).hexdigest(), EMPTY_SHA256)

    def test_a_duplicate_provenance_row_is_refused(self):
        dup = (
            "| `launch_guards.py` | `" + "0" * 64 + "` |\n"
            "| `launch_guards.py` | `" + "1" * 64 + "` |\n"
        )
        with self.assertRaisesRegex(ValueError, "duplicate provenance row: launch_guards.py"):
            parse_table(dup)

    def test_no_other_file_sits_in_the_vendored_directory(self):
        names = sorted(p.name for p in LIB.iterdir() if p.name != "__pycache__")
        self.assertEqual(names, ["PROVENANCE.md", "__init__.py", "launch_guards.py"])

    def test_the_vendored_files_have_unix_line_endings(self):
        for name in ("__init__.py", "launch_guards.py"):
            self.assertNotIn(b"\r\n", (LIB / name).read_bytes(), name)


class WheelContentsTest(unittest.TestCase):
    def test_the_built_wheel_ships_the_vendored_files_and_the_vm_cells(self):
        import subprocess
        import sys
        import tempfile
        import zipfile

        root = Path(__file__).parents[1]
        with tempfile.TemporaryDirectory() as tmp:
            proc = subprocess.run(
                [sys.executable, "-m", "pip", "wheel", str(root), "--no-deps", "--no-build-isolation", "-q", "-w", tmp],
                capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr[-800:])
            wheel = next(Path(tmp).glob("colab_runner-*.whl"))
            names = set(zipfile.ZipFile(wheel).namelist())
        for want in (
            "colab_runner/window_lib/__init__.py",
            "colab_runner/window_lib/launch_guards.py",
            "colab_runner/window_lib/PROVENANCE.md",
            "colab_runner/vm/lib.py",
            "colab_runner/vm/start.py",
            "colab_runner/vm/poll.py",
            "colab_runner/vm/resident.py",
            "colab_runner/vm/kill.py",
        ):
            self.assertIn(want, names)


if __name__ == "__main__":
    unittest.main()

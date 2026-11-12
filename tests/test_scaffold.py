import io
import unittest
from contextlib import redirect_stderr, redirect_stdout

from colab_runner import __version__
from colab_runner.__main__ import main


class ScaffoldTest(unittest.TestCase):
    def test_version(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main(["--version"]), 0)
        self.assertEqual(out.getvalue().strip(), __version__)

    def test_help(self):
        out = io.StringIO()
        with redirect_stdout(out):
            self.assertEqual(main([]), 0)
            self.assertEqual(main(["--help"]), 0)
        self.assertIn("usage", out.getvalue())

    def test_unknown_argument_exits_2(self):
        err = io.StringIO()
        with redirect_stderr(err):
            self.assertEqual(main(["--nope"]), 2)
        self.assertIn("unknown arguments", err.getvalue())


if __name__ == "__main__":
    unittest.main()

"""PR #3's review follow-ups (B4, C1-C7) and the fix-1 review's (C3) and (C4) that need a test of their own."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from colab_runner.colab_cli import BinaryDownloadRefused, _byte_twin_source, default_quarantine_dir, scrub_local
from colab_runner.errors import Refused
from colab_runner.runner import Runner
from colab_runner.window_lib import launch_guards as lg
from tests.fakes import FakeColab, VmLog, make, rec
from tests.test_tokens import fake_tokens, values

POSIX = os.name == "posix"
PNG_HEAD = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"


def poisoned(t: dict) -> bytes:
    return PNG_HEAD + b"meta=" + t["hf"].encode("ascii") + b"\x00end"


class AfterTheRunTest(unittest.TestCase):
    def test_b4_a_quarantine_after_run_returned_reaches_runner_result_json(self):
        t = fake_tokens()
        with tempfile.TemporaryDirectory() as tmp:
            colab = FakeColab(polls=lambda n, env: rec(rc=0))
            runner, _ = make(colab, tmp)
            self.assertEqual(runner.run(script="/content/x.sh", wall_s=600).reason, "done")
            self.assertEqual(json.loads((runner.out_dir / "runner_result.json").read_text())["quarantined"], [])
            colab.run_log = poisoned(t)  # a lane's own download through the same Runner, after run()
            with self.assertRaises(BinaryDownloadRefused):
                runner.download("/content/job.run.out", runner.out_dir / "renders" / "late.png")
            written = json.loads((runner.out_dir / "runner_result.json").read_text())
            self.assertEqual(len(written["quarantined"]), 1)
            self.assertEqual(written["quarantined"][0]["pattern"], "hf_")
            self.assertEqual((written["reason"], written["stopped"]), ("done", True))  # the end itself unchanged
            self.assertEqual(Path(written["quarantined"][0]["path"]).read_bytes(), poisoned(t))
            for v in values(t):
                self.assertNotIn(v, json.dumps(written))

    def test_b4_a_download_before_any_run_writes_no_result_file(self):
        t = fake_tokens()
        with tempfile.TemporaryDirectory() as tmp:
            colab = FakeColab(run_log=poisoned(t))
            runner, _ = make(colab, tmp)
            with self.assertRaises(BinaryDownloadRefused):
                runner.download("/content/job.run.out", runner.out_dir / "x.png")
            self.assertFalse((runner.out_dir / "runner_result.json").exists())

    def test_c6_a_refusal_during_cleanup_keeps_the_end_it_replaced(self):
        t = fake_tokens()
        with tempfile.TemporaryDirectory() as tmp:
            # a resident run trips (progress_stall), and the run log its cleanup collects is a poisoned binary
            colab = FakeColab(vm_poll_file=rec(progress=1), run_log=poisoned(t))
            runner, _ = make(colab, tmp)
            with self.assertRaises(BinaryDownloadRefused):
                runner.run(script="/content/x.sh", wall_s=36000, resident=True, progress_stall_s=300)
            res = runner._res
            self.assertEqual((res.reason, res.prior_reason, res.stopped), ("error", "progress_stall", True))
            self.assertIn("(the run had ended progress_stall: no progress for", res.detail)
            self.assertEqual((res.refusal["code"], res.refusal["retryable"]), ("BINARY_DOWNLOAD_TOKEN", False))
            written = json.loads((runner.out_dir / "runner_result.json").read_text())
            self.assertEqual((written["reason"], written["prior_reason"]), ("error", "progress_stall"))

    def test_c6_an_error_with_nothing_before_it_has_no_prior_reason(self):
        with tempfile.TemporaryDirectory() as tmp:
            colab = FakeColab(vm_poll_file=rec(rc=0), run_log=poisoned(fake_tokens()))
            runner, _ = make(colab, tmp)
            with self.assertRaises(BinaryDownloadRefused):
                runner.run(script="/content/x.sh", wall_s=600, resident=True)
            self.assertEqual((runner._res.reason, runner._res.prior_reason), ("error", None))


class QuarantinePathTest(unittest.TestCase):
    def test_c1_the_quarantine_is_named_from_the_resolved_out_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp).resolve()
            (root / "runs" / "a").mkdir(parents=True)
            for given, real in ((root / "runs" / "a" / "..", root / "runs"), (root / "runs" / ".", root / "runs")):
                with self.subTest(given=str(given)):
                    q = default_quarantine_dir(given)
                    self.assertEqual(q, real.parent / (real.name + ".quarantine"))
                    with self.assertRaises(ValueError):
                        q.relative_to(real)  # never inside the out dir
            with contextlib.chdir(root / "runs" / "a"):
                q = default_quarantine_dir(Path(".."))
            self.assertEqual(q, root / "runs.quarantine")

    def test_c1_a_filesystem_root_out_dir_is_refused(self):
        anchor = Path(Path.cwd().anchor)
        with self.assertRaises(Refused) as cm:
            default_quarantine_dir(anchor)
        self.assertEqual(cm.exception.code, "OUT_DIR_INVALID")
        colab = FakeColab()
        with self.assertRaises(Refused):
            Runner(colab, "s", anchor)
        from colab_runner.__main__ import main

        err = io.StringIO()
        with mock.patch("colab_runner.__main__.cli_from_env", return_value=colab), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(io.StringIO()):
            code = main(["run", "--session", "s", "--out-dir", str(anchor), "--script", "/c/x.sh", "--wall-s", "60",
                         "--max-rate", "1", "--cap", "5", "--cleanup-bound-s", "600"])
        self.assertEqual((code, colab.calls), (1, []))
        self.assertIn("REFUSED: OUT_DIR_INVALID:", err.getvalue())


class DirectoryScrubTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.t = fake_tokens()

    def test_c2_a_failed_move_goes_on_with_the_other_files_and_raises_at_the_end(self):
        import shutil

        bundle = self.root / "bundle"
        bundle.mkdir()
        (bundle / "a_first.png").write_bytes(poisoned(self.t))
        (bundle / "b_second.png").write_bytes(poisoned(self.t) + b"2")
        (bundle / "c_notes.txt").write_text("Bearer %s\n" % self.t["bearer"], encoding="utf-8")
        real_move, seen = shutil.move, []

        def move(src, dst):
            if src.endswith("a_first.png"):
                raise PermissionError("cannot move %s" % src)
            return real_move(src, dst)

        qdir, logs = self.root / "bundle.quarantine", []
        with mock.patch("colab_runner.colab_cli.shutil.move", side_effect=move):
            with self.assertRaises(BinaryDownloadRefused) as ctx:
                scrub_local(bundle, quarantine_dir=qdir, on_quarantine=seen.append, log=logs.append)
        self.assertEqual(len(ctx.exception.failures), 1)
        self.assertIsInstance(ctx.exception.failures[0], PermissionError)
        self.assertEqual(len(ctx.exception.removed), 1)
        self.assertEqual(len(ctx.exception.quarantined), 1)
        self.assertTrue(any("a_first.png could not be moved to quarantine and was removed" in ln for ln in logs))
        for v in values(self.t):
            self.assertNotIn(v, " ".join(logs))
        self.assertFalse((bundle / "a_first.png").exists())  # removed, never left with its token
        self.assertEqual((qdir / "b_second.png").read_bytes(), poisoned(self.t) + b"2")  # the scrub went on
        self.assertEqual([Path(e["path"]).name for e in seen], ["b_second.png"])
        self.assertEqual((bundle / "c_notes.txt").read_text(encoding="utf-8"), "Bearer [REDACTED]\n")

    def test_c3_an_unlistable_subdirectory_is_refused_not_skipped(self):
        bundle = self.root / "bundle"
        (bundle / "locked").mkdir(parents=True)
        (bundle / "locked" / "hidden.txt").write_text("x", encoding="utf-8")
        (bundle / "open.txt").write_text("Bearer %s\n" % self.t["bearer"], encoding="utf-8")
        real_scandir = os.scandir

        def scandir(path="."):
            if Path(path).name == "locked":
                raise PermissionError("cannot list %s" % path)
            return real_scandir(path)

        with mock.patch("os.scandir", side_effect=scandir):
            with self.assertRaises(PermissionError):
                scrub_local(bundle)
        self.assertEqual((bundle / "open.txt").read_text(encoding="utf-8"), "Bearer [REDACTED]\n")

    def test_c3_only_regular_files_are_read(self):
        bundle = self.root / "bundle"
        bundle.mkdir()
        (bundle / "notes.txt").write_text("Bearer %s\n" % self.t["bearer"], encoding="utf-8")
        odd = bundle / "odd"
        odd.write_text("never read", encoding="utf-8")
        real_is_file, read = Path.is_file, []

        def is_file(self):
            return False if self.name == "odd" else real_is_file(self)

        real_read = Path.read_bytes

        def read_bytes(self):
            read.append(self.name)
            return real_read(self)

        with mock.patch.object(Path, "is_file", is_file), mock.patch.object(Path, "read_bytes", read_bytes):
            scrub_local(bundle)
        self.assertEqual(read, ["notes.txt"])  # the entry that is not a regular file is never read
        self.assertFalse(odd.exists())  # it is removed instead
        self.assertEqual((bundle / "notes.txt").read_text(encoding="utf-8"), "Bearer [REDACTED]\n")

    @unittest.skipUnless(POSIX and hasattr(os, "mkfifo"), "a real FIFO needs POSIX")
    def test_c3_a_fifo_in_a_download_never_blocks_the_scrub(self):
        bundle = self.root / "bundle"
        bundle.mkdir()
        os.mkfifo(bundle / "pipe")
        (bundle / "notes.txt").write_text("Bearer %s\n" % self.t["bearer"], encoding="utf-8")
        scrub_local(bundle)  # a read of the FIFO would block here
        self.assertFalse((bundle / "pipe").exists())
        self.assertEqual((bundle / "notes.txt").read_text(encoding="utf-8"), "Bearer [REDACTED]\n")
        os.mkfifo(self.root / "lone_pipe")
        scrub_local(self.root / "lone_pipe")
        self.assertFalse((self.root / "lone_pipe").exists())


class CliQuotingTest(unittest.TestCase):
    def test_c5_colab_runner_cli_takes_posix_quoting_for_a_windows_path(self):
        from colab_runner.colab_cli import cli_from_env

        cases = {
            r'"C:\tools\colab.exe" --flag': ["C:\\tools\\colab.exe", "--flag"],
            r"'C:\tools\colab.exe' --flag": ["C:\\tools\\colab.exe", "--flag"],
            "C:/tools/colab.exe --flag": ["C:/tools/colab.exe", "--flag"],
            r'"C:\Program Files\colab\colab.exe"': ["C:\\Program Files\\colab\\colab.exe"],
            r"C:\tools\colab.exe": ["C:toolscolab.exe"],  # unquoted: POSIX drops the backslashes (the README says so)
        }
        for raw, want in cases.items():
            with self.subTest(raw=raw), mock.patch.dict(os.environ, {"COLAB_RUNNER_CLI": raw}):
                self.assertEqual(cli_from_env().argv, want)


class VmCellTest(unittest.TestCase):
    def test_c4_already_running_with_no_pid_file_answers_pid_0(self):
        # PR 3 review (C4): a status file of this run with its pid file gone never raises TypeError
        with tempfile.TemporaryDirectory() as tmp:
            ns = VmLog(tmp).ns
            (Path(tmp) / "job.status.json").write_text('{"rc": 0, "run_id": "run-1"}', encoding="utf-8")
            (Path(tmp) / "job.run_id").write_text("run-1\n", encoding="utf-8")
            with mock.patch.dict(os.environ, {"CR_RUN_ID": "run-1"}):
                self.assertEqual(ns["cr_start_job"](), "CR_ALREADY_RUNNING pid=0")


class ByteTwinTest(unittest.TestCase):
    """Fix-1 review (C3): _byte_twin_source refuses what it cannot translate span for span."""

    def test_patterns_it_refuses(self):
        for src in (r"[^a]{3}", r"[^a]", r"[^\s]?", r"[^a]{2,}", r"a.b", r"x.+", r"\S+", r"\w+", r"[é]+"):
            with self.subTest(src=src), self.assertRaises(ValueError):
                _byte_twin_source(src)

    def test_patterns_it_translates(self):
        import re

        for src, text in ((r"[^\s&]+", "aé b"), (r"[^a]*", "bé\u3000c"), (r"[A-Z]{3}", "xABCx"), (r"\.", "a.b"),
                          (r"[.]+", "a..b"), (r"(Bearer\s+)[A-Za-z0-9._\-]+", "Bearer\u00a0abc")):
            with self.subTest(src=src):
                b = re.compile(_byte_twin_source(src))
                s = re.compile(src)
                data = text.encode("utf-8")
                spans = [(len(text[:m.start()].encode()), len(text[:m.end()].encode())) for m in s.finditer(text)]
                self.assertEqual([m.span() for m in b.finditer(data)], spans)

    def test_every_library_pattern_has_a_twin(self):
        from colab_runner.colab_cli import BYTE_TOKEN_PATTERNS

        self.assertEqual(len(BYTE_TOKEN_PATTERNS), len(lg.TOKEN_SCRUB_PATTERNS))


if __name__ == "__main__":
    unittest.main()

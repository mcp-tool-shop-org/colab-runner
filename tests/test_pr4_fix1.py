"""PR #4 fix 1, after Carbon's FAIL at b6dc8bc5: directory symlinks are unlinked, a quarantine never escapes its
directory, pre-marker errors are refusals (exit 1), the stop hint after a RUN_ERROR, and the review's (C)s."""

import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from colab_runner import colab_cli
from colab_runner.colab_cli import BinaryDownloadRefused, QuarantineEscape, _byte_twin_source, scrub_local
from colab_runner.errors import CODES
from colab_runner.runner import Runner, RunResult
from tests.fakes import FakeClock, FakeColab, make, rec
from tests.test_tokens import fake_tokens

PNG_HEAD = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"


def poisoned(t: dict, tag: bytes = b"") -> bytes:
    return PNG_HEAD + b"meta=" + t["hf"].encode("ascii") + b"\x00end" + tag


def snapshot(folder: Path) -> dict:
    return {p.relative_to(folder).as_posix(): p.read_bytes() for p in sorted(folder.rglob("*")) if p.is_file()}


def cli(argv, colab=None, env=None):
    """main(argv), on `colab` (else the CLI COLAB_RUNNER_CLI names) with a fake clock; (code, stdout, stderr)."""
    from colab_runner.__main__ import main

    clock = FakeClock() if colab is None else (colab.clock or FakeClock())
    if colab is not None:
        colab.clock = clock
    out, err = io.StringIO(), io.StringIO()
    with contextlib.ExitStack() as stack:
        if colab is not None:
            stack.enter_context(mock.patch("colab_runner.__main__.cli_from_env", return_value=colab))
        stack.enter_context(mock.patch("colab_runner.__main__.Runner", side_effect=lambda *a, **kw: Runner(
            *a, clock=clock.now, sleep=clock.sleep, **kw)))
        stack.enter_context(mock.patch.dict(os.environ, env or {}))
        stack.enter_context(contextlib.redirect_stdout(out))
        stack.enter_context(contextlib.redirect_stderr(err))
        code = main(argv)
    return code, out.getvalue(), err.getvalue()


BASE = ["--session", "s", "--script", "/c/x.sh", "--wall-s", "3600", "--max-rate", "1", "--cap", "5",
        "--cleanup-bound-s", "1200"]


class DirectorySymlinkTest(unittest.TestCase):
    """Fix 1, (A) 1: a downloaded symlink to a directory is unlinked, never followed."""

    def test_a_link_to_a_directory_is_unlinked_and_its_target_untouched(self):
        t = fake_tokens()
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as elsewhere:
            root = Path(tmp)
            bundle, sibling, outside = root / "bundle", root / "sibling", Path(elsewhere) / "outside"
            for d in (bundle / "sub", sibling, outside):
                d.mkdir(parents=True)
            for d in (sibling, outside):  # what a followed link would scrub or quarantine
                (d / "notes.txt").write_text("Bearer %s\n" % t["bearer"], encoding="utf-8")
                (d / "render.png").write_bytes(poisoned(t))
            (bundle / "keep.txt").write_text("Bearer %s\n" % t["bearer"], encoding="utf-8")
            try:
                (bundle / "to_sibling").symlink_to(sibling, target_is_directory=True)
                (bundle / "sub" / "to_outside").symlink_to(outside, target_is_directory=True)
            except OSError:
                self.skipTest("symlinks need privilege on this Windows host (CI runs Linux)")
            before = {d: snapshot(d) for d in (sibling, outside)}
            scrub_local(bundle)  # no binary hit inside the bundle itself, so nothing is refused
            self.assertFalse(os.path.lexists(bundle / "to_sibling"))
            self.assertFalse(os.path.lexists(bundle / "sub" / "to_outside"))
            for d in (sibling, outside):
                self.assertTrue(d.is_dir())
                self.assertEqual(snapshot(d), before[d])  # never scrubbed, never moved
            self.assertEqual((bundle / "keep.txt").read_text(encoding="utf-8"), "Bearer [REDACTED]\n")


class QuarantineEscapeTest(unittest.TestCase):
    """Fix 1, (A) 2: every quarantine path is resolved and checked to stay inside its quarantine directory."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.t = fake_tokens()

    def runs_with_a_hit(self) -> Path:
        (self.root / "runs" / "a").mkdir(parents=True)
        (self.root / "runs" / "x.png").write_bytes(poisoned(self.t))
        return self.root / "runs" / "a" / ".."

    def test_a_root_ending_in_dotdot_with_the_bare_temp_quarantine(self):
        given = self.runs_with_a_hit()
        tempbase = self.root / "tempbase"
        tempbase.mkdir()
        with mock.patch.object(tempfile, "tempdir", str(tempbase)), self.assertRaises(BinaryDownloadRefused) as cm:
            scrub_local(given)
        dest = Path(cm.exception.quarantined[0]["path"])
        qdirs = [p for p in tempbase.iterdir()]
        self.assertEqual(len(qdirs), 1)  # the colab-runner-quarantine-* folder and nothing beside it
        self.assertTrue(qdirs[0].name.startswith(colab_cli.QUARANTINE_PREFIX))
        self.assertEqual(dest, qdirs[0] / "runs" / "x.png")  # the resolved root's own name, never ".."
        self.assertEqual(dest.read_bytes(), poisoned(self.t))
        self.assertFalse((tempbase / "x.png").exists())  # where the unresolved ".." put it

    def test_a_root_ending_in_dotdot_with_a_custom_quarantine(self):
        given = self.runs_with_a_hit()
        q = self.root / "q"
        with self.assertRaises(BinaryDownloadRefused) as cm:
            scrub_local(given, quarantine_dir=q)
        self.assertEqual(Path(cm.exception.quarantined[0]["path"]), q / "runs" / "x.png")
        self.assertEqual(snapshot(self.root / "runs"), {})
        self.assertFalse((self.root / "x.png").exists())

    def test_runner_download_to_a_path_ending_in_dotdot(self):
        parent = self.root / "parent"
        out = parent / "out"
        out.mkdir(parents=True)
        (out / "secret.png").write_bytes(poisoned(self.t))  # already in the out dir when the download lands
        colab = FakeColab()  # the download itself fails (no such remote), and the scrub still runs over the target
        runner = Runner(colab, "s", out, clock=FakeClock().now)
        with self.assertRaises(BinaryDownloadRefused) as cm:
            runner.download("/content/nothing.bin", parent / "work" / "..")
        dest = Path(cm.exception.quarantined[0]["path"])
        self.assertEqual(dest, parent / "out.quarantine" / "secret.png")  # relative to the resolved out dir
        self.assertEqual(snapshot(out), {})  # nothing quarantined inside the out dir
        self.assertEqual(dest.read_bytes(), poisoned(self.t))

    def test_an_explicit_quarantine_never_needs_a_sibling_of_the_root(self):
        # the old code called default_quarantine_dir(root), which refuses a filesystem root even with a quarantine given
        with mock.patch.object(colab_cli, "default_quarantine_dir", side_effect=AssertionError("not called")):
            given = self.runs_with_a_hit()
            with self.assertRaises(BinaryDownloadRefused):
                scrub_local(given, quarantine_dir=self.root / "q")

    def test_the_escape_check_refuses_by_name_and_removes_the_file(self):
        src = self.root / "render.png"
        src.write_bytes(poisoned(self.t))
        q = self.root / "q"
        logs: list[str] = []
        with mock.patch.object(colab_cli, "_quarantine_rel", return_value=Path("..") / "escaped.png"):
            with self.assertRaises(QuarantineEscape) as cm:
                scrub_local(src, quarantine_dir=q, log=logs.append)
        self.assertEqual(cm.exception.code, "QUARANTINE_ESCAPE")
        self.assertIsInstance(cm.exception, OSError)
        self.assertIn("QUARANTINE_ESCAPE", CODES)
        self.assertFalse(src.exists())  # removed, never left with its token
        self.assertFalse((self.root / "escaped.png").exists())  # and never moved outside q
        self.assertTrue(any("could not be moved to quarantine and was removed" in ln for ln in logs))

    def test_an_escape_in_cleanup_ends_the_run_as_error_never_swallowed(self):
        colab = FakeColab(vm_poll_file=rec(progress=1), run_log=poisoned(self.t))
        runner, _ = make(colab, self.root)
        with mock.patch.object(colab_cli, "_quarantine_rel", return_value=Path("..") / "escaped.png"):
            with self.assertRaises(QuarantineEscape):
                runner.run(script="/c/x.sh", wall_s=36000, resident=True, progress_stall_s=300)
        res = runner._res
        self.assertEqual((res.reason, res.prior_reason, res.stopped), ("error", "progress_stall", True))
        self.assertEqual(res.refusal["code"], "QUARANTINE_ESCAPE")
        self.assertFalse((runner.out_dir / "job.run.out").exists())

    def test_the_escape_refusal_reaches_the_cli_as_a_run_error(self):
        colab = FakeColab(vm_poll_file=rec(rc=0), run_log=poisoned(self.t))
        with mock.patch.object(colab_cli, "_quarantine_rel", return_value=Path("..") / "escaped.png"):
            code, out, err = cli(["run"] + BASE + ["--out-dir", str(self.root / "o"), "--resident"], colab)
        self.assertEqual(code, 2)
        self.assertIn("RUN_ERROR: QUARANTINE_ESCAPE: ", err)
        self.assertIn("hint: report it", err)
        self.assertFalse((self.root / "escaped.png").exists())


class PreMarkerRefusalTest(unittest.TestCase):
    """Fix 1, (B): an error before the marker is a refusal (exit 1): the runner never owned a session."""

    def test_a_colab_runner_cli_that_cannot_be_split_is_cli_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            code, out, err = cli(["run"] + BASE + ["--out-dir", str(Path(tmp) / "o")],
                                 env={"COLAB_RUNNER_CLI": '"/opt/colab --flag'})
            self.assertFalse((Path(tmp) / "o").exists())
        self.assertEqual((code, out), (1, ""))
        self.assertTrue(err.startswith("REFUSED: CLI_INVALID: "), err)
        self.assertIn("hint: fix COLAB_RUNNER_CLI", err)
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "session_open.json").write_text('{"session": "s1"}')
            code, _, err = cli(["stop", "--out-dir", tmp], env={"COLAB_RUNNER_CLI": "'unclosed"})
        self.assertEqual(code, 1)
        self.assertIn("REFUSED: CLI_INVALID: ", err)

    def test_an_out_dir_that_cannot_be_created_is_out_dir_invalid(self):
        with tempfile.TemporaryDirectory() as tmp:
            blocker = Path(tmp) / "a_file"
            blocker.write_text("not a folder")
            colab = FakeColab()
            code, out, err = cli(["run"] + BASE + ["--out-dir", str(blocker / "out")], colab)
        self.assertEqual((code, out, colab.calls), (1, "", []))
        self.assertTrue(err.startswith("REFUSED: OUT_DIR_INVALID: "), err)
        self.assertIn("cannot be created or listed", err)
        self.assertEqual(CODES["OUT_DIR_INVALID"].retryable, False)

    def test_a_binary_refusal_whose_stop_failed_prints_the_stop_hint(self):
        t = fake_tokens()
        for stops, want_hint in (([1, 1, 1], True), ([], False)):
            with self.subTest(stops=stops), tempfile.TemporaryDirectory() as tmp:
                colab = FakeColab(vm_poll_file=rec(rc=0), run_log=poisoned(t), stops=stops)
                code, out, err = cli(["run"] + BASE + ["--out-dir", str(Path(tmp) / "o"), "--resident"], colab)
                self.assertEqual(code, 2)
                self.assertIn("RUN_ERROR: BINARY_DOWNLOAD_TOKEN: ", err)
                self.assertEqual((Path(tmp) / "o" / "session_open.json").is_file(), want_hint)
                self.assertEqual("hint: run `python -m colab_runner stop --out-dir D` again" in err, want_hint)


class ReviewCsTest(unittest.TestCase):
    def test_an_early_resident_trip_whose_kill_cell_failed_collects_no_run_log(self):
        def kill_fails(kind, fake):
            if kind == "kill":
                raise OSError("the kill cell could not be sent")

        for hook, want_log in ((kill_fails, False), (None, True)):  # CR_KILLED proves the job is this run's
            with self.subTest(kill="fails" if hook else "CR_KILLED"), tempfile.TemporaryDirectory() as tmp:
                colab = FakeColab(vm_poll_file=None, run_log="whose log is this\n", on_call=hook)
                log: list[str] = []
                runner, _ = make(colab, tmp, log=log.append)
                res = runner.run(script="/c/x.sh", wall_s=100, resident=True, setup_stall_s=600)
                self.assertEqual((res.reason, res.polls_ok), ("wall", 0))
                self.assertEqual((runner.out_dir / "job.run.out").exists(), want_log)
                self.assertEqual(any("never shown to be this run's" in ln for ln in log), not want_log)
                self.assertEqual(colab.stops, [("sess", 0)])

    def test_byte_twins_refuse_reluctant_quantifiers_and_non_ascii_literals(self):
        for src in (r"[^a]+?", r"[^\s]*?", "é+", "aé", "x[^a]+?"):
            with self.subTest(src=src), self.assertRaises(ValueError):
                _byte_twin_source(src)
        self.assertEqual(_byte_twin_source(r"[^a]+"), rb"[^a]+")
        self.assertEqual(_byte_twin_source("ab+"), b"ab+")

    def test_a_prior_end_with_no_detail_reads_cleanly(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, _ = make(FakeColab(), tmp)
            runner._res = RunResult("done")
            runner._download_refusal = BinaryDownloadRefused([{"path": "q", "source": "s", "pattern": "hf_"}])
            runner._end_on_download_refusal()
            self.assertTrue(runner._res.detail.endswith("(the run had ended done)"), runner._res.detail)
            self.assertNotIn("done: )", runner._res.detail)

    def test_runner_start_with_a_none_run_id_gets_a_fresh_one(self):
        with tempfile.TemporaryDirectory() as tmp:
            colab = FakeColab()
            runner, clock = make(colab, tmp)
            runner._wall_s, runner._t0 = 600.0, clock.now()
            self.assertTrue(runner.start({"CR_RUN_ID": None, "CR_SCRIPT": "/c/x.sh"})[0])
            sent = colab.envs[-1]["CR_RUN_ID"]
            self.assertNotEqual(sent, "None")
            self.assertEqual((len(sent), runner._run_id), (32, sent))


if __name__ == "__main__":
    unittest.main()

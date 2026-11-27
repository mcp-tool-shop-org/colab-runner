"""Item 7: tokens. Every CLI out/err is scrubbed (launch_guards.scrub_colab_tokens) before it is logged, stored or
raised; every downloaded file is scrubbed in place first; secrets reach the VM inside the cell file, never on a
command line. The fake tokens are built at run time: no token is a literal in this repo."""

import inspect
import json
import os
import re
import string
import sys
import tempfile
import textwrap
import unittest
from pathlib import Path

from colab_runner.colab_cli import CliResult, Colab
from colab_runner.runner import cell_source
from colab_runner.window_lib import launch_guards as lg
from tests.fakes import FakeColab, VmLog, make, rec

POSIX = os.name == "posix"


def fake_tokens() -> dict:
    """One value per kind the library scrubs, made here at run time."""
    alnum = string.ascii_letters + string.digits

    def pick(seed, n):
        return "".join(alnum[(seed * 7 + i * 13) % len(alnum)] for i in range(n))

    return {"hf": "hf_" + pick(1, 30), "proxy": pick(2, 40), "bearer": pick(3, 32), "access": pick(4, 28)}


def leak_lines(t: dict) -> list[str]:
    """Lines that carry every token the way a CLI or a job would print it."""
    return [
        "loading with HF_TOKEN=%s" % t["hf"],
        "RuntimeError: https://x/y?colab-runtime-proxy-token=%s&z=1" % t["proxy"],
        "Authorization: Bearer %s" % t["bearer"],
        "callback?access_token=%s" % t["access"],
    ]


def values(t: dict) -> list[str]:
    return [t["hf"][3:], t["proxy"], t["bearer"], t["access"]]  # what must never be seen (hf_ itself is fine)


class RecordingColab(Colab):
    """The real wrapper, with its process call replaced by a recorder that also reads the cell file it was given."""

    def __init__(self):
        super().__init__(["colab"], env={})
        self.argvs, self.cells = [], []

    def run(self, args, timeout):
        args = list(args)
        self.argvs.append(args)
        if "-f" in args:
            cell = Path(args[args.index("-f") + 1])
            self.cells.append((cell.read_text(encoding="utf-8"), cell.stat().st_mode & 0o777))
        return CliResult(0, "")


class CommandLineTest(unittest.TestCase):
    def test_no_secret_reaches_an_argv_the_cli_sees(self):
        t = fake_tokens()
        colab = RecordingColab()
        colab.exec_cell("s", "print(1)", {"HF_TOKEN": t["hf"], "CR_JOB": "job"}, timeout=60)
        argv = " ".join(colab.argvs[0])
        self.assertNotIn("--env", argv)
        for v in values(t):
            self.assertNotIn(v, argv)
        text, mode = colab.cells[0]
        self.assertIn(t["hf"], text)  # the value travels inside the cell file instead
        if POSIX:
            self.assertEqual(mode, 0o600)  # owner-only

    def test_the_cell_sets_its_environment_before_anything_else_runs(self):
        t = fake_tokens()
        colab = RecordingColab()
        colab.exec_cell("s", cell_source("start"), {"CR_JOB": "j7", "HF_TOKEN": t["hf"]}, timeout=60)
        text, _ = colab.cells[0]
        ns = {}
        exec(compile(text.split("import re as _cr_re")[0], "preamble", "exec"), ns)  # only the env preamble
        self.assertEqual((os.environ.pop("CR_JOB"), os.environ.pop("HF_TOKEN")), ("j7", t["hf"]))

    def test_env_name_alone_takes_the_value_from_the_environment_not_the_command_line(self):
        import contextlib
        import io
        from unittest import mock

        from colab_runner.__main__ import main

        t = fake_tokens()
        with tempfile.TemporaryDirectory() as tmp:
            colab = FakeColab(polls=lambda n, env: rec(rc=0))
            argv = ["run", "--session", "s", "--out-dir", str(Path(tmp) / "out"), "--script", "/c/x.sh", "--wall-s",
                    "3600", "--max-rate", "1", "--cap", "5", "--cleanup-bound-s", "600", "--env", "CR_TEST_SECRET",
                    "--env", "PLAIN=1"]
            with mock.patch.dict(os.environ, {"CR_TEST_SECRET": t["hf"]}), \
                    mock.patch("colab_runner.__main__.cli_from_env", return_value=colab), \
                    contextlib.redirect_stderr(io.StringIO()), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(main(argv), 0)
        self.assertFalse(any(t["hf"] in a for a in argv))
        start_env = colab.envs[0]
        self.assertEqual((start_env["CR_TEST_SECRET"], start_env["PLAIN"]), (t["hf"], "1"))

    def test_a_background_cell_keeps_secrets_off_its_command_line_too(self):
        t = fake_tokens()
        with tempfile.TemporaryDirectory() as tmp:
            record = Path(tmp) / "argv.json"
            cli = Path(tmp) / "cli.py"
            cli.write_text(textwrap.dedent("""
                import json, os, sys
                cell = sys.argv[sys.argv.index("-f") + 1]
                json.dump({"argv": sys.argv, "cell": open(cell).read(), "mode": os.stat(cell).st_mode & 0o777},
                          open(sys.argv[1], "w"))
            """))
            call = Colab([sys.executable, str(cli), str(record)]).start_cell("s", "print(1)", {"HF_TOKEN": t["hf"]}, 60)
            if not _wait(call):
                call.kill()
            got = json.loads(record.read_text())
        self.assertNotIn("--env", got["argv"])
        self.assertFalse(any(t["hf"] in a for a in got["argv"]))
        self.assertIn(t["hf"], got["cell"])
        if POSIX:
            self.assertEqual(got["mode"], 0o600)


def _wait(call, secs=20.0):
    import time

    end = time.monotonic() + secs
    while call.poll() is None and time.monotonic() < end:
        time.sleep(0.05)
    return call.result is not None


class WrapperScrubTest(unittest.TestCase):
    def test_every_cli_out_and_err_is_scrubbed(self):
        t = fake_tokens()
        code = "import sys\nfor ln in %r:\n    print(ln)\n    print(ln, file=sys.stderr)\nsys.exit(3)\n" % (leak_lines(t),)
        res = Colab([sys.executable, "-c", code]).run([], 30)
        self.assertEqual(res.rc, 3)
        for v in values(t):
            self.assertNotIn(v, res.out)
            self.assertNotIn(v, res.err)
        self.assertIn("[REDACTED]", res.out)

    def test_a_downloaded_file_is_scrubbed_in_place_before_the_call_returns(self):
        t = fake_tokens()
        with tempfile.TemporaryDirectory() as tmp:
            cli = Path(tmp) / "cli.py"
            cli.write_text("import sys\nopen(sys.argv[-1], 'w').write(%r)\n" % ("\n".join(leak_lines(t)) + "\n",))
            local = Path(tmp) / "got.log"
            res = Colab([sys.executable, str(cli)]).download("s", "/content/x.log", str(local), 30)
            self.assertTrue(res.ok)
            text = local.read_text()
        for v in values(t):
            self.assertNotIn(v, text)
        self.assertEqual(text.count("[REDACTED]"), 4)

    def test_the_vm_cells_scrub_with_the_library_itself(self):
        source = cell_source("poll")
        self.assertIn(inspect.getsource(lg.scrub_colab_tokens), source)
        for pattern, _ in lg.TOKEN_SCRUB_PATTERNS:
            self.assertIn(repr(pattern.pattern), source)
        t = fake_tokens()
        with tempfile.TemporaryDirectory() as tmp:
            vm = VmLog(tmp)
            vm.write(("\n".join(leak_lines(t)) + "\n").encode())
            _, lines = vm.poll(0)
        joined = "\n".join(lines)
        for v in values(t):
            self.assertNotIn(v, joined)


class TokenLeakingColab(FakeColab):
    """A fake whose CLI errors, poll records, job log and downloads all carry tokens."""

    def __init__(self, t, **kw):
        super().__init__(**kw)
        self.t = t
        self.log_lines = leak_lines(t)

    def _fail(self, timeout, err="RuntimeError: Connection was lost"):
        return super()._fail(timeout, err + " " + " ".join(leak_lines(self.t)))

    def stop(self, session, timeout=120.0):
        res = super().stop(session, timeout)
        return res if res.ok else CliResult(res.rc, "", "stop failed: " + " ".join(leak_lines(self.t)))


class RunnerScrubTest(unittest.TestCase):
    def assert_clean(self, out_dir: Path, log: list[str], t: dict):
        files = [p for p in out_dir.rglob("*") if p.is_file()]
        self.assertGreaterEqual(len(files), 2)  # runner_result.json and stop_confirmed.json at least
        for p in files:
            text = p.read_text(encoding="utf-8", errors="replace")
            for v in values(t):
                self.assertNotIn(v, text, "a token in %s" % p.name)
        for v in values(t):
            self.assertNotIn(v, "\n".join(log))

    def test_nothing_under_out_dir_or_in_the_log_carries_a_token(self):
        t = fake_tokens()
        step = "Bearer %s" % t["bearer"]

        def polls(n, env):
            if n == 2:
                return None  # a lost poll: its error carries tokens
            return rec(rc=1 if n >= 4 else None, phase="run " + t["hf"], step=step, progress=n)

        with tempfile.TemporaryDirectory() as tmp:
            colab = TokenLeakingColab(t, polls=polls, starts=["lose_reply"], stops=[1, 0])
            log = []
            runner, _ = make(colab, tmp, log=log.append)
            res = runner.run(script="/content/x.sh", wall_s=3600, env={"HF_TOKEN": t["hf"]},
                             salvage=lambda why: True)
            self.assertEqual(res.reason, "failed")
            self.assertTrue((runner.out_dir / "job.run.out").is_file())
            self.assertTrue((runner.out_dir / "stop_confirmed.json").is_file())
            self.assertIn("[REDACTED]", (runner.out_dir / "runner_result.json").read_text())
            self.assert_clean(runner.out_dir, log, t)

    def test_a_failed_stop_record_carries_no_token(self):
        t = fake_tokens()
        with tempfile.TemporaryDirectory() as tmp:
            colab = TokenLeakingColab(t, polls=lambda n, env: rec(rc=0), stops=[1, 1, 1])
            log = []
            runner, _ = make(colab, tmp, log=log.append)
            self.assertFalse(runner.run(script="/content/x.sh", wall_s=3600).stopped)
            pending = (runner.out_dir / "stop_pending.json").read_text()
            self.assertIn("[REDACTED]", pending)
            self.assert_clean(runner.out_dir, log, t)

    def test_resident_downloads_are_scrubbed_before_they_are_read(self):
        t = fake_tokens()
        with tempfile.TemporaryDirectory() as tmp:
            colab = TokenLeakingColab(t, vm_poll_file=rec(rc=0, step="Bearer %s" % t["bearer"]),
                                      run_log="\n".join(leak_lines(t)) + "\n")
            log = []
            runner, _ = make(colab, tmp, log=log.append)
            res = runner.run(script="/content/x.sh", wall_s=3600, resident=True)
            self.assertEqual(res.reason, "done")
            self.assertIn("[REDACTED]", res.last["step"])
            self.assert_clean(runner.out_dir, log, t)

    def test_an_exception_with_a_token_is_scrubbed_where_it_is_kept(self):
        t = fake_tokens()

        def boom(kind, fake):
            if kind == "poll" and fake.poll_count == 1:
                raise RuntimeError("failed at " + leak_lines(t)[1])

        with tempfile.TemporaryDirectory() as tmp:
            colab = FakeColab(on_call=boom)
            log = []
            runner, _ = make(colab, tmp, log=log.append)
            with self.assertRaises(RuntimeError):
                runner.run(script="/content/x.sh", wall_s=3600)
            self.assertIn("[REDACTED]", runner._res.detail)
            self.assert_clean(runner.out_dir, log, t)

    def test_the_cli_shows_a_crash_scrubbed_on_stderr(self):
        import contextlib
        import io
        from unittest import mock

        from colab_runner.__main__ import main

        t = fake_tokens()

        def boom(kind, fake):
            if kind == "poll":
                raise RuntimeError("failed at " + leak_lines(t)[1])

        with tempfile.TemporaryDirectory() as tmp:
            colab = FakeColab(on_call=boom)
            err, out = io.StringIO(), io.StringIO()
            with mock.patch("colab_runner.__main__.cli_from_env", return_value=colab), \
                    contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
                code = main(["run", "--session", "s", "--out-dir", str(Path(tmp) / "out"), "--script", "/c/x.sh",
                             "--wall-s", "3600", "--max-rate", "1", "--cap", "5", "--cleanup-bound-s", "600"])
            self.assertEqual(code, 2)
            self.assertIn("RUN_ERROR: UNEXPECTED_ERROR: RuntimeError", err.getvalue())
            for v in values(t):
                self.assertNotIn(v, err.getvalue() + out.getvalue())
            self.assertEqual(colab.confirmed_stops(), ["s"])


PARTIAL_CLI = textwrap.dedent("""
    import sys, time
    mode, text = sys.argv[1], sys.argv[2]
    local = sys.argv[-1]
    with open(local, "w") as fh:  # the first bytes of the transfer; then the CLI never finishes it
        fh.write(text)
    if mode == "hang":
        time.sleep(60)
    sys.exit(1)
""")


class PartialDownloadTest(unittest.TestCase):
    """Fix 2, item 1: a download that times out, fails or is interrupted after writing partial bytes leaves no
    token at `local`, in the wrapper and in the runner."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.t = fake_tokens()
        self.partial = "\n".join(leak_lines(self.t))  # no final newline: cut mid-transfer
        self.cli = Path(self._tmp.name) / "partial_cli.py"
        self.cli.write_text(PARTIAL_CLI)
        self.local = Path(self._tmp.name) / "got.run.out"

    def assert_no_token(self):
        text = self.local.read_text(encoding="utf-8") if self.local.exists() else ""
        for v in values(self.t):
            self.assertNotIn(v, text)

    def test_a_timed_out_download_is_scrubbed(self):
        res = Colab([sys.executable, str(self.cli), "hang", self.partial]).download("s", "/content/x", str(self.local), 1.5)
        self.assertEqual(res.rc, 124)
        self.assertIn("[REDACTED]", self.local.read_text())
        self.assert_no_token()

    def test_a_failed_download_is_scrubbed(self):
        res = Colab([sys.executable, str(self.cli), "fail", self.partial]).download("s", "/content/x", str(self.local), 30)
        self.assertEqual(res.rc, 1)
        self.assertIn("[REDACTED]", self.local.read_text())
        self.assert_no_token()

    def test_an_interrupted_download_is_scrubbed(self):
        colab = Colab(["colab"])

        def interrupted(args, timeout):
            Path(args[-1]).write_text(self.partial)
            raise KeyboardInterrupt

        colab.run = interrupted
        with self.assertRaises(KeyboardInterrupt):
            colab.download("s", "/content/x", str(self.local), 30)
        self.assertIn("[REDACTED]", self.local.read_text())
        self.assert_no_token()

    def test_a_partial_file_that_cannot_be_scrubbed_is_removed(self):
        from unittest import mock

        from colab_runner import colab_cli

        self.local.write_text(self.partial)
        with mock.patch.object(Path, "write_text", side_effect=PermissionError):
            colab_cli.scrub_local(self.local)
        self.assertFalse(self.local.exists())

    def test_the_runner_scrubs_a_partial_download_from_any_colab(self):
        partial = self.partial

        class PartialColab(FakeColab):
            mode = "fail"

            def download(self, session, remote, local, timeout=120.0):
                Path(local).write_text(partial)
                if self.mode == "raise":
                    raise KeyboardInterrupt
                return CliResult(124 if self.mode == "timeout" else 1, "", "timed out mid-transfer")

        for mode in ("timeout", "fail", "raise"):
            with self.subTest(mode=mode):
                colab = PartialColab()
                colab.mode = mode
                runner, _ = make(colab, self._tmp.name)
                self.local = runner.out_dir / "job.run.out"
                if mode == "raise":
                    with self.assertRaises(KeyboardInterrupt):
                        runner._download("/content/job.run.out", self.local, 10)
                else:
                    self.assertFalse(runner._download("/content/job.run.out", self.local, 10).ok)
                self.assertTrue(self.local.exists())
                self.assert_no_token()

    def test_a_resident_run_whose_log_download_breaks_keeps_no_token(self):
        partial = self.partial

        class BreakingLogColab(FakeColab):
            def download(self, session, remote, local, timeout=120.0):
                if remote.endswith(".run.out"):
                    self._record("download", session, timeout)
                    Path(local).write_text(partial)
                    return self._fail(timeout, "timed out mid-transfer")
                return super().download(session, remote, local, timeout)

        colab = BreakingLogColab(vm_poll_file=rec(rc=0))
        runner, _ = make(colab, self._tmp.name)
        res = runner.run(script="/content/x.sh", wall_s=3600, resident=True)
        self.assertEqual(res.reason, "done")
        self.local = runner.out_dir / "job.run.out"
        self.assertTrue(self.local.exists())
        self.assert_no_token()


class CliStderrScrubTest(unittest.TestCase):
    """Fix 2, item 2: every refusal and error line the CLI writes is scrubbed; a token is fed through each path."""

    def call(self, args):
        import contextlib
        import io
        from unittest import mock

        from colab_runner.__main__ import main

        colab = FakeColab(polls=lambda n, env: rec(rc=0))
        err, out = io.StringIO(), io.StringIO()
        with mock.patch("colab_runner.__main__.cli_from_env", return_value=colab), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            try:
                code = main(args)
            except SystemExit as exc:  # argparse errors exit
                code = exc.code
        return code, err.getvalue() + out.getvalue(), colab

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.t = fake_tokens()
        self.base = ["run", "--session", "s", "--out-dir", str(Path(self._tmp.name) / "out"), "--script", "/c/x.sh",
                     "--wall-s", "3600", "--max-rate", "1", "--cap", "5", "--cleanup-bound-s", "600"]

    def assert_clean(self, text):
        for v in values(self.t):
            self.assertNotIn(v, text)

    def test_env_unset_with_a_token_for_a_name(self):
        code, text, colab = self.call(self.base + ["--env", self.t["hf"]])  # a token where a NAME should be
        self.assertEqual((code, colab.calls), (1, []))
        self.assertIn("REFUSED: ENV_UNSET", text)
        self.assertIn("hf_[REDACTED]", text)
        self.assert_clean(text)

    def test_unknown_arguments_with_a_token(self):
        code, text, colab = self.call(["rn", "--env", "K=" + self.t["hf"], "--note", leak_lines(self.t)[1]])
        self.assertEqual((code, colab.calls), (1, []))
        self.assertIn("unknown arguments", text)
        self.assert_clean(text)

    def test_an_argparse_error_echoing_a_token(self):
        code, text, colab = self.call(self.base[:-6] + ["--max-rate", self.t["hf"], "--cap", "5",
                                                        "--cleanup-bound-s", "600"])
        self.assertEqual((code, colab.calls), (1, []))
        self.assertIn("invalid float value", text)
        self.assert_clean(text)

    def test_a_refusal_whose_why_carries_a_token(self):
        from unittest import mock

        from colab_runner.runner import Refused

        with mock.patch("colab_runner.__main__.kit_env", side_effect=Refused("ENV_INVALID", leak_lines(self.t)[2])):
            code, text, _ = self.call(self.base)
        self.assertEqual(code, 1)
        self.assertIn("Bearer [REDACTED]", text)
        self.assert_clean(text)

    def test_stop_with_a_token_in_its_out_dir_path(self):
        out_dir = Path(self._tmp.name) / self.t["hf"]
        out_dir.mkdir()
        (out_dir / "session_open.json").write_text("{}")
        code, text, colab = self.call(["stop", "--out-dir", str(out_dir)])
        self.assertEqual((code, colab.calls), (1, []))
        self.assertIn("SESSION_MARKER_INVALID", text)
        self.assert_clean(text)


class BinaryDownloadScrubTest(unittest.TestCase):
    """Follow-up item 1: content-based binary-safe download scrub, quarantine outside out_dir, and runner refusal."""

    PNG_HEAD = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR\x00\x00\x00\x01\xff\xd8\xfe"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.t = fake_tokens()

    def test_each_byte_pattern_matches_its_text_pattern_on_ascii_input(self):
        from colab_runner.colab_cli import BYTE_TOKEN_PATTERNS

        self.assertEqual(len(BYTE_TOKEN_PATTERNS), len(lg.TOKEN_SCRUB_PATTERNS))
        samples = [
            "https://x/y?colab-runtime-proxy-token=" + self.t["proxy"] + "&z=1",
            "callback?access_token=" + self.t["access"] + "&ok=1",
            "Authorization: Bearer " + self.t["bearer"],
            "token=" + self.t["hf"] + " done",
            "clean ascii line with hf_short and bearer without value",
            "plain text nothing secret here 12345",
        ]
        for (name, bpat), (tpat, _) in zip(BYTE_TOKEN_PATTERNS, lg.TOKEN_SCRUB_PATTERNS, strict=True):
            for sample in samples:
                with self.subTest(pattern=name, sample=sample):
                    tm = tpat.search(sample)
                    bm = bpat.search(sample.encode("ascii"))
                    self.assertEqual(bm is not None, tm is not None)
                    if tm is not None and bm is not None:
                        self.assertEqual(bm.group(0).decode("ascii"), tm.group(0))

    def test_the_twins_know_every_str_space_in_the_code_space(self):
        from colab_runner import colab_cli

        spaces = [chr(c) for c in range(sys.maxunicode + 1) if re.fullmatch(r"\s", chr(c))]
        self.assertEqual(colab_cli._STR_SPACES, spaces)  # none above U+3000 was missed
        for ch in "\x1c\x1d\x1e\x1f\x85\xa0   　":
            self.assertIn(ch, spaces)

    def test_every_byte_twin_matches_exactly_what_its_text_pattern_matches(self):
        """PR 3 fix 1, (B): over a generated corpus with every str space (\\x1c-\\x1f, NEL, NBSP, EM SPACE and the
        rest) and look-alikes that are not spaces, each byte twin finds the same matches, at the same places, as its
        text pattern on the decoded text. The one difference is `\\b` next to a non-ASCII word character, where the
        byte twin of `hf_` matches and the text pattern does not: a superset, never a miss."""
        import itertools

        from colab_runner import colab_cli

        tok = self.t["bearer"][:24]
        spaces = list(colab_cli._STR_SPACES)
        seps = [""] + spaces + ["\x1c ", "  ", "　　", "​", "﻿", "-", "é", "&"]
        prefixes = ["", "x", " ", "é", "Authorization: "]
        heads = ["Bearer", "colab-runtime-proxy-token=", "access_token=", "hf_", "Bearer Bearer"]
        bodies = [tok, tok[:5], "a.b-c_d", "tokén" + tok, tok + " " + tok, tok + "é", " " + tok, ""]
        suffixes = ["", " end", "\x1f", "&x=1", "é", "\u0085"]
        word_non_ascii = re.compile(r"[^\x00-\x7f]")
        cases = exact = 0
        for pre, head, sep, body, suf in itertools.product(prefixes, heads, seps, bodies, suffixes):
            s = pre + head + sep + body + suf
            b = s.encode("utf-8")
            for (name, bpat), (tpat, _) in zip(colab_cli.BYTE_TOKEN_PATTERNS, lg.TOKEN_SCRUB_PATTERNS, strict=True):
                text = [(len(s[:m.start()].encode()), len(s[:m.end()].encode())) for m in tpat.finditer(s)]
                byte = [(m.start(), m.end()) for m in bpat.finditer(b)]
                cases += 1
                if name == "hf_" and any(re.match(r"\w", c) for c in word_non_ascii.findall(s)):
                    self.assertTrue(set(text) <= set(byte), (name, s))
                else:
                    exact += 1
                    self.assertEqual(byte, text, (name, s))
        self.assertGreater(exact, 30000)
        self.assertGreater(cases, exact)

    def test_the_review_cases_are_caught_in_a_binary(self):
        from colab_runner.colab_cli import binary_token_hit

        tok = self.t["bearer"].encode("ascii")
        for sep in (b" ", b"\x1c", b"\x1f", b"\t", " ".encode(), "\u0085".encode(), " ".encode(),
                    "　".encode(), " ".encode(), b" \xc2\xa0 "):
            with self.subTest(sep=sep):
                data = self.PNG_HEAD + b"Authorization: Bearer" + sep + tok + b"\x00"
                self.assertEqual(binary_token_hit(data), "Bearer")
                text = ("Authorization: Bearer" + sep.decode("utf-8") + self.t["bearer"])
                self.assertNotIn(self.t["bearer"], lg.scrub_colab_tokens(text))  # the text path catches it too

    def test_content_decides_binary_never_extension(self):
        from colab_runner.colab_cli import is_binary_bytes, scrub_local

        self.assertFalse(is_binary_bytes("plain utf8 café\n".encode("utf-8")))
        self.assertTrue(is_binary_bytes(b"abc\x00def"))
        self.assertTrue(is_binary_bytes(b"\x80\xff\xfeinvalid"))
        # A .png containing valid UTF-8 text without NUL is scrubbed as text.
        fake_png = self.root / "actually_text.png"
        fake_png.write_text("HF_TOKEN=" + self.t["hf"] + "\n", encoding="utf-8")
        scrub_local(fake_png)
        self.assertEqual(fake_png.read_text(encoding="utf-8"), "HF_TOKEN=hf_[REDACTED]\n")
        # A .txt containing a NUL byte without tokens is treated as binary and left byte-identical.
        fake_txt = self.root / "actually_binary.txt"
        raw = b"row1\x00row2\r\n"
        fake_txt.write_bytes(raw)
        scrub_local(fake_txt)
        self.assertEqual(fake_txt.read_bytes(), raw)

    def test_clean_png_nul_byte_and_invalid_utf8_are_byte_identical(self):
        from colab_runner.colab_cli import scrub_local

        cases = {
            "clean.png": self.PNG_HEAD + b"\x00\x01\x02IEND\xaeB`\x82",
            "has_nul.bin": b"header\x00payload\xff\xfe\r\n",
            "bad_utf8.dat": b"\x80\x81\xfe\xffnot-utf8-at-all\r\n",
        }
        for name, raw in cases.items():
            with self.subTest(name=name):
                p = self.root / name
                p.write_bytes(raw)
                res = Colab([sys.executable, "-c", "import sys; sys.exit(0)"]).download(
                    "s", "/content/" + name, str(p), 30,
                )
                self.assertTrue(res.ok)
                self.assertEqual(p.read_bytes(), raw)
                scrub_local(p)
                self.assertEqual(p.read_bytes(), raw)

    def test_png_with_token_is_refused_and_quarantined_unchanged(self):
        from colab_runner.colab_cli import BinaryDownloadRefused

        raw = self.PNG_HEAD + b"metadata=" + self.t["hf"].encode("ascii") + b"\x00IEND\xaeB`\x82"
        cli = self.root / "dl.py"
        cli.write_text("import pathlib, sys\npathlib.Path(sys.argv[-1]).write_bytes(%r)\n" % (raw,))
        out_dir = self.root / "out"
        out_dir.mkdir()
        target = out_dir / "render.png"
        qdir = out_dir.parent / "out.quarantine"
        logs: list[str] = []
        colab = Colab([sys.executable, str(cli)], quarantine_dir=qdir, log=logs.append)
        colab._base_dir = out_dir
        with self.assertRaises(BinaryDownloadRefused) as cm:
            colab.download("s", "/content/render.png", str(target), 30)
        self.assertEqual(cm.exception.code, "BINARY_DOWNLOAD_TOKEN")
        self.assertFalse(target.exists())
        self.assertEqual(list(out_dir.rglob("*")), [])
        qfile = qdir / "render.png"
        self.assertTrue(qfile.is_file())
        self.assertEqual(qfile.read_bytes(), raw)
        self.assertEqual(len(cm.exception.quarantined), 1)
        self.assertEqual(cm.exception.quarantined[0]["pattern"], "hf_")
        self.assertTrue(any("hf_" in ln for ln in logs))
        for v in values(self.t):
            self.assertNotIn(v, "\n".join(logs))
            self.assertNotIn(v, str(cm.exception))

    def test_partial_binary_download_follows_same_rule(self):
        from colab_runner.colab_cli import BinaryDownloadRefused

        clean_partial = self.PNG_HEAD + b"clean-partial\x80\xff"
        poison_partial = self.PNG_HEAD + b"Bearer " + self.t["bearer"].encode("ascii") + b"\x00cut"
        partial_cli = self.root / "partial_bin.py"
        partial_cli.write_text(textwrap.dedent("""
            import pathlib, sys, time
            mode, hex_bytes, local = sys.argv[1], sys.argv[2], sys.argv[-1]
            pathlib.Path(local).write_bytes(bytes.fromhex(hex_bytes))
            if mode == "hang":
                time.sleep(60)
            sys.exit(1)
        """))

        # Clean partial binary on fail / timeout / interrupt stays byte-identical.
        for mode in ("fail", "hang"):
            p = self.root / ("clean_%s.png" % mode)
            res = Colab([sys.executable, str(partial_cli), mode, clean_partial.hex()]).download(
                "s", "/c/a.png", str(p), 1.0 if mode == "hang" else 30.0,
            )
            self.assertFalse(res.ok)
            self.assertEqual(p.read_bytes(), clean_partial)

        # Poisoned partial binary on fail / timeout / interrupt is quarantined and refused.
        for mode in ("fail", "hang", "raise"):
            with self.subTest(mode=mode):
                out_dir = self.root / ("out_%s" % mode)
                out_dir.mkdir()
                p = out_dir / "partial.png"
                qdir = out_dir.parent / (out_dir.name + ".quarantine")
                if mode == "raise":
                    colab = Colab(["colab"], quarantine_dir=qdir)
                    colab._base_dir = out_dir

                    def interrupted(args, timeout):
                        Path(args[-1]).write_bytes(poison_partial)
                        raise KeyboardInterrupt

                    colab.run = interrupted
                else:
                    colab = Colab(
                        [sys.executable, str(partial_cli), mode, poison_partial.hex()],
                        quarantine_dir=qdir,
                    )
                    colab._base_dir = out_dir
                with self.assertRaises(BinaryDownloadRefused) as cm:
                    colab.download("s", "/c/partial.png", str(p), 1.0 if mode == "hang" else 30.0)
                self.assertEqual(cm.exception.quarantined[0]["pattern"], "Bearer")
                self.assertFalse(p.exists())
                self.assertEqual((qdir / "partial.png").read_bytes(), poison_partial)

    def test_directory_download_handles_clean_text_and_poison_binaries_and_collisions(self):
        from colab_runner.colab_cli import BinaryDownloadRefused, scrub_local

        bundle = self.root / "bundle"
        (bundle / "sub").mkdir(parents=True)
        clean_png = self.PNG_HEAD + b"\x00clean"
        bad_utf8 = b"\x80\xffbad-utf8"
        poison_png = self.PNG_HEAD + b"access_token=" + self.t["access"].encode("ascii") + b"&x=1"
        (bundle / "clean.png").write_bytes(clean_png)
        (bundle / "raw.bin").write_bytes(bad_utf8)
        (bundle / "notes.txt").write_text("Bearer %s\n" % self.t["bearer"], encoding="utf-8")
        (bundle / "sub" / "poison.png").write_bytes(poison_png)

        qdir = self.root / "bundle.quarantine"
        with self.assertRaises(BinaryDownloadRefused) as cm:
            scrub_local(bundle, quarantine_dir=qdir)
        self.assertEqual(cm.exception.quarantined[0]["pattern"], "access_token")
        # Clean binaries remain byte-identical and text files are scrubbed even when a sibling is quarantined.
        self.assertEqual((bundle / "clean.png").read_bytes(), clean_png)
        self.assertEqual((bundle / "raw.bin").read_bytes(), bad_utf8)
        self.assertEqual((bundle / "notes.txt").read_text(encoding="utf-8"), "Bearer [REDACTED]\n")
        self.assertFalse((bundle / "sub" / "poison.png").exists())
        self.assertEqual((qdir / "sub" / "poison.png").read_bytes(), poison_png)

        # Collision: additional poisoned files at the same relative path are quarantined without overwriting.
        poison_2 = self.PNG_HEAD + self.t["hf"].encode("ascii") + b"\x00v2"
        poison_3 = self.PNG_HEAD + self.t["hf"].encode("ascii") + b"\x00v3"
        (bundle / "sub" / "poison.png").write_bytes(poison_2)
        with self.assertRaises(BinaryDownloadRefused):
            scrub_local(bundle, quarantine_dir=qdir)
        (bundle / "sub" / "poison.png").write_bytes(poison_3)
        with self.assertRaises(BinaryDownloadRefused):
            scrub_local(bundle, quarantine_dir=qdir)
        self.assertEqual((qdir / "sub" / "poison.png").read_bytes(), poison_png)
        self.assertEqual((qdir / "sub" / "poison.png.1").read_bytes(), poison_2)
        self.assertEqual((qdir / "sub" / "poison.png.2").read_bytes(), poison_3)

    def test_unreadable_file_and_quarantine_move_failure_remove_source(self):
        from unittest import mock

        from colab_runner.colab_cli import BinaryDownloadRefused, scrub_local

        p = self.root / "unreadable.bin"
        p.write_bytes(b"\x00secret")
        with mock.patch.object(Path, "read_bytes", side_effect=PermissionError):
            scrub_local(p)
        self.assertFalse(p.exists())

        p2 = self.root / "cannot_move.png"
        p2.write_bytes(self.PNG_HEAD + self.t["hf"].encode("ascii"))
        with mock.patch("colab_runner.colab_cli.shutil.move", side_effect=PermissionError), \
                mock.patch.object(tempfile, "tempdir", str(self.root)):  # the bare quarantine, made here
            with self.assertRaises(BinaryDownloadRefused) as ctx:
                scrub_local(p2)
        self.assertEqual(len(ctx.exception.failures), 1)
        self.assertIsInstance(ctx.exception.failures[0], PermissionError)
        self.assertEqual(len(ctx.exception.removed), 1)
        self.assertFalse(p2.exists())

    def test_symlinks_are_unlinked_without_following(self):
        from colab_runner.colab_cli import scrub_local

        outside = self.root / "outside.txt"
        outside.write_text("Bearer %s\n" % self.t["bearer"], encoding="utf-8")
        link = self.root / "link.txt"
        try:
            link.symlink_to(outside)
        except OSError:
            self.skipTest("symlinks require privilege on this Windows host")
        scrub_local(link)
        self.assertFalse(link.exists())
        self.assertIn(self.t["bearer"], outside.read_text(encoding="utf-8"))

        folder = self.root / "dir_with_link"
        folder.mkdir()
        (folder / "inner.txt").write_text("Bearer %s\n" % self.t["bearer"], encoding="utf-8")
        (folder / "sym.txt").symlink_to(outside)
        scrub_local(folder)
        self.assertFalse((folder / "sym.txt").exists())
        self.assertEqual((folder / "inner.txt").read_text(encoding="utf-8"), "Bearer [REDACTED]\n")

    def test_runner_quarantine_isolates_out_dir_and_salvage_and_still_stops(self):
        from colab_runner.colab_cli import BinaryDownloadRefused
        from colab_runner.runner import Runner

        poison_png = self.PNG_HEAD + b"colab-runtime-proxy-token=" + self.t["proxy"].encode("ascii") + b"\x00"
        clean_png = self.PNG_HEAD + b"\x00ok"
        run_dir = self.root / "run1"
        run_dir.mkdir()
        out_dir = run_dir / "out"
        vm_files = {
            "/content/clean.png": clean_png,
            "/content/renders/poison.png": poison_png,
        }

        class RenderColab(FakeColab):
            def download(self, session, remote, local, timeout=120.0):
                self._record("download", session, timeout)
                payload = vm_files.get(remote)
                if payload is not None:
                    Path(local).write_bytes(payload)
                    return CliResult(0)
                return super().download(session, remote, local, timeout)

        def polls(n, env):
            return rec(rc=1 if n >= 2 else None)

        colab = RenderColab(polls=polls)
        logs: list[str] = []
        runner = Runner(colab, "sess", out_dir, poll_s=0.05, log=logs.append)
        salvage_saw: list[str] = []

        def salvage(reason: str) -> bool:
            runner.download("/content/clean.png", runner.out_dir / "renders" / "clean.png", 10)
            try:
                runner.download("/content/renders/poison.png", runner.out_dir / "renders" / "poison.png", 10)
            finally:
                # Neither the out dir a lane bundles nor salvage sees any quarantined file.
                salvage_saw.extend(
                    p.relative_to(runner.out_dir).as_posix() for p in runner.out_dir.rglob("*") if p.is_file()
                )
            return True

        with self.assertRaises(BinaryDownloadRefused) as cm:
            runner.run(script="/content/x.sh", wall_s=3600, salvage=salvage)

        self.assertEqual(cm.exception.code, "BINARY_DOWNLOAD_TOKEN")
        self.assertEqual(colab.stops, [("sess", 0)])
        self.assertTrue((out_dir / "stop_confirmed.json").is_file())
        self.assertFalse((out_dir / "session_open.json").exists())
        # Out-dir bundle contents only have clean files and runner metadata, never the quarantined file.
        bundled = [p.relative_to(out_dir).as_posix() for p in out_dir.rglob("*") if p.is_file()]
        self.assertNotIn("renders/poison.png", bundled)
        self.assertNotIn("renders/poison.png", salvage_saw)
        self.assertFalse(any("quarantine" in p for p in bundled))
        self.assertFalse(any("quarantine" in p for p in salvage_saw))
        self.assertEqual((out_dir / "renders" / "clean.png").read_bytes(), clean_png)

        # Quarantine lives as sibling <out-dir>.quarantine outside out_dir with bytes intact.
        qdir = run_dir / "out.quarantine"
        self.assertEqual(runner.quarantine_dir, qdir)
        qfile = qdir / "renders" / "poison.png"
        self.assertTrue(qfile.is_file())
        self.assertEqual(qfile.read_bytes(), poison_png)

        # runner_result.json names the quarantined file with pattern name only, never token bytes.
        result_text = (out_dir / "runner_result.json").read_text(encoding="utf-8")
        result = json.loads(result_text)
        self.assertEqual((result["reason"], result["stopped"]), ("error", True))
        self.assertEqual(len(result["quarantined"]), 1)
        self.assertEqual(result["quarantined"][0]["path"], str(qfile))
        self.assertEqual(result["quarantined"][0]["pattern"], "colab-runtime-proxy-token")
        for v in values(self.t):
            self.assertNotIn(v, result_text)
            self.assertNotIn(v, "\n".join(logs))

    def test_runner_resident_quarantined_log_and_cli_exit_1_still_stop(self):
        import contextlib
        import io
        from unittest import mock

        from colab_runner.__main__ import main

        poison_log = self.PNG_HEAD + b"line=" + self.t["hf"].encode("ascii") + b"\x00end"
        colab = FakeColab(vm_poll_file=rec(rc=0), run_log=poison_log)
        out_dir = self.root / "cli_run" / "out"
        err, out = io.StringIO(), io.StringIO()
        with mock.patch("colab_runner.__main__.cli_from_env", return_value=colab), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            code = main([
                "run", "--session", "s", "--out-dir", str(out_dir), "--script", "/c/x.sh",
                "--wall-s", "3600", "--max-rate", "1", "--cap", "5", "--cleanup-bound-s", "1200", "--resident",
            ])
        self.assertEqual(code, 2)
        self.assertIn("RUN_ERROR: BINARY_DOWNLOAD_TOKEN: binary download quarantined:", err.getvalue())
        self.assertIn("hint: keep tokens out of the job's binary outputs", err.getvalue())
        self.assertEqual(out.getvalue(), "")
        self.assertEqual(colab.stops, [("s", 0)])
        self.assertFalse((out_dir / "job.run.out").exists())
        qfile = out_dir.parent / "out.quarantine" / "job.run.out"
        self.assertEqual(qfile.read_bytes(), poison_log)
        result = json.loads((out_dir / "runner_result.json").read_text(encoding="utf-8"))
        self.assertEqual((result["reason"], result["stopped"]), ("error", True))
        self.assertEqual(result["quarantined"][0]["pattern"], "hf_")
        for v in values(self.t):
            self.assertNotIn(v, err.getvalue())
            self.assertNotIn(v, json.dumps(result))

        # When resident log collection runs in _end (no time left inside loop), a poisoned log still turns into error + stop.
        from colab_runner.colab_cli import BinaryDownloadRefused
        from colab_runner.runner import Runner
        from tests.fakes import FakeClock

        clock = FakeClock()

        def advance_on_poll_download(kind, fake, clk=clock):
            if kind == "download" and fake.poll_file_count == 1:
                clk.advance(1.0)

        end_colab = FakeColab(vm_poll_file=rec(rc=0), run_log=poison_log, clock=clock, on_call=advance_on_poll_download)
        end_out = self.root / "end_run" / "out"
        runner = Runner(end_colab, "s", end_out, clock=clock.now, sleep=clock.sleep)
        with self.assertRaises(BinaryDownloadRefused):
            runner.run(script="/c/x.sh", wall_s=1.5, resident=True)
        self.assertEqual(end_colab.stops, [("s", 0)])
        end_res = json.loads((end_out / "runner_result.json").read_text(encoding="utf-8"))
        self.assertEqual((end_res["reason"], end_res["stopped"]), ("error", True))
        self.assertEqual(end_res["quarantined"][0]["pattern"], "hf_")

    def test_runner_download_with_real_colab_uses_sibling_quarantine(self):
        from colab_runner.colab_cli import BinaryDownloadRefused
        from colab_runner.runner import Runner

        poison_png = self.PNG_HEAD + b"token=" + self.t["hf"].encode("ascii") + b"\x00"
        cli = self.root / "real_dl.py"
        cli.write_text("import pathlib, sys\npathlib.Path(sys.argv[-1]).write_bytes(%r)\n" % (poison_png,))
        out_dir = self.root / "with_colab" / "out"
        colab = Colab([sys.executable, str(cli)])
        runner = Runner(colab, "s", out_dir)
        with self.assertRaises(BinaryDownloadRefused):
            runner.download("/c/render.png", out_dir / "render.png", 30)
        qfile = out_dir.parent / "out.quarantine" / "render.png"
        self.assertEqual(qfile.read_bytes(), poison_png)
        with self.assertRaises(BinaryDownloadRefused):
            runner.download("/c/ext.png", self.root / "outside_out.png", 30)
        self.assertEqual((out_dir.parent / "out.quarantine" / "outside_out.png").read_bytes(), poison_png)
        self.assertEqual(len(runner._res.quarantined), 2)
        self.assertIsNone(colab.quarantine_dir)  # unbound cleanly after the call


def png(*texts: tuple[bytes, bytes]) -> bytes:
    """A real 1x1 grey PNG with tEXt chunks (keyword, text), as ComfyUI writes its prompt and workflow."""
    import struct
    import zlib

    def chunk(kind: bytes, data: bytes) -> bytes:
        return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data) & 0xFFFFFFFF)

    ihdr = struct.pack(">IIBBBBB", 1, 1, 8, 0, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr)
            + b"".join(chunk(b"tEXt", key + b"\x00" + text) for key, text in texts)
            + chunk(b"IDAT", zlib.compress(b"\x00\x80")) + chunk(b"IEND", b""))


class RenderMetadataTest(unittest.TestCase):
    """PR 3 fix 1, (B): a render whose prompt says "Bearer" is a good render; a real bearer token in it is not."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.t = fake_tokens()

    @staticmethod
    def prompt(text: str) -> bytes:
        return json.dumps({"6": {"class_type": "CLIPTextEncode", "inputs": {"text": text}}}).encode("utf-8")

    def real_shaped(self) -> list[str]:
        """Bearer values shaped like the credentials a lane can meet, made at run time (none is a literal here)."""
        alnum = string.ascii_letters + string.digits
        body = "".join(alnum[(i * 11 + 5) % len(alnum)] for i in range(120))
        jwt = "eyJ" + body[:33] + "." + "eyJ" + body[40:80] + "." + body[80:120].replace("a", "-")
        return [self.t["bearer"], "ya" + "29." + body, jwt, self.t["hf"], body[:20]]

    def download(self, raw: bytes, out_dir: Path):
        from colab_runner.runner import Runner

        cli = self.root / "dl.py"
        cli.write_text("import pathlib, sys\npathlib.Path(sys.argv[-1]).write_bytes(%r)\n" % (raw,))
        runner = Runner(Colab([sys.executable, str(cli)]), "s", out_dir)
        target = out_dir / "renders" / "r.png"
        return runner, target, lambda: runner.download("/content/r.png", target, 30)

    def test_token_shape(self):
        from colab_runner.colab_cli import BEARER_TOKEN_MIN, token_shaped

        self.assertEqual(BEARER_TOKEN_MIN, 20)
        for word in (b"of", b"Of-The-North", b"highly_detailed_background", b"x" * 64, b"A1b2C3d4E5f6G7h8i9J"):
            self.assertFalse(token_shaped(word), word)
        for tok in self.real_shaped():
            self.assertTrue(token_shaped(tok.encode("ascii")), tok)

    def test_every_real_shaped_token_the_text_pattern_catches_is_caught_in_a_binary(self):
        from colab_runner.colab_cli import binary_token_hit

        for tok in self.real_shaped():
            for sep in (" ", "\t", "\x1f", " ", " "):
                with self.subTest(tok=tok[:6], sep=sep):
                    text = "Authorization: Bearer" + sep + tok
                    self.assertNotIn(tok[3:], lg.scrub_colab_tokens(text))
                    raw = png((b"prompt", self.prompt("a render")), (b"note", text.encode("utf-8")))
                    self.assertIsNotNone(binary_token_hit(raw))

    def test_a_render_whose_prompt_says_bearer_comes_through_byte_identical(self):
        from colab_runner.colab_cli import binary_token_hit, is_binary_bytes

        for i, words in enumerate(("Flame Bearer of the North", "the Ring-Bearer, oil painting", "Bearer of light",
                                   "Standard Bearer highly_detailed_background_masterpiece",
                                   "Bearer Bearer of the Dawn")):
            with self.subTest(words=words):
                # the prompt as ComfyUI writes it (JSON, in tEXt), and as plain UTF-8 text in a chunk of its own
                raw = png((b"prompt", self.prompt(words)), (b"workflow", self.prompt(words + ", 8k")),
                          (b"parameters", words.encode("utf-8")))
                self.assertTrue(is_binary_bytes(raw))
                self.assertIsNone(binary_token_hit(raw))
                runner, target, go = self.download(raw, self.root / ("ok_%d" % i) / "out")
                self.assertTrue(go().ok)
                self.assertEqual(target.read_bytes(), raw)  # byte-identical
                self.assertFalse(runner.quarantine_dir.exists())
                self.assertEqual(runner._res.quarantined, [])

    def test_a_render_with_a_real_bearer_token_is_refused(self):
        from colab_runner.colab_cli import BinaryDownloadRefused

        raw = png((b"prompt", self.prompt("Flame Bearer of the North")),
                  (b"note", ("Authorization: Bearer " + self.t["bearer"]).encode("ascii")))
        out_dir = self.root / "bad" / "out"
        runner, target, go = self.download(raw, out_dir)
        with self.assertRaises(BinaryDownloadRefused) as cm:
            go()
        self.assertEqual(cm.exception.quarantined[0]["pattern"], "Bearer")
        self.assertFalse(target.exists())
        self.assertEqual((runner.quarantine_dir / "renders" / "r.png").read_bytes(), raw)


class BareQuarantineTest(unittest.TestCase):
    """PR 3 fix 1, (B): with no quarantine_dir (a bare `Colab.download` or `scrub_local`), a refused binary goes to a
    new `colab-runner-quarantine-*` folder under the system temp dir, never next to the file in the out dir."""

    def setUp(self):
        from unittest import mock

        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.temp = self.root / "system-temp"
        self.temp.mkdir()
        patcher = mock.patch.object(tempfile, "tempdir", str(self.temp))  # what tempfile.gettempdir() answers
        patcher.start()
        self.addCleanup(patcher.stop)
        self.t = fake_tokens()
        self.poison = BinaryDownloadScrubTest.PNG_HEAD + b"x=" + self.t["hf"].encode("ascii") + b"\x00"

    def assert_outside(self, out_dir: Path, exc):
        q = Path(exc.quarantined[0]["path"])
        self.assertEqual(q.read_bytes(), self.poison)
        self.assertIn(self.temp, q.parents)
        self.assertTrue(q.relative_to(self.temp).parts[0].startswith("colab-runner-quarantine-"))
        self.assertNotIn(out_dir, q.parents)
        self.assertEqual([p for p in out_dir.rglob("*") if p.is_file()], [])
        self.assertFalse(any("quarantine" in p.name for p in out_dir.rglob("*")))

    def test_a_bare_colab_download_quarantines_under_the_system_temp_dir(self):
        from colab_runner.colab_cli import BinaryDownloadRefused

        cli = self.root / "dl.py"
        cli.write_text("import pathlib, sys\npathlib.Path(sys.argv[-1]).write_bytes(%r)\n" % (self.poison,))
        out_dir = self.root / "out"
        (out_dir / "renders").mkdir(parents=True)
        with self.assertRaises(BinaryDownloadRefused) as cm:
            Colab([sys.executable, str(cli)]).download("s", "/content/r.png", str(out_dir / "renders" / "r.png"), 30)
        self.assert_outside(out_dir, cm.exception)
        self.assertEqual(Path(cm.exception.quarantined[0]["path"]).name, "r.png")

    def test_a_bare_scrub_of_a_directory_quarantines_under_the_system_temp_dir(self):
        from colab_runner.colab_cli import BinaryDownloadRefused, scrub_local

        out_dir = self.root / "out"
        (out_dir / "renders" / "sub").mkdir(parents=True)
        (out_dir / "renders" / "sub" / "p.png").write_bytes(self.poison)
        with self.assertRaises(BinaryDownloadRefused) as cm:
            scrub_local(out_dir / "renders")
        self.assert_outside(out_dir, cm.exception)
        self.assertEqual(Path(cm.exception.quarantined[0]["path"]).relative_to(self.temp).parts[1:],
                         ("renders", "sub", "p.png"))

    def test_a_clean_bare_download_makes_no_quarantine(self):
        from colab_runner.colab_cli import scrub_local

        p = self.root / "clean.png"
        p.write_bytes(BinaryDownloadScrubTest.PNG_HEAD + b"\x00clean")
        scrub_local(p)
        self.assertEqual(list(self.temp.iterdir()), [])

    def test_cli_results_keep_local_paths_the_runner_parses(self):
        # CLI output is parsed (e.g. CR_TAR_PART lines with local paths in --local-cpu), so it is
        # token-scrubbed only; home paths are scrubbed where text is logged or written to JSON.
        from colab_runner.colab_cli import scrub, scrub_tokens

        line = "CR_TAR_PART /home/someone/proj/runs/w1/_local_vm_content/out/model.tar.part0000 10 abc"
        self.assertEqual(scrub_tokens(line), line)
        self.assertNotIn("/home/someone", scrub(line))

    def test_scrubbed_state_paths_read_back_as_the_real_home(self):
        # window_meta.json stores home paths as ~; reading them back must never create a literal "~" dir.
        from colab_runner.launcher import _default_open_lock_path, _state_paths

        s_dir, l_file = _state_paths("~/.local/state/colab-runner", None, live=True)
        self.assertEqual(s_dir, Path.home() / ".local" / "state" / "colab-runner")
        self.assertEqual(l_file, s_dir / "study_ledger.json")
        self.assertEqual(_default_open_lock_path("~/x/open.lock"), Path.home() / "x" / "open.lock")

    def test_cli_scrub_strips_home_paths_from_tracebacks_and_keeps_urls_and_content(self):
        from unittest import mock

        from colab_runner.colab_cli import scrub, scrub_home_paths

        self.assertEqual(scrub_home_paths(""), "")
        u_a, u_b = "al" + "ice", "bo" + "b"
        samples = ("/" + f"home/{u_a}", "/" + f"Users/{u_a}", "/" + f"mnt/c/Users/{u_b}", "C:\\Us" + f"ers\\{u_b}")
        tb = (f"│ {samples[0]}/.local/share/uv/tools/cli/session.py:141 │\n  {samples[1]}/venv/bin/colab\n"
              f"  {samples[2]}/AppData/Local/Temp/x\n  {samples[3]}\\AppData\\Local\\file.py\n"
              f"Keep https://host{samples[0]} and /content/lane3_out intact\n")
        cleaned = scrub(tb)
        head_lines = "\n".join(cleaned.splitlines()[:4])
        for leaked in samples:
            self.assertNotIn(leaked, head_lines)
        for want in ("~/.local/share/uv/tools/cli/session.py:141", "~/venv/bin/colab", "~/AppData/Local/Temp/x", f"https://host{samples[0]}", "/content/lane3_out"):
            self.assertIn(want, cleaned)
        with mock.patch.dict(os.environ, {"HOME": "/opt/custom_runner_home", "USERPROFILE": ""}):
            self.assertEqual(scrub("err at /opt/custom_runner_home/bin/colab"), "err at ~/bin/colab")
        with mock.patch.dict(os.environ, {"HOME": tempfile.gettempdir()}):
            self.assertIn(tempfile.gettempdir(), scrub_home_paths(tempfile.gettempdir() + "/keep"))


if __name__ == "__main__":
    unittest.main()

"""Item 7: tokens. Every CLI out/err is scrubbed (launch_guards.scrub_colab_tokens) before it is logged, stored or
raised; every downloaded file is scrubbed in place first; secrets reach the VM inside the cell file, never on a
command line. The fake tokens are built at run time: no token is a literal in this repo."""

import inspect
import json
import os
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
            self.assertEqual(code, 1)
            self.assertIn("RUN_ERROR: RuntimeError", err.getvalue())
            for v in values(t):
                self.assertNotIn(v, err.getvalue() + out.getvalue())
            self.assertEqual(colab.confirmed_stops(), ["s"])


if __name__ == "__main__":
    unittest.main()

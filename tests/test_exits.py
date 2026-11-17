"""Items 1 and 8: every exit stops this run's own session, once, after the kill and the salvage; failures are
failures; `python -m colab_runner stop` is the compensator for a rig process killed outright."""

import contextlib
import io
import json
import os
import signal
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from colab_runner.__main__ import main
from colab_runner.runner import TRIPS
from tests.fakes import FakeColab, guard, make, rec

POSIX = os.name == "posix"


def scenarios():
    """reason -> (FakeColab kwargs, run kwargs). Every end the runner has."""

    def raise_mid_loop(kind, fake):
        if kind == "poll" and fake.poll_count == 2:
            raise RuntimeError("simulated crash mid-loop")

    return {
        "done": ({"polls": lambda n, env: rec(rc=0)}, {}),
        "failed": ({"polls": lambda n, env: rec(rc=1)}, {}),
        "oom": ({"polls": lambda n, env: rec(rc=137)}, {}),
        "wall": ({"polls": lambda n, env: rec(progress=n)}, {"wall_s": 600}),
        # 10/h with 1000 s reserved: ceiling 5 - 2.78 = 2.22 units, reached after 800 s
        "cap": ({"polls": lambda n, env: rec(progress=n)}, {"guard": guard(max_rate=10.0, cap=5.0)}),
        "setup_stall": ({"polls": lambda n, env: rec(phase="setup", setup_bytes=500)}, {}),
        "progress_stall": ({"polls": lambda n, env: rec(phase="run", progress=7)}, {}),
        "dead": ({"polls": lambda n, env: rec(alive=False)}, {}),
        "unreachable": ({"polls": lambda n, env: None}, {}),
        "start_failed": ({"starts": ["drop"] * 9}, {}),
        "error": ({"on_call": raise_mid_loop}, {}),
    }


def signal_at_second_poll(signum):
    def hook(kind, fake):
        if kind == "poll" and fake.poll_count == 2:
            signal.getsignal(signum)(signum, None)  # the runner's own handler, as the signal would call it

    return hook


class EveryExitStopsTest(unittest.TestCase):
    def run_one(self, fake_kw, run_kw, tmp):
        colab = FakeColab(**fake_kw)
        runner, _ = make(colab, tmp)
        salvaged = []
        kw = dict(script="/content/x.sh", wall_s=36000, salvage=lambda why: colab.events.append("salvage") or
                  salvaged.append(why) or True)
        kw.update(run_kw)
        return colab, runner, salvaged, kw

    def assert_stopped_once(self, colab, runner, res):
        self.assertEqual(colab.stops, [("sess", 0)], res.reason)  # one stop call, confirmed, of this session
        self.assertTrue(res.stopped, res.reason)
        self.assertFalse((runner.out_dir / "session_open.json").exists(), res.reason)
        self.assertEqual(json.loads((runner.out_dir / "stop_confirmed.json").read_text())["session"], "sess")
        written = json.loads((runner.out_dir / "runner_result.json").read_text())
        self.assertEqual((written["reason"], written["stopped"]), (res.reason, True))

    def test_each_exit_path_ends_with_exactly_one_confirmed_stop_of_its_own_session(self):
        for reason, (fake_kw, run_kw) in scenarios().items():
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as tmp:
                colab, runner, salvaged, kw = self.run_one(fake_kw, run_kw, tmp)
                if reason == "error":
                    with self.assertRaises(RuntimeError):
                        runner.run(**kw)
                    res = runner._res
                else:
                    res = runner.run(**kw)
                self.assertEqual(res.reason, reason)
                self.assert_stopped_once(colab, runner, res)
                self.assertEqual(salvaged, [] if reason == "done" else [reason])
                # on a trip the VM job's process group is terminated first, then salvage, then the stop
                tail = [e for e in colab.events if e in ("kill", "salvage", "stop")]
                if reason in TRIPS:
                    self.assertEqual(tail, ["kill", "salvage", "stop"], reason)
                else:
                    self.assertEqual(tail, ([] if reason == "done" else ["salvage"]) + ["stop"], reason)

    def test_sigint_and_sigterm_raise_into_the_run_and_the_finally_stops(self):
        for signum in (signal.SIGINT, signal.SIGTERM):
            with self.subTest(signal=signum), tempfile.TemporaryDirectory() as tmp:
                before = signal.getsignal(signum)
                colab, runner, salvaged, kw = self.run_one({"on_call": signal_at_second_poll(signum)}, {}, tmp)
                res = runner.run(**kw)
                self.assertEqual(res.reason, "interrupted")
                self.assertIn(signal.Signals(signum).name, res.detail)
                self.assert_stopped_once(colab, runner, res)
                self.assertEqual(salvaged, ["interrupted"])
                self.assertIs(signal.getsignal(signum), before)  # the caller's handler is back

    @unittest.skipUnless(POSIX, "a real SIGTERM to this process; Windows would terminate it outright")
    def test_a_real_sigterm_mid_run_still_stops(self):
        def hook(kind, fake):
            if kind == "poll" and fake.poll_count == 2:
                os.kill(os.getpid(), signal.SIGTERM)

        with tempfile.TemporaryDirectory() as tmp:
            colab, runner, _, kw = self.run_one({"on_call": hook}, {}, tmp)
            res = runner.run(**kw)
            self.assertEqual(res.reason, "interrupted")
            self.assert_stopped_once(colab, runner, res)

    def test_a_signal_during_cleanup_never_stops_the_stop(self):
        def hook(kind, fake):
            if kind == "poll" and fake.poll_count == 2:
                signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)  # first: into the run
            if kind == "kill":
                signal.getsignal(signal.SIGTERM)(signal.SIGTERM, None)  # second, during cleanup: recorded only

        with tempfile.TemporaryDirectory() as tmp:
            colab, runner, salvaged, kw = self.run_one({"on_call": hook}, {}, tmp)
            res = runner.run(**kw)
            self.assertEqual(res.reason, "interrupted")
            self.assertEqual(salvaged, [])  # a second signal skips the salvage
            self.assertFalse(res.salvaged)
            self.assert_stopped_once(colab, runner, res)

    def test_a_signal_during_the_salvage_aborts_it_and_the_stop_still_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            colab = FakeColab(polls=lambda n, env: rec(alive=False))
            runner, _ = make(colab, tmp)

            def salvage(why):
                signal.getsignal(signal.SIGINT)(signal.SIGINT, None)
                raise AssertionError("not reached: the signal aborts the salvage")

            res = runner.run(script="/content/x.sh", wall_s=3600, salvage=salvage)
            self.assertEqual(res.reason, "dead")
            self.assertFalse(res.salvaged)
            self.assert_stopped_once(colab, runner, res)

    def test_a_simulated_crash_mid_loop_still_stops(self):
        with tempfile.TemporaryDirectory() as tmp:
            fake_kw, _ = scenarios()["error"]
            colab, runner, _, kw = self.run_one(fake_kw, {}, tmp)
            with self.assertRaises(RuntimeError):
                runner.run(**kw)
            self.assertEqual(runner._res.reason, "error")
            self.assertIn("simulated crash", runner._res.detail)
            self.assert_stopped_once(colab, runner, runner._res)
            self.assertEqual(colab.kill_calls, 1)

    def test_session_open_json_names_the_session_before_the_start_cell(self):
        seen = []

        with tempfile.TemporaryDirectory() as tmp:
            def hook(kind, fake):
                if kind == "start":
                    marker = Path(tmp) / "out" / "session_open.json"
                    seen.append(json.loads(marker.read_text())["session"] if marker.is_file() else None)

            colab = FakeColab(polls=lambda n, env: rec(rc=0), on_call=hook)
            runner, _ = make(colab, tmp)
            runner.run(script="/content/x.sh", wall_s=3600)
            self.assertEqual(seen, ["sess"])

    def test_a_failed_stop_keeps_session_open_json_for_a_later_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            colab = FakeColab(polls=lambda n, env: rec(rc=0), stops=[1, 1, 1])
            runner, _ = make(colab, tmp)
            res = runner.run(script="/content/x.sh", wall_s=3600)
            self.assertEqual((res.reason, res.stopped), ("done", False))
            self.assertEqual(colab.confirmed_stops(), [])
            self.assertTrue((runner.out_dir / "session_open.json").is_file())
            self.assertTrue((runner.out_dir / "stop_pending.json").is_file())
            self.assertFalse((runner.out_dir / "stop_confirmed.json").exists())

    def test_only_this_runs_session_is_ever_stopped(self):
        for reason, (fake_kw, run_kw) in scenarios().items():
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as tmp:
                colab, runner, _, kw = self.run_one(fake_kw, run_kw, tmp)
                try:
                    runner.run(**kw)
                except RuntimeError:
                    pass
                self.assertEqual({c["session"] for c in colab.calls if c["kind"] != "usage"}, {"sess"})


class StopCommandTest(unittest.TestCase):
    """`python -m colab_runner stop --out-dir D`: the compensator for a rig process killed outright."""

    def call(self, out_dir, colab):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch("colab_runner.__main__.cli_from_env", return_value=colab), \
                contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = main(["stop", "--out-dir", str(out_dir)])
        return code, out.getvalue(), err.getvalue()

    def test_a_leftover_session_open_json_is_stopped(self):
        with tempfile.TemporaryDirectory() as tmp:
            # a run whose rig process was killed outright: its marker is all that is left
            (Path(tmp) / "session_open.json").write_text(json.dumps({"session": "left-over", "job": "job"}))
            colab = FakeColab()
            code, out, _ = self.call(tmp, colab)
            self.assertEqual(code, 0)
            self.assertEqual(colab.stops, [("left-over", 0)])
            self.assertIn("STOP_END session=left-over confirmed=True", out)
            self.assertFalse((Path(tmp) / "session_open.json").exists())
            self.assertTrue((Path(tmp) / "stop_confirmed.json").is_file())

    def test_a_run_whose_stops_all_failed_is_finished_by_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            colab = FakeColab(polls=lambda n, env: rec(alive=False), stops=[1, 1, 1])
            runner, _ = make(colab, tmp)
            self.assertFalse(runner.run(script="/content/x.sh", wall_s=3600).stopped)
            later = FakeColab()
            self.assertEqual(self.call(runner.out_dir, later)[0], 0)
            self.assertEqual(later.stops, [("sess", 0)])
            self.assertFalse((runner.out_dir / "session_open.json").exists())
            self.assertFalse((runner.out_dir / "stop_pending.json").exists())

    def test_nothing_to_stop_makes_no_call(self):
        with tempfile.TemporaryDirectory() as tmp:
            colab = FakeColab()
            code, out, _ = self.call(tmp, colab)
            self.assertEqual((code, colab.calls), (0, []))
            self.assertIn("STOP_NOTHING", out)

    def test_a_stop_that_fails_keeps_the_marker_and_exits_1(self):
        with tempfile.TemporaryDirectory() as tmp:
            (Path(tmp) / "session_open.json").write_text(json.dumps({"session": "s1"}))
            colab = FakeColab(stops=[1, 1, 1])
            code, _, _ = self.call(tmp, colab)
            self.assertEqual(code, 1)
            self.assertTrue((Path(tmp) / "session_open.json").is_file())
            self.assertTrue((Path(tmp) / "stop_pending.json").is_file())

    def test_a_marker_without_a_session_is_refused(self):
        for body in ("{}", '{"session": 5}', '{"session": ""}', "not json"):
            with self.subTest(body=body), tempfile.TemporaryDirectory() as tmp:
                (Path(tmp) / "session_open.json").write_text(body)
                colab = FakeColab()
                code, _, err = self.call(tmp, colab)
                self.assertEqual((code, colab.calls), (3, []))
                self.assertIn("REFUSED: SESSION_MARKER_INVALID", err)


class FailuresAreFailuresTest(unittest.TestCase):
    """Item 8: a non-zero rc is `failed`; rc 137 or an OOM kill line is `oom`; both are salvaged."""

    def run_rc(self, rc, lines=None):
        with tempfile.TemporaryDirectory() as tmp:
            colab = FakeColab(polls=lambda n, env: rec(rc=rc))
            colab.log_lines = list(lines or [])
            runner, _ = make(colab, tmp)
            got = []
            res = runner.run(script="/content/x.sh", wall_s=3600, salvage=lambda why: got.append(why) or True)
            return res, got

    def test_rc_1_is_failed_and_salvaged(self):
        res, got = self.run_rc(1)
        self.assertEqual((res.reason, res.rc, got, res.salvaged), ("failed", 1, ["failed"], True))

    def test_rc_137_is_oom_and_salvaged(self):
        res, got = self.run_rc(137)
        self.assertEqual((res.reason, res.rc, got), ("oom", 137, ["oom"]))

    def test_an_oom_kill_line_in_the_log_is_oom_and_salvaged(self):
        for line in ("[ 812.1] Out of memory: Killed process 4242 (python3)", "oom_kill 1"):
            with self.subTest(line=line):
                res, got = self.run_rc(0, ["working", line])
                self.assertEqual((res.reason, res.rc, got), ("oom", 0, ["oom"]))

    def test_a_zero_oom_kill_count_is_not_an_oom(self):
        res, got = self.run_rc(0, ["oom_kill 0"])
        self.assertEqual((res.reason, got), ("done", []))

    def test_the_cli_exits_non_zero_for_failed_and_oom(self):
        base = ["run", "--session", "s", "--script", "/content/x.sh", "--wall-s", "3600", "--max-rate", "1",
                "--cap", "5", "--cleanup-bound-s", "600"]
        for rc, want in ((0, 0), (1, 1), (137, 1)):
            with self.subTest(rc=rc), tempfile.TemporaryDirectory() as tmp:
                colab = FakeColab(polls=lambda n, env, rc=rc: rec(rc=rc))
                out = io.StringIO()
                with mock.patch("colab_runner.__main__.cli_from_env", return_value=colab), \
                        contextlib.redirect_stdout(out), contextlib.redirect_stderr(io.StringIO()):
                    code = main(base + ["--out-dir", str(Path(tmp) / "out")])
                self.assertEqual(code, want, out.getvalue())
                self.assertIn("reason=%s" % {0: "done", 1: "failed", 137: "oom"}[rc], out.getvalue())


if __name__ == "__main__":
    unittest.main()

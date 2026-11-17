"""Items 2 and 3: no unbounded window (every bad bound refused by name before any `colab` call), and every step
clamped to the wall and the cap's ceiling. Also the (B): a non-empty out dir is refused."""

import contextlib
import io
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from colab_runner.__main__ import main
from colab_runner.runner import MIN_CALL_S, REACHED_EPS_S, Refused, Runner
from colab_runner.spend import SpendGuard
from colab_runner.window_lib import launch_guards as lg
from tests.fakes import FakeClock, FakeColab, guard, make, rec

# The bound the clamp tests hold the runner to: every step (a sleep, or a call's timeout, which a lost call uses in
# full) is clamped to the time left before the nearer of the wall and the ceiling when it starts, so a window passes
# neither by more than one clamped step, and a clamped step ends at the bound at the latest. What is left is float
# noise, under REACHED_EPS_S.
ONE_CLAMPED_STEP_OVERRUN_S = REACHED_EPS_S


class CliRefusalTest(unittest.TestCase):
    """Every bad value is refused by name (REFUSED: <CODE>, exit 3), with zero fake calls made."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.out = str(Path(self._tmp.name) / "out")

    def base(self):
        return ["run", "--session", "s", "--out-dir", self.out, "--hold-s", "1", "--wall-s", "3600",
                "--max-rate", "1", "--cap", "5", "--cleanup-bound-s", "600"]

    def call(self, args):
        colab = FakeColab(polls=lambda n, env: rec(rc=0))
        err, out = io.StringIO(), io.StringIO()
        with mock.patch("colab_runner.__main__.cli_from_env", return_value=colab), \
                contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
            code = main(args)
        return code, err.getvalue(), colab

    def with_flag(self, flag, value):
        args = self.base()
        if flag in args:
            i = args.index(flag)
            del args[i:i + 2]
        return args + ["%s=%s" % (flag, value)]  # the = form, so argparse takes "-inf" as a value

    def test_the_good_base_does_call_colab(self):  # so "zero calls" below means something
        code, err, colab = self.call(self.base())
        self.assertEqual(code, 0, err)
        self.assertGreater(len(colab.calls), 0)

    def test_each_bad_value_is_refused_by_name_with_zero_calls(self):
        cases = [
            ("--wall-s", ("0", "-5", "nan", "inf"), "WALL_INVALID"),
            ("--poll-s", ("0", "-1", "nan", "inf"), "POLL_INVALID"),
            ("--heartbeat-s", ("-1", "nan", "inf"), "HEARTBEAT_INVALID"),
            ("--margin", ("-0.1", "nan", "inf", "-inf"), "MARGIN_INVALID"),
            ("--max-rate", ("0", "-0.1", "nan", "inf"), "MAX_RATE_INVALID"),
            ("--cap", ("0", "-1", "nan", "inf"), "CAP_INVALID"),
            ("--cleanup-bound-s", ("-1", "nan", "inf"), "CLEANUP_BOUND_INVALID"),
            ("--hold-s", ("0", "nan", "inf"), "HOLD_INVALID"),
            ("--progress-stall-s", ("0", "nan", "inf"), "PROGRESS_STALL_INVALID"),
            ("--setup-stall-s", ("0", "nan", "inf"), "SETUP_STALL_INVALID"),
            ("--unreachable-polls", ("0", "-3"), "UNREACHABLE_INVALID"),
            ("--unreachable-s", ("0", "nan", "inf"), "UNREACHABLE_INVALID"),
            ("--pre-open-rate", ("-1", "nan", "inf"), "PRE_OPEN_RATE_INVALID"),
            ("--pre-open-assignments", ("-1",), "PRE_OPEN_ASSIGNMENTS_INVALID"),
            # a ceiling at or below zero: margin + cleanup_reserve >= cap (the library's evaluate_cap_budget)
            ("--margin", ("5", "4.9"), "CAP_TOO_SMALL_FOR_CLEANUP"),  # 4.9 + 600/3600 x 1 >= 5
            ("--cleanup-bound-s", ("18000", "20000"), "CAP_TOO_SMALL_FOR_CLEANUP"),  # 5 units of reserve at 1/h
            # below the runner's own kill + stop bound
            ("--cleanup-bound-s", ("0", "100"), "CLEANUP_BOUND_TOO_SMALL"),
        ]
        for flag, values, code_name in cases:
            for value in values:
                with self.subTest(flag=flag, value=value):
                    code, err, colab = self.call(self.with_flag(flag, value))
                    self.assertEqual(code, 3, err)
                    self.assertIn("REFUSED: %s:" % code_name, err)
                    self.assertEqual(colab.calls, [])

    def test_missing_required_bounds_are_refused_by_name(self):
        for flag, code_name in (("--cap", "CAP_REQUIRED"), ("--max-rate", "MAX_RATE_REQUIRED"),
                                ("--cleanup-bound-s", "CLEANUP_BOUND_REQUIRED")):
            with self.subTest(flag=flag):
                args = self.base()
                i = args.index(flag)
                del args[i:i + 2]
                code, err, colab = self.call(args)
                self.assertEqual((code, colab.calls), (3, []))
                self.assertIn("REFUSED: %s:" % code_name, err)

    def test_a_resident_run_needs_the_resident_cleanup_bound(self):
        code, err, colab = self.call(self.base() + ["--resident"])  # 600 s is below the resident bound (966 s)
        self.assertEqual((code, colab.calls), (3, []))
        self.assertIn("REFUSED: CLEANUP_BOUND_TOO_SMALL:", err)

    def test_bad_env_entries_are_refused_without_echoing_a_value(self):
        for entry, code_name in (("1BAD=x", "ENV_INVALID"), ("=v", "ENV_INVALID"), ("CR_TEST_SURELY_UNSET", "ENV_UNSET")):
            with self.subTest(entry=entry):
                code, err, colab = self.call(self.base() + ["--env", entry])
                self.assertEqual((code, colab.calls), (3, []))
                self.assertIn("REFUSED: %s:" % code_name, err)
                self.assertNotIn("=x", err)

    def test_a_non_empty_out_dir_is_refused(self):
        Path(self.out).mkdir()
        (Path(self.out) / "stop_confirmed.json").write_text("{}")  # a stale marker from an earlier run
        code, err, colab = self.call(self.base())
        self.assertEqual((code, colab.calls), (3, []))
        self.assertIn("REFUSED: OUT_DIR_NOT_EMPTY:", err)

    def test_the_old_rate_flag_no_longer_stands_in_for_max_rate(self):
        with self.assertRaises(SystemExit):
            self.call(["run", "--session", "s", "--out-dir", self.out, "--hold-s", "1", "--wall-s", "10", "--rate",
                       "10.5", "--cap", "5"])


class ApiRefusalTest(unittest.TestCase):
    """The same bounds hold for library callers: SpendGuard, Runner and Runner.run refuse before any call."""

    def test_the_guard_refuses_a_bad_rate_cap_margin_or_ceiling(self):
        for kw in ({"max_rate": 0}, {"max_rate": float("inf")}, {"max_rate": float("nan")}, {"cap": 0},
                   {"cap": float("inf")}, {"margin": float("nan")}, {"margin": -1}, {"margin": float("-inf")},
                   {"cleanup_bound_s": float("inf")}, {"margin": 50.0}):
            with self.subTest(kw=kw):
                with self.assertRaises(Refused):
                    guard(**kw)

    def test_the_guard_needs_a_cleanup_bound(self):
        with self.assertRaises(TypeError):
            SpendGuard(max_rate=1.0, cap=5.0)

    def test_the_runner_refuses_bad_settings(self):
        for kw in ({"poll_s": 0}, {"poll_s": float("nan")}, {"poll_timeout_s": float("inf")}, {"backoff_max_s": -1},
                   {"start_attempts": 0}, {"stop_attempts": 1.5}):
            with self.subTest(kw=kw), tempfile.TemporaryDirectory() as tmp:
                with self.assertRaises(Refused):
                    Runner(FakeColab(), "s", Path(tmp), **kw)

    def test_run_refuses_before_any_call(self):
        for kw, code_name in (({"wall_s": float("inf")}, "WALL_INVALID"), ({"wall_s": float("nan")}, "WALL_INVALID"),
                              ({"wall_s": 10, "heartbeat_s": -1}, "HEARTBEAT_INVALID"),
                              ({"wall_s": 10, "guard": guard(cleanup_bound_s=10)}, "CLEANUP_BOUND_TOO_SMALL")):
            with self.subTest(kw=kw), tempfile.TemporaryDirectory() as tmp:
                colab = FakeColab()
                runner, _ = make(colab, tmp)
                with self.assertRaises(Refused) as cm:
                    runner.run(script="/content/x.sh", **kw)
                self.assertEqual((cm.exception.code, colab.calls), (code_name, []))

    def test_run_refuses_a_non_empty_out_dir(self):
        with tempfile.TemporaryDirectory() as tmp:
            colab = FakeColab()
            runner, _ = make(colab, tmp)
            runner.out_dir.mkdir()
            (runner.out_dir / "job.run.out").write_text("an earlier run's log\n")
            with self.assertRaises(Refused) as cm:
                runner.run(script="/content/x.sh", wall_s=600)
            self.assertEqual((cm.exception.code, colab.calls), ("OUT_DIR_NOT_EMPTY", []))

    def test_the_cleanup_reserve_is_the_library_rule(self):
        g = guard(max_rate=3.6, cap=5.0, margin=0.5, cleanup_bound_s=1000)
        want = lg.compute_cap_trip(5.0, 0.5, 1000, 3.6)
        self.assertEqual(g.trip(), want)
        self.assertEqual(want["cleanup_reserve_units"], 1.0)  # 1000 / 3600 x 3.6
        self.assertEqual(g.ceiling, 3.5)


class ClampTest(unittest.TestCase):
    """With the fake clock (a lost call costs its whole timeout): during start retries, backoff and normal polling,
    no window passes its wall or its ceiling by more than ONE_CLAMPED_STEP_OVERRUN_S."""

    def assert_steps_clamped(self, colab, clock, t0, bound_s):
        end = t0 + bound_s + ONE_CLAMPED_STEP_OVERRUN_S
        for when, length in clock.sleeps:
            self.assertLessEqual(when + length, end, "a sleep passed the bound")
        for call in colab.calls:
            if call["kind"] in ("poll", "start", "usage", "download"):
                self.assertLessEqual(call["at"] + call["timeout"], end, "a %s timeout passed the bound" % call["kind"])

    def run_window(self, colab, wall_s, g=None, **kw):
        with tempfile.TemporaryDirectory() as tmp:
            runner, clock = make(colab, tmp, **kw.pop("runner_kw", {}))
            t0 = clock.now()
            res = runner.run(script="/content/x.sh", wall_s=wall_s, guard=g, unreachable_polls=10_000,
                             unreachable_s=1e9, **kw)
            # only the window itself is clamped: the kill and the stop after the trip are paid by the cleanup reserve
            colab.calls = [c for c in colab.calls if c["at"] <= t0 + res.elapsed_s]
            clock.sleeps = [s for s in clock.sleeps if s[0] < t0 + res.elapsed_s]
            return res, clock, t0

    def test_start_retries_stop_before_the_wall(self):
        colab = FakeColab(starts=["drop"] * 9)
        res, clock, t0 = self.run_window(colab, 200)
        self.assertEqual(res.reason, "start_failed")
        # 0-60, 62-122, 126-186, then 194-200 clamped to the 6 s left after its 8 s backoff; a fifth has no time
        self.assertEqual(colab.start_calls, 4)
        self.assertEqual([c["timeout"] for c in colab.calls if c["kind"] == "start"], [60, 60, 60, 6])
        self.assertLessEqual(res.elapsed_s, 200 + ONE_CLAMPED_STEP_OVERRUN_S)
        self.assert_steps_clamped(colab, clock, t0, 200)

    def test_start_retries_stop_before_the_ceiling(self):
        g = guard(max_rate=3.6, cap=1.15)  # ceiling 1.15 - 1000/3600 x 3.6 = 0.15 units: 150 s at 3.6/h
        self.assertAlmostEqual(g.seconds_left(0), 150.0)
        colab = FakeColab(starts=["drop"] * 9)
        res, clock, t0 = self.run_window(colab, 3600, g)
        # 0-60, 62-122, then 126-150 clamped to the 24 s left after its 4 s backoff
        self.assertEqual((res.reason, colab.start_calls), ("start_failed", 3))
        self.assertAlmostEqual([c["timeout"] for c in colab.calls if c["kind"] == "start"][-1], 24.0, delta=1e-6)
        self.assert_steps_clamped(colab, clock, t0, 150)

    def test_backoff_is_clamped_to_the_wall(self):
        colab = FakeColab(polls=lambda n, env: None)  # every poll lost, each costing its whole timeout
        res, clock, t0 = self.run_window(colab, 1000)
        self.assertEqual(res.reason, "wall")
        self.assertGreater(res.polls_failed, 3)
        self.assertLessEqual(res.elapsed_s, 1000 + ONE_CLAMPED_STEP_OVERRUN_S)
        self.assert_steps_clamped(colab, clock, t0, 1000)

    def test_backoff_is_clamped_to_the_ceiling(self):
        g = guard(max_rate=3.6, cap=1.7)  # ceiling 0.7 units: 700 s
        colab = FakeColab(polls=lambda n, env: None)
        res, clock, t0 = self.run_window(colab, 36000, g)
        self.assertEqual(res.reason, "cap")
        self.assertLessEqual(res.elapsed_s, 700 + ONE_CLAMPED_STEP_OVERRUN_S)
        self.assert_steps_clamped(colab, clock, t0, 700)

    def test_normal_polling_is_clamped_to_the_wall(self):
        colab = FakeColab(polls=lambda n, env: rec(progress=n))  # wall 1000 is not a multiple of poll_s 60
        res, clock, t0 = self.run_window(colab, 1000)
        self.assertEqual(res.reason, "wall")
        self.assertAlmostEqual(res.elapsed_s, 1000, delta=ONE_CLAMPED_STEP_OVERRUN_S)
        self.assertAlmostEqual(clock.sleeps[-1][1], 1000 - 16 * 60, delta=1e-6)  # the last sleep, clamped
        self.assert_steps_clamped(colab, clock, t0, 1000)

    def test_normal_polling_is_clamped_to_the_ceiling(self):
        g = guard(max_rate=3.6, cap=1.65)  # ceiling 0.65 units: 650 s
        colab = FakeColab(polls=lambda n, env: rec(progress=n))
        res, clock, t0 = self.run_window(colab, 36000, g)
        self.assertEqual(res.reason, "cap")
        self.assertLessEqual(res.elapsed_s, 650 + ONE_CLAMPED_STEP_OVERRUN_S)
        self.assert_steps_clamped(colab, clock, t0, 650)

    def test_a_poll_timeout_is_clamped_to_the_time_left(self):
        colab = FakeColab(polls=lambda n, env: None)
        self.run_window(colab, 230, runner_kw={"poll_s": 50, "poll_timeout_s": 100, "backoff_max_s": 50})
        polls = [c for c in colab.calls if c["kind"] == "poll"]
        self.assertEqual([c["timeout"] for c in polls], [100, 80])  # 0-100, a 50 s backoff, then the 80 s left

    def test_no_call_is_made_with_less_than_a_second_left(self):
        colab = FakeColab(polls=lambda n, env: rec(progress=n))
        self.run_window(colab, 60.5, runner_kw={"poll_s": 60})
        polls = [c for c in colab.calls if c["kind"] == "poll"]
        self.assertTrue(all(c["timeout"] >= MIN_CALL_S for c in polls))

    def test_a_ceiling_reached_by_the_clock_ends_the_run_even_if_the_guard_is_quiet(self):
        class QuietGuard:  # never trips by itself, but its ceiling is 300 s away
            def check(self, elapsed_s):
                return None

            def seconds_left(self, elapsed_s):
                return 300.0 - elapsed_s

        colab = FakeColab(polls=lambda n, env: rec(progress=n))
        res, clock, t0 = self.run_window(colab, 36000, QuietGuard())
        self.assertEqual((res.reason, res.detail), ("cap", "the cap was reached"))
        self.assertAlmostEqual(res.elapsed_s, 300, delta=ONE_CLAMPED_STEP_OVERRUN_S)

    def test_the_fake_clock_starts_where_make_puts_it(self):
        self.assertEqual(FakeClock().now(), 1000.0)


def cli(args, colab):
    err, out = io.StringIO(), io.StringIO()
    with mock.patch("colab_runner.__main__.cli_from_env", return_value=colab), \
            contextlib.redirect_stderr(err), contextlib.redirect_stdout(out):
        code = main(args)
    return code, err.getvalue() + out.getvalue()


class Fix2BoundsTest(unittest.TestCase):
    """Fix 2: pre-open values come in pairs (item 3), a start attempt is clamped rather than skipped (item 4),
    a hold job refuses a zero heartbeat (item 5), and the resident cleanup bound counts its extra waits (item 7)."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = ["run", "--session", "s", "--out-dir", str(Path(self._tmp.name) / "out"), "--wall-s", "3600",
                     "--max-rate", "1", "--cap", "5", "--cleanup-bound-s", "600"]

    def test_an_unpaired_pre_open_value_is_refused_at_the_cli(self):
        for extra in (["--pre-open-rate", "20.0"], ["--pre-open-assignments", "2"]):
            with self.subTest(extra=extra):
                colab = FakeColab(polls=lambda n, env: rec(rc=0))
                code, text = cli(self.base + ["--script", "/c/x.sh"] + extra, colab)
                self.assertEqual((code, colab.calls), (3, []))
                self.assertIn("REFUSED: PRE_OPEN_UNPAIRED:", text)

    def test_an_unpaired_pre_open_value_is_refused_at_the_api(self):
        for kw in ({"pre_open_rate": 20.0}, {"pre_open_assignments": 2}):
            with self.subTest(kw=kw), self.assertRaises(Refused) as cm:
                guard(**kw)
            self.assertEqual(cm.exception.code, "PRE_OPEN_UNPAIRED")
        self.assertEqual(guard(pre_open_rate=20.0, pre_open_assignments=2).pre_open_assignments, 2)  # both: fine

    def test_a_real_clock_run_with_a_60_s_wall_makes_its_start_attempt(self):
        colab = FakeColab(polls=lambda n, env: rec(rc=0))  # no fake clock: the runner's real monotonic clock
        code, text = cli(self.base[:-8] + ["--wall-s", "60", "--max-rate", "1", "--cap", "5", "--cleanup-bound-s",
                                           "600", "--script", "/c/x.sh"], colab)
        self.assertEqual(code, 0, text)
        starts = [c for c in colab.calls if c["kind"] == "start"]
        self.assertEqual(len(starts), 1)  # made, not skipped
        self.assertLessEqual(starts[0]["timeout"], 60.0)  # clamped to what was left

    def test_a_60_s_wall_whose_clock_has_moved_still_makes_its_start_attempt(self):
        class TickingClock(FakeClock):  # real time passes a little at every read, as time.monotonic does
            def now(self):
                self.now_s += 0.001
                return self.now_s

        colab = FakeColab(polls=lambda n, env: rec(rc=0), clock=TickingClock())
        with tempfile.TemporaryDirectory() as tmp:
            runner, _ = make(colab, tmp)
            res = runner.run(script="/c/x.sh", wall_s=60)
        self.assertEqual(res.reason, "done")
        start = next(c for c in colab.calls if c["kind"] == "start")
        self.assertLess(start["timeout"], 60.0)  # the old code skipped this attempt: 60 s no longer fitted

    def test_a_start_attempt_with_less_than_min_call_s_left_is_not_made(self):
        colab = FakeColab(starts=["drop"] * 9)
        with tempfile.TemporaryDirectory() as tmp:
            runner, _ = make(colab, tmp)
            res = runner.run(script="/c/x.sh", wall_s=MIN_CALL_S / 2)
        self.assertEqual((res.reason, colab.start_calls), ("start_failed", 0))

    def test_a_hold_job_refuses_a_zero_heartbeat_and_a_script_job_does_not_care(self):
        colab = FakeColab(polls=lambda n, env: rec(rc=0))
        code, text = cli(self.base + ["--hold-s", "60", "--heartbeat-s", "0"], colab)
        self.assertEqual((code, colab.calls), (3, []))
        self.assertIn("REFUSED: HEARTBEAT_INVALID:", text)
        code, text = cli(self.base + ["--script", "/c/x.sh", "--heartbeat-s", "0"], colab)
        self.assertEqual(code, 0, text)
        with tempfile.TemporaryDirectory() as tmp:
            api = FakeColab()
            runner, _ = make(api, tmp)
            with self.assertRaises(Refused) as cm:
                runner.run(hold_s=60, heartbeat_s=0, wall_s=600)
            self.assertEqual((cm.exception.code, api.calls), ("HEARTBEAT_INVALID", []))

    def test_the_resident_cleanup_bound_counts_its_report_time_and_its_own_kill(self):
        from colab_runner.colab_cli import EXEC_REPORT_S, KILL_WAIT_S

        runner = Runner(FakeColab(), "s", Path(self._tmp.name))
        plain = runner.own_cleanup_bound_s(False)
        extra = runner.restart_timeout_s + runner.log_timeout_s + 2 * 2 * KILL_WAIT_S
        self.assertAlmostEqual(runner.own_cleanup_bound_s(True) - plain, extra + EXEC_REPORT_S + KILL_WAIT_S)


if __name__ == "__main__":
    unittest.main()

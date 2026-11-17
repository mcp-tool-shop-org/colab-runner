"""Item 6: one implementation. The guard's prices are the vendored library's on shared fixtures, rates above
--max-rate are refused as the library refuses them, and the CLI wrapper kills a call's whole process group."""

import os
import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path
from unittest import mock

from colab_runner import colab_cli
from colab_runner.colab_cli import TIMEOUT_RC, Colab
from colab_runner.spend import SpendGuard
from colab_runner.window_lib import launch_guards as lg
from tests.fakes import usage_text

POSIX = os.name == "posix"

# Shared fixtures: (name, guard settings, readings as (epoch, rc, usage text), what the library decides).
FIXTURES = [
    # a CPU window (0.26/h) beside two G4 neighbours: never a stable rate at the GPU floor, so --max-rate prices it
    ("cpu beside g4 neighbours", {"max_rate": 0.26, "pre_open_rate": 20.1, "pre_open_assignments": 2},
     [(e, 0, usage_text(rate=20.36, n=3, balance=1800 - e / 180)) for e in (0, 60, 120, 180)], "unread"),
    # a GPU window stable at 10.05 under a 10.5 max: priced at max(--max-rate, highest stable) = 10.5
    ("gpu stable under max", {"max_rate": 10.5, "pre_open_rate": 0.0, "pre_open_assignments": 0},
     [(e, 0, usage_text(rate=10.05, n=1)) for e in (0, 60, 120)], "ok"),
    # a GPU window stable at 11.2 above a 10.5 max: refused, RATE_ABOVE_MAX
    ("gpu stable above max", {"max_rate": 10.5, "pre_open_rate": 0.0, "pre_open_assignments": 0},
     [(e, 0, usage_text(rate=11.2, n=1)) for e in (0, 60, 120)], "above_max"),
    # a neighbour opened between the pre-open read and ours (2 more assignments): unattributable, --max-rate
    ("unattributable", {"max_rate": 10.5, "pre_open_rate": 10.0, "pre_open_assignments": 1},
     [(e, 0, usage_text(rate=30.1, n=3)) for e in (0, 60)], "unattributable"),
    # after a stable read, three reads with no assignment count: ASSIGNMENTS_UNREADABLE
    ("assignments unreadable", {"max_rate": 10.5, "pre_open_rate": 0.0, "pre_open_assignments": 0},
     [(0, 0, usage_text(rate=10.0, n=1)), (60, 0, usage_text(rate=10.0, n=1))]
     + [(e, 0, "Current balance: 90.00 compute units\nUsage rate: 10.00 / hr\n") for e in (120, 180, 240)],
     "assignments_unreadable"),
    # the account reads 0.00/h at first (the session not registered yet), then the window's rate
    ("rate unread at first", {"max_rate": 10.5, "pre_open_rate": 0.0, "pre_open_assignments": 0},
     [(0, 0, usage_text(rate=0.0, n=0)), (60, 0, usage_text(rate=10.05, n=1)), (120, 0, usage_text(rate=10.05, n=1))],
     "ok"),
]


def library_alone(settings, readings):
    """The library's own answer for the readings, with no SpendGuard involved."""
    state, balance, error = {}, None, None
    for epoch, rc, raw in readings:
        try:
            parsed = lg.parse_usage_text(raw, "gpu", usage_rc=rc, session_opened=True)
        except lg.RateUnread as exc:
            parsed = dict(exc.partial, ok=True, error=None)
        if not parsed["ok"]:
            error = parsed["error"]
            continue
        error, balance = None, parsed["balance"]
        lg.update_window_rate(state, account_rate=parsed["rate_measured"], pre_open_rate=settings.get("pre_open_rate"),
                              at="t%d" % epoch, epoch=epoch, assignments=parsed["active_assignments"],
                              pre_open_assignments=settings.get("pre_open_assignments"))
    return state, balance, error


class SharedFixturesTest(unittest.TestCase):
    CAP, MARGIN, CLEANUP = 40.0, 0.5, 900.0

    def guard_for(self, settings, readings):
        g = SpendGuard(cap=self.CAP, margin=self.MARGIN, cleanup_bound_s=self.CLEANUP, **settings)
        for epoch, rc, raw in readings:
            g.fold_usage(raw, rc=rc, epoch=epoch, at="t%d" % epoch)
        return g

    def test_the_guard_prices_as_the_library_on_shared_fixtures(self):
        for name, settings, readings, decision in FIXTURES:
            with self.subTest(fixture=name):
                g = self.guard_for(settings, readings)
                state, balance, error = library_alone(settings, readings)
                max_rate = settings["max_rate"]
                self.assertEqual(g.state, state)
                self.assertEqual(g.decision(), lg.window_rate_decision(state, max_rate))
                self.assertEqual(g.decision(), decision)
                priced = lg.gpu_projection_rate(state, max_rate)
                self.assertEqual(g.priced_rate(), priced)
                self.assertGreaterEqual(priced, max_rate)  # never below --max-rate
                self.assertEqual(g.trip(), lg.compute_cap_trip(self.CAP, self.MARGIN, self.CLEANUP, priced))
                for elapsed in (0.0, 1800.0, 7200.0):
                    want = lg.evaluate_cap_guard(
                        mode="live", cap=self.CAP, margin=self.MARGIN, cleanup_bound_s=self.CLEANUP,
                        start_balance=balance, current_balance=balance, rate_ccu_per_hour=priced, elapsed_s=elapsed,
                        usage_parse_error=error)
                    self.assertEqual(g.cap_record(elapsed), want)

    def test_a_window_the_library_cannot_price_is_priced_at_max_rate_x_elapsed(self):
        # launch_guards.py:310: `return max(float(max_rate), float(best)) if best is not None else float(max_rate)`
        for name in ("cpu beside g4 neighbours", "unattributable"):
            _, settings, readings, _ = next(f for f in FIXTURES if f[0] == name)
            g = self.guard_for(settings, readings)
            self.assertEqual(g.priced_rate(), settings["max_rate"])
            self.assertAlmostEqual(g.cap_record(3600)["spend_ccu"], settings["max_rate"], places=6)

    def test_a_stable_rate_above_max_rate_is_refused_as_the_library_refuses_it(self):
        _, settings, readings, _ = next(f for f in FIXTURES if f[0] == "gpu stable above max")
        trip = self.guard_for(settings, readings).check(60.0)
        self.assertTrue(trip.startswith("RATE_ABOVE_MAX:"), trip)

    def test_three_bad_assignment_reads_are_refused_as_the_library_refuses_them(self):
        _, settings, readings, _ = next(f for f in FIXTURES if f[0] == "assignments unreadable")
        self.assertTrue(self.guard_for(settings, readings).check(60.0).startswith("ASSIGNMENTS_UNREADABLE:"))

    def test_a_failed_usage_read_trips_the_cap_guard_as_the_library_does(self):
        g = self.guard_for({"max_rate": 1.0}, [(0, 0, usage_text(rate=0.26))])
        self.assertIsNone(g.check(60.0))
        g.fold_usage("RuntimeError: Connection was lost", rc=1, epoch=60, at="t60")
        self.assertTrue(g.check(60.0).startswith("COLAB_USAGE_PARSE_FAILED:"))
        g.fold_usage(usage_text(rate=0.26), rc=0, epoch=120, at="t120")  # a good read clears it
        self.assertIsNone(g.check(120.0))

    def test_no_reading_yet_is_not_a_price(self):
        g = SpendGuard(max_rate=1.0, cap=5.0, cleanup_bound_s=600)
        self.assertTrue(g.check(10.0).startswith("COLAB_USAGE_PARSE_FAILED:"))  # live mode needs a reading


GRANDCHILD_CLI = textwrap.dedent("""
    import os, subprocess, sys, time
    pid_file, mode = sys.argv[1], sys.argv[2]
    kw = {"start_new_session": True} if mode == "escaped" else {}
    child = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(60)"], **kw)  # inherits the pipes
    with open(pid_file, "w") as fh:
        fh.write(str(child.pid))
    print("cli out", flush=True)
    if mode == "hang":
        time.sleep(60)
""")


def gone(pid, secs=5.0):
    end = time.time() + secs
    while time.time() < end:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


class ProcessGroupTest(unittest.TestCase):
    """colab_cli kills the whole process group on a timeout, so a grandchild holding the pipes cannot hang it."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.cli = Path(self._tmp.name) / "cli.py"
        self.cli.write_text(GRANDCHILD_CLI)
        self.pid_file = Path(self._tmp.name) / "grandchild.pid"

    def run_cli(self, mode, timeout):
        colab = Colab([sys.executable, str(self.cli), str(self.pid_file), mode])
        t = time.monotonic()
        res = colab.run([], timeout)
        return res, time.monotonic() - t

    @unittest.skipUnless(POSIX, "process groups are POSIX")
    def test_a_cli_that_leaves_a_sleeping_grandchild_still_returns_within_its_timeout(self):
        for mode in ("hang", "exit"):  # the CLI hangs too, or exits at once and leaves only the grandchild
            with self.subTest(mode=mode):
                res, took = self.run_cli(mode, 1.5)
                self.assertEqual(res.rc, TIMEOUT_RC)
                self.assertLess(took, 1.5 + 2.0)
                self.assertTrue(gone(int(self.pid_file.read_text())), "the grandchild outlived the call")

    @unittest.skipUnless(POSIX, "process groups are POSIX")
    def test_a_grandchild_outside_the_group_still_cannot_hang_the_call(self):
        with mock.patch.object(colab_cli, "KILL_WAIT_S", 0.3):
            res, took = self.run_cli("escaped", 1.0)
        pid = int(self.pid_file.read_text())
        try:
            self.assertEqual(res.rc, TIMEOUT_RC)
            self.assertLess(took, 1.0 + 2 * 0.3 + 1.0)
        finally:
            os.kill(pid, 9)

    def test_a_hung_cli_returns_rc_124_within_its_timeout(self):
        hang = Path(self._tmp.name) / "hang.py"
        hang.write_text("import time\nprint('partial', flush=True)\ntime.sleep(60)\n")
        t = time.monotonic()
        res = Colab([sys.executable, str(hang)]).run([], 1.0)
        self.assertEqual(res.rc, TIMEOUT_RC)
        self.assertLess(time.monotonic() - t, 1.0 + 2 * colab_cli.KILL_WAIT_S)
        self.assertIn("timeout after", res.err)

    def test_an_interrupted_call_is_killed_before_the_exception_goes_on(self):
        hang = Path(self._tmp.name) / "hang.py"
        hang.write_text("import time\ntime.sleep(60)\n")
        colab = Colab([sys.executable, str(hang)])
        procs = []
        real_popen = colab_cli.subprocess.Popen

        def popen(*a, **kw):
            procs.append(real_popen(*a, **kw))
            return procs[-1]

        real_communicate = real_popen.communicate
        calls = []

        def interrupted(self, *a, **kw):  # the first wait is interrupted; the drain after the kill is real
            calls.append(1)
            if len(calls) == 1:
                raise KeyboardInterrupt
            return real_communicate(self, *a, **kw)

        with mock.patch.object(colab_cli.subprocess, "Popen", popen):
            with mock.patch.object(real_popen, "communicate", interrupted):
                with self.assertRaises(KeyboardInterrupt):
                    colab.run([], 30)
        self.assertIsNotNone(procs[0].returncode)  # killed and reaped, not left running
        self.assertEqual(len(calls), 2)

    def test_cli_result_flags(self):
        self.assertTrue(colab_cli.CliResult(TIMEOUT_RC).timed_out)
        self.assertFalse(colab_cli.CliResult(0).timed_out)

    @unittest.skipUnless(POSIX, "process groups are POSIX")
    def test_a_group_already_gone_is_not_an_error(self):
        hang = Path(self._tmp.name) / "hang.py"
        hang.write_text("import time\ntime.sleep(60)\n")
        with mock.patch.object(colab_cli.os, "killpg", side_effect=ProcessLookupError):
            res = Colab([sys.executable, str(hang)]).run([], 0.5)  # killpg fails; the call is still killed
        self.assertEqual(res.rc, TIMEOUT_RC)


if __name__ == "__main__":
    unittest.main()

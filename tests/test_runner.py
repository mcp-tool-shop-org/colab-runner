import json
import tempfile
import unittest
from pathlib import Path

from colab_runner.runner import Runner, cell_source, parse_poll
from colab_runner.spend import SpendGuard, attribute_rate, neighbour_warning
from tests.fakes import FakeClock, FakeColab, rec


def make(colab, tmp, **kw):
    clock = FakeClock()
    runner = Runner(colab, "sess", Path(tmp), clock=clock.now, sleep=clock.sleep, **kw)
    return runner, clock


class StartTest(unittest.TestCase):
    def test_lost_start_reply_never_starts_two_jobs(self):
        with tempfile.TemporaryDirectory() as tmp:
            colab = FakeColab(polls=lambda n, env: rec(rc=0), starts=["lose_reply"])
            runner, _ = make(colab, tmp)
            res = runner.run(script="/content/x.sh", wall_s=3600)
            self.assertEqual(res.reason, "done")
            self.assertEqual(colab.start_calls, 2)
            self.assertEqual(colab.jobs_started, 1)

    def test_start_that_never_arrives_fails_after_the_attempts(self):
        with tempfile.TemporaryDirectory() as tmp:
            colab = FakeColab(starts=["drop"] * 9)
            runner, _ = make(colab, tmp, start_attempts=3)
            res = runner.run(script="/content/x.sh", wall_s=3600)
            self.assertEqual(res.reason, "start_failed")
            self.assertEqual(colab.jobs_started, 0)
            self.assertTrue((Path(tmp) / "runner_result.json").is_file())

    def test_run_needs_something_to_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, _ = make(FakeColab(), tmp)
            with self.assertRaises(ValueError):
                runner.run(wall_s=10)


class PollTest(unittest.TestCase):
    def test_four_lost_polls_in_a_row_still_complete(self):
        def polls(n, env):
            if n <= 4:
                return None
            return rec(rc=0, ended_at="t")

        with tempfile.TemporaryDirectory() as tmp:
            colab = FakeColab(polls=polls)
            runner, _ = make(colab, tmp)
            res = runner.run(script="/content/x.sh", wall_s=36000)
            self.assertEqual((res.reason, res.rc), ("done", 0))
            self.assertEqual(res.polls_failed, 4)
            self.assertEqual(res.max_consecutive_failed, 4)
            self.assertEqual(colab.jobs_started, 1)  # a failed poll never touched the job

    def test_log_chunks_are_appended_and_the_offset_is_sent_back(self):
        def polls(n, env):
            return rec(rc=0 if n == 2 else None, log_offset=n * 10)

        with tempfile.TemporaryDirectory() as tmp:
            colab = FakeColab(polls=polls)
            colab.log_lines = ["hello"]
            runner, _ = make(colab, tmp)
            runner.run(script="/content/x.sh", wall_s=3600)
            offsets = [e["CR_POLL_LOG_OFFSET"] for e in colab.envs if "CR_POLL_LOG_OFFSET" in e]
            self.assertEqual(offsets, ["0", "10"])
            self.assertEqual((Path(tmp) / "job.run.out").read_text().count("hello"), 2)

    def test_more_log_to_read_polls_again_at_once(self):
        calls = []

        def polls(n, env):
            calls.append(n)
            return rec(rc=0 if n == 3 else None, log_eof=n >= 2, log_offset=n)

        with tempfile.TemporaryDirectory() as tmp:
            runner, clock = make(FakeColab(polls=polls), tmp)
            runner.run(script="/content/x.sh", wall_s=3600)
            self.assertEqual(calls, [1, 2, 3])
            self.assertEqual(clock.now_s, 1000.0 + 60.0)  # only one sleep, after the poll that reached eof

    def test_parse_poll_ignores_garbage(self):
        self.assertIsNone(parse_poll("nothing here"))
        self.assertIsNone(parse_poll("CR_POLL {not json"))
        got = parse_poll('CR_LOG a\nCR_POLL {"rc": 0}\n')
        self.assertEqual(got, ({"rc": 0}, ["a"]))


class EndTest(unittest.TestCase):
    def test_dead_without_status_is_salvaged_and_the_marker_follows_the_salvage(self):
        with tempfile.TemporaryDirectory() as tmp:
            colab = FakeColab(polls=lambda n, env: rec(alive=False))
            runner, _ = make(colab, tmp)
            seen = []
            res = runner.run(script="/content/x.sh", wall_s=3600, salvage=lambda why: seen.append(why) or True)
            self.assertEqual(res.reason, "dead")
            self.assertEqual(res.polls_ok, 2)  # confirmed on a second poll, not on the first
            self.assertEqual(seen, ["dead"])
            self.assertTrue(res.salvaged)
            self.assertTrue((Path(tmp) / "salvage_confirmed.json").is_file())

    def test_a_failed_salvage_writes_no_confirmed_marker(self):
        with tempfile.TemporaryDirectory() as tmp:
            colab = FakeColab(polls=lambda n, env: rec(alive=False))
            runner, _ = make(colab, tmp)
            res = runner.run(script="/content/x.sh", wall_s=3600, salvage=lambda why: False)
            self.assertFalse(res.salvaged)
            self.assertFalse((Path(tmp) / "salvage_confirmed.json").exists())

    def test_a_job_that_is_alive_again_resets_the_dead_count(self):
        def polls(n, env):
            if n == 1:
                return rec(alive=False)
            if n == 2:
                return rec(alive=True)
            return rec(rc=0)

        with tempfile.TemporaryDirectory() as tmp:
            res = make(FakeColab(polls=polls), tmp)[0].run(script="/content/x.sh", wall_s=3600)
            self.assertEqual(res.reason, "done")

    def test_setup_stall(self):
        with tempfile.TemporaryDirectory() as tmp:
            colab = FakeColab(polls=lambda n, env: rec(phase="setup", setup_bytes=500))
            runner, _ = make(colab, tmp)
            res = runner.run(script="/content/x.sh", wall_s=36000, setup_stall_s=600)
            self.assertEqual(res.reason, "setup_stall")
            self.assertIn("setup.log unchanged", res.detail)

    def test_growing_setup_log_is_not_a_stall(self):
        def polls(n, env):
            return rec(phase="setup", setup_bytes=n * 10, rc=0 if n >= 30 else None)

        with tempfile.TemporaryDirectory() as tmp:
            res = make(FakeColab(polls=polls), tmp)[0].run(script="/content/x.sh", wall_s=36000, setup_stall_s=600)
            self.assertEqual(res.reason, "done")

    def test_progress_stall(self):
        with tempfile.TemporaryDirectory() as tmp:
            colab = FakeColab(polls=lambda n, env: rec(phase="run", progress=7, units_done=1))
            runner, _ = make(colab, tmp)
            res = runner.run(script="/content/x.sh", wall_s=36000, progress_stall_s=900)
            self.assertEqual(res.reason, "progress_stall")

    def test_progress_that_keeps_moving_runs_to_done(self):
        def polls(n, env):
            return rec(phase="run", progress=n, rc=0 if n >= 40 else None)

        with tempfile.TemporaryDirectory() as tmp:
            res = make(FakeColab(polls=polls), tmp)[0].run(script="/content/x.sh", wall_s=36000, progress_stall_s=900)
            self.assertEqual((res.reason, res.rc), ("done", 0))

    def test_the_run_phase_gets_its_own_stall_clock(self):
        def polls(n, env):
            if n <= 12:  # twelve minutes of setup, with the log growing
                return rec(phase="setup", setup_bytes=n)
            return rec(phase="run", progress=1, rc=0 if n >= 14 else None)

        with tempfile.TemporaryDirectory() as tmp:
            res = make(FakeColab(polls=polls), tmp)[0].run(script="/content/x.sh", wall_s=36000, progress_stall_s=300)
            self.assertEqual(res.reason, "done")

    def test_wall(self):
        with tempfile.TemporaryDirectory() as tmp:
            colab = FakeColab(polls=lambda n, env: rec(progress=n))
            res = make(colab, tmp)[0].run(script="/content/x.sh", wall_s=600)
            self.assertEqual(res.reason, "wall")

    def test_nonzero_rc_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            res = make(FakeColab(polls=lambda n, env: rec(rc=3)), tmp)[0].run(script="/content/x.sh", wall_s=600)
            self.assertEqual((res.reason, res.rc), ("done", 3))

    def test_cap_check_ends_the_run(self):
        with tempfile.TemporaryDirectory() as tmp:
            colab = FakeColab(polls=lambda n, env: rec(progress=n))
            res = make(colab, tmp)[0].run(
                script="/content/x.sh", wall_s=36000, cap_check=lambda el: "CAP" if el > 100 else None
            )
            self.assertEqual((res.reason, res.detail), ("cap", "CAP"))


class ModesTest(unittest.TestCase):
    def test_hold_job_passes_its_env_and_needs_no_setup_log(self):
        def polls(n, env):
            return rec(phase=None, hold_beats=n, rc=0 if n >= 5 else None)

        with tempfile.TemporaryDirectory() as tmp:
            colab = FakeColab(polls=polls)
            res = make(colab, tmp)[0].run(hold_s=120, heartbeat_s=30, wall_s=36000, setup_stall_s=60)
            self.assertEqual(res.reason, "done")
            start_env = colab.envs[0]
            self.assertEqual((start_env["CR_MODE"], start_env["CR_HOLD_S"], start_env["CR_HEARTBEAT_S"]), ("hold", "120", "30"))

    def test_hold_job_whose_heartbeat_stops_is_a_progress_stall(self):
        with tempfile.TemporaryDirectory() as tmp:
            colab = FakeColab(polls=lambda n, env: rec(phase=None, hold_beats=3))
            res = make(colab, tmp)[0].run(hold_s=9999, wall_s=36000, progress_stall_s=300)
            self.assertEqual(res.reason, "progress_stall")

    def test_resident_mode_starts_the_busy_cell_and_polls_by_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            colab = FakeColab(vm_poll_file=rec(rc=0))
            res = make(colab, tmp)[0].run(script="/content/x.sh", wall_s=600, resident=True)
            self.assertEqual(res.reason, "done")
            self.assertEqual(colab.poll_count, 0)  # no exec polls: a busy kernel would queue them
            self.assertEqual(colab.resident_calls, 1)

    def test_resident_poll_that_cannot_be_downloaded_is_a_failed_poll(self):
        with tempfile.TemporaryDirectory() as tmp:
            colab = FakeColab(vm_poll_file=None)
            res = make(colab, tmp)[0].run(script="/content/x.sh", wall_s=300, resident=True)
            self.assertEqual(res.reason, "wall")
            self.assertGreater(res.polls_failed, 0)

    def test_resident_poll_with_a_torn_file_is_a_failed_poll(self):
        with tempfile.TemporaryDirectory() as tmp:
            runner, _ = make(FakeColab(vm_poll_file="{torn"), tmp)
            self.assertIsNone(runner.poll_resident())


class StopTest(unittest.TestCase):
    def test_stop_marker_only_after_a_confirmed_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            colab = FakeColab(stops=[1, 1, 1])
            runner, _ = make(colab, tmp)
            self.assertFalse(runner.stop_session(attempts=3))
            self.assertFalse((Path(tmp) / "stop_confirmed.json").exists())
            pending = json.loads((Path(tmp) / "stop_pending.json").read_text())
            self.assertEqual(pending["attempts"], 3)
            # a later retry finds nothing confirmed, stops, and only then writes the marker and clears the pending file
            colab.stop_results = [0]
            self.assertTrue(runner.stop_session(attempts=3))
            self.assertTrue((Path(tmp) / "stop_confirmed.json").is_file())
            self.assertFalse((Path(tmp) / "stop_pending.json").exists())

    def test_stop_that_succeeds_on_the_second_attempt(self):
        with tempfile.TemporaryDirectory() as tmp:
            colab = FakeColab(stops=[1, 0])
            runner, _ = make(colab, tmp)
            self.assertTrue(runner.stop_session())
            self.assertEqual(colab.stop_calls, 2)


class CellsTest(unittest.TestCase):
    def test_every_cell_is_one_compilable_source(self):
        for name in ("start", "poll", "resident"):
            compile(cell_source(name), name, "exec")


class SpendTest(unittest.TestCase):
    CPU_RATE, CAP = 0.26, 0.9
    NEIGHBOUR_RATE = 2 * 10.05  # two fake G4 sessions billing beside the CPU window

    def scenario(self, guard):
        """A CPU stub window runs two hours beside two G4 neighbours; returns why the run ended."""
        def polls(n, env):
            return rec(phase="run", progress=n)

        with tempfile.TemporaryDirectory() as tmp:
            colab = FakeColab(polls=polls)
            runner, _ = make(colab, tmp)
            res = runner.run(script="/content/stub.sh", wall_s=7200, cap_check=guard.check)
            return res.reason

    def test_cpu_stub_stays_under_its_cap_beside_g4_neighbours_and_runs_to_its_wall(self):
        guard = SpendGuard(rate_per_h=self.CPU_RATE, cap=self.CAP)
        self.assertEqual(self.scenario(guard), "wall")
        # the balance fell by about 40 in those two hours: a record only
        rec_ = guard.record_balance(1800.0, 1800.0 - self.NEIGHBOUR_RATE * 2)
        self.assertFalse(rec_["used_for_pricing"])
        self.assertAlmostEqual(guard.spend(7200), 0.52)

    def test_mutation_pricing_a_cpu_window_from_the_balance_drop_makes_the_scenario_trip(self):
        neighbour_rate = self.NEIGHBOUR_RATE

        class BalanceDropGuard(SpendGuard):  # the old behaviour: max(balance drop, own rate x elapsed)
            def spend(self, elapsed_s):
                return max(neighbour_rate * elapsed_s / 3600.0, super().spend(elapsed_s))

        mutant = BalanceDropGuard(rate_per_h=self.CPU_RATE, cap=self.CAP)
        self.assertEqual(self.scenario(mutant), "cap")  # so the real guard's "wall" above is a real check

    def test_attribute_rate(self):
        self.assertEqual(
            attribute_rate(pre_open_assignments=2, assignments_now=3, account_rate_now=20.3, baseline_rate=20.04, max_rate=0.3),
            (0.26, "attributed", 2),
        )
        rate, basis, others = attribute_rate(
            pre_open_assignments=2, assignments_now=4, account_rate_now=30.0, baseline_rate=20.0, max_rate=0.3
        )
        self.assertEqual((rate, basis, others), (0.3, "max_rate_x_elapsed", 3))
        self.assertEqual(
            attribute_rate(pre_open_assignments=0, assignments_now=1, account_rate_now=0.0, baseline_rate=0.0, max_rate=0.3)[1],
            "max_rate_x_elapsed",
        )

    def test_neighbour_warning(self):
        self.assertIsNone(neighbour_warning(1))
        self.assertIsNone(neighbour_warning(0))
        self.assertIn("2 other assignment", neighbour_warning(3))

    def test_guard_trips_on_its_own_rate_and_reserve(self):
        guard = SpendGuard(rate_per_h=10.0, cap=5.0, margin=0.5, cleanup_reserve=1.5)
        self.assertEqual(guard.ceiling, 3.0)
        self.assertIsNone(guard.check(1000))
        self.assertIn("CAP_GUARD_TRIPPED", guard.check(1100))


class CliEnvTest(unittest.TestCase):
    def test_cli_from_env(self):
        import os
        from unittest import mock

        from colab_runner.colab_cli import cli_from_env

        with mock.patch.dict(os.environ, {"COLAB_RUNNER_CLI": ""}):
            self.assertEqual(cli_from_env().argv, ["colab"])
        with mock.patch.dict(os.environ, {"COLAB_RUNNER_CLI": "python fake.py"}):
            self.assertEqual(cli_from_env().argv, ["python", "fake.py"])


if __name__ == "__main__":
    unittest.main()

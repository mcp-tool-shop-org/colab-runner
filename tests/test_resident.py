"""Item 5: the resident fallback verifies its start, kills its background `colab exec` on every exit, collects the
job's run log, and a sustained poll blackout trips `unreachable` (both modes), which goes through the stop."""

import sys
import tempfile
import textwrap
import time
import unittest
from pathlib import Path

from colab_runner.colab_cli import TIMEOUT_RC, Colab, CliResult
from tests.fakes import FakeColab, guard, make, rec


class ResidentRunTest(unittest.TestCase):
    def run_resident(self, colab, tmp, **kw):
        runner, clock = make(colab, tmp)
        kw.setdefault("wall_s", 36000)
        res = runner.run(script="/content/x.sh", resident=True, **kw)
        return runner, res

    def test_a_resident_start_whose_poll_file_never_appears_is_start_failed(self):
        with tempfile.TemporaryDirectory() as tmp:
            # the background exec fails at once and the poll file never shows up on the VM
            colab = FakeColab(vm_poll_file=None, resident_result=CliResult(1, "", "RuntimeError: Connection was lost"),
                              resident_done_at_start=True)
            runner, res = self.run_resident(colab, tmp, setup_stall_s=600)
            self.assertEqual(res.reason, "start_failed")
            self.assertIn("did not appear within 600s", res.detail)
            self.assertAlmostEqual(res.elapsed_s, 600, delta=1e-6)  # the setup window, not the wall
            self.assertEqual(colab.stops, [("sess", 0)])
            tail = [e for e in colab.events if e in ("restart", "kill", "stop")]
            self.assertEqual(tail, ["restart", "kill", "stop"])  # the kernel freed, the job killed, then the stop

    def test_an_early_exit_kills_the_background_child(self):
        with tempfile.TemporaryDirectory() as tmp:
            colab = FakeColab(vm_poll_file=lambda n: rec(progress=n))  # a job that keeps running
            g = guard(max_rate=3.6, cap=2.0)  # ceiling 1.0 unit after the 1000 s reserve: 1000 s at 3.6/h
            runner, res = self.run_resident(colab, tmp, guard=g)
            self.assertEqual(res.reason, "cap")
            self.assertTrue(colab.resident_call.killed)
            self.assertEqual(colab.resident_call.result.rc, 124)
            self.assertEqual(colab.stops, [("sess", 0)])

    def test_the_background_child_is_reaped_or_killed_on_every_exit(self):
        cases = {
            "done": {"vm_poll_file": rec(rc=0)},
            "failed": {"vm_poll_file": rec(rc=2)},
            "progress_stall": {"vm_poll_file": rec(progress=1)},
            "dead": {"vm_poll_file": rec(alive=False)},
            "unreachable": {"vm_poll_file": lambda n: rec() if n == 1 else None},
        }
        for reason, kw in cases.items():
            with self.subTest(reason=reason), tempfile.TemporaryDirectory() as tmp:
                colab = FakeColab(**kw)
                runner, res = self.run_resident(colab, tmp)
                self.assertEqual(res.reason, reason)
                self.assertGreaterEqual(colab.resident_call.kill_calls, 1)
                self.assertIsNotNone(colab.resident_call.result)  # nothing is left running on the rig
                self.assertEqual(colab.resident_call.killed, reason not in ("done", "failed"))
                self.assertEqual(colab.stops, [("sess", 0)])

    def test_a_poll_blackout_trips_unreachable_in_resident_mode(self):
        with tempfile.TemporaryDirectory() as tmp:
            colab = FakeColab(vm_poll_file=lambda n: rec(progress=n) if n == 1 else None)
            runner, res = self.run_resident(colab, tmp, unreachable_polls=3)
            self.assertEqual(res.reason, "unreachable")
            self.assertEqual(res.max_consecutive_failed, 3)
            self.assertEqual(colab.stops, [("sess", 0)])

    def test_resident_mode_collects_the_job_run_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            colab = FakeColab(vm_poll_file=rec(rc=0), run_log="first line\nsecond line\n")
            runner, res = self.run_resident(colab, tmp)
            self.assertEqual(res.reason, "done")
            self.assertEqual((runner.out_dir / "job.run.out").read_text(), "first line\nsecond line\n")
            self.assertEqual(colab.events.count("download"), 2)  # the poll file, then the log, before the stop

    def test_resident_mode_collects_the_log_after_a_trip_too(self):
        with tempfile.TemporaryDirectory() as tmp:
            colab = FakeColab(vm_poll_file=rec(progress=1), run_log="partial\n")
            runner, res = self.run_resident(colab, tmp, progress_stall_s=300)
            self.assertEqual(res.reason, "progress_stall")
            self.assertEqual((runner.out_dir / "job.run.out").read_text(), "partial\n")
            tail = [e for e in colab.events if e in ("restart", "kill", "download", "stop")][-4:]
            self.assertEqual(tail, ["restart", "kill", "download", "stop"])  # killed, then the log, then stopped

    def test_an_oom_line_in_a_log_collected_at_the_end_still_makes_it_oom(self):
        with tempfile.TemporaryDirectory() as tmp:
            colab = FakeColab(vm_poll_file=rec(rc=0))  # the log download fails inside the window...

            def hook(kind, fake):
                if kind == "download" and fake.events.count("download") == 3:  # ...and works in the cleanup
                    fake.run_log = "Out of memory: Killed process 9 (x)\n"

            colab.on_call = hook
            got = []
            runner, res = self.run_resident(colab, tmp, salvage=lambda why: got.append(why) or True)
            self.assertEqual((res.reason, got), ("oom", ["oom"]))

    def test_an_oom_line_in_the_resident_log_is_oom(self):
        with tempfile.TemporaryDirectory() as tmp:
            colab = FakeColab(vm_poll_file=rec(rc=0), run_log="ok\nOut of memory: Killed process 77 (python3)\n")
            got = []
            runner, res = self.run_resident(colab, tmp, salvage=lambda why: got.append(why) or True)
            self.assertEqual((res.reason, got), ("oom", ["oom"]))


class BlackoutTest(unittest.TestCase):
    def test_a_poll_blackout_trips_unreachable_by_count(self):
        with tempfile.TemporaryDirectory() as tmp:
            colab = FakeColab(polls=lambda n, env: rec(progress=n) if n == 1 else None)
            runner, _ = make(colab, tmp)
            res = runner.run(script="/content/x.sh", wall_s=36000, unreachable_polls=4, unreachable_s=1e9)
            self.assertEqual((res.reason, res.polls_failed), ("unreachable", 4))
            self.assertEqual(colab.stops, [("sess", 0)])

    def test_a_poll_blackout_trips_unreachable_by_time(self):
        with tempfile.TemporaryDirectory() as tmp:
            colab = FakeColab(polls=lambda n, env: rec(progress=n) if n == 1 else None)
            runner, _ = make(colab, tmp)
            res = runner.run(script="/content/x.sh", wall_s=36000, unreachable_polls=1000, unreachable_s=900)
            self.assertEqual(res.reason, "unreachable")
            self.assertIn("without a good poll", res.detail)
            self.assertLess(res.elapsed_s, 900 + 300 + 60)  # within one backoff step of the 900 s window
            self.assertEqual(colab.stops, [("sess", 0)])

    def test_a_good_poll_resets_the_blackout(self):
        with tempfile.TemporaryDirectory() as tmp:
            colab = FakeColab(polls=lambda n, env: rec(progress=n, rc=0 if n >= 12 else None) if n % 3 == 0 else None)
            runner, _ = make(colab, tmp)
            res = runner.run(script="/content/x.sh", wall_s=36000, unreachable_polls=3)
            self.assertEqual(res.reason, "done")


class ResidentTimeoutTest(unittest.TestCase):
    """Fix 2, item 6: the resident exec's `--timeout` (kernel_timeout of its bound) never ends before the cell's
    CR_RESIDENT_MAX_S, for windows short and long, and a job that ends at the window's last second is still done."""

    def test_the_exec_timeout_is_never_shorter_than_the_resident_max(self):
        from colab_runner.colab_cli import kernel_timeout

        for wall_s in (2.0, 9.5, 29.0, 31.0, 600.0, 36000.0):
            with self.subTest(wall_s=wall_s), tempfile.TemporaryDirectory() as tmp:
                colab = FakeColab(vm_poll_file=rec(rc=0))
                runner, _ = make(colab, tmp)
                runner.run(script="/content/x.sh", wall_s=wall_s, resident=True)
                env = next(e for e in colab.envs if "CR_RESIDENT_MAX_S" in e)
                call = next(c for c in colab.calls if c["kind"] == "resident")
                self.assertGreaterEqual(kernel_timeout(call["timeout"]), int(env["CR_RESIDENT_MAX_S"]))
                self.assertEqual(int(env["CR_RESIDENT_MAX_S"]), max(1, int(wall_s)))

    def test_a_caller_resident_max_gets_the_report_time_too(self):
        from colab_runner.colab_cli import kernel_timeout

        with tempfile.TemporaryDirectory() as tmp:
            colab = FakeColab(vm_poll_file=rec(rc=0))
            runner, _ = make(colab, tmp)
            runner.run(script="/content/x.sh", wall_s=600, resident=True, env={"CR_RESIDENT_MAX_S": "120"})
            call = next(c for c in colab.calls if c["kind"] == "resident")
            self.assertGreaterEqual(kernel_timeout(call["timeout"]), 120)
        with tempfile.TemporaryDirectory() as tmp:  # a value that is not a number: the window's own end
            colab = FakeColab(vm_poll_file=rec(rc=0))
            runner, _ = make(colab, tmp)
            runner.run(script="/content/x.sh", wall_s=600, resident=True, env={"CR_RESIDENT_MAX_S": "soon"})
            call = next(c for c in colab.calls if c["kind"] == "resident")
            self.assertGreaterEqual(kernel_timeout(call["timeout"]), 600)

    def test_a_job_that_ends_in_the_last_seconds_of_the_window_is_done(self):
        from colab_runner.colab_cli import kernel_timeout

        def poll_file(n):
            # the VM: the resident cell rewrites the poll file only while its exec's --timeout has not run out;
            # after that the file stays frozen at its last write. The job ends 590 s into the 600 s window.
            call = next(c for c in colab.calls if c["kind"] == "resident")
            cell_alive_until = call["at"] + kernel_timeout(call["timeout"])
            now = min(colab.clock.now(), cell_alive_until)
            return rec(rc=0) if now - 1000.0 >= 590 else rec(progress=int(now))

        with tempfile.TemporaryDirectory() as tmp:
            colab = FakeColab(vm_poll_file=poll_file)
            runner, _ = make(colab, tmp, poll_s=10)
            res = runner.run(script="/content/x.sh", wall_s=600, resident=True)
            self.assertEqual(res.reason, "done")
            self.assertFalse(colab.resident_call.killed)  # the cell ended on its own, with the job


FAKE_EXEC = textwrap.dedent("""
    import sys, time
    mode = sys.argv[1]
    print("CR_STARTED pid=1", flush=True)
    print("access_token=" + "Q" * 24, file=sys.stderr, flush=True)
    if mode == "hang":
        time.sleep(60)
    print("CR_RESIDENT_DONE", flush=True)
""")


class BackgroundCallTest(unittest.TestCase):
    """The real wrapper's background call (Colab.start_cell), against a small fake CLI."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.script = Path(self._tmp.name) / "fake_exec.py"
        self.script.write_text(FAKE_EXEC)

    def colab(self, mode):
        return Colab([sys.executable, str(self.script), mode])

    def test_a_call_that_ends_is_reaped_with_its_output_scrubbed_and_its_cell_file_removed(self):
        call = self.colab("quick").start_cell("s", "print(1)", {"K": "v"}, timeout=60)
        folder = Path(call._tmp.name)
        self.assertTrue((folder / "cell.py").is_file())
        end = time.monotonic() + 30
        while call.poll() is None and time.monotonic() < end:
            time.sleep(0.05)
        self.assertEqual(call.result.rc, 0)
        self.assertIn("CR_RESIDENT_DONE", call.result.out)
        self.assertNotIn("Q" * 24, call.result.err)
        self.assertFalse(call.killed)
        self.assertFalse(folder.exists())
        self.assertIs(call.kill(), call.result)  # killing an ended call only returns its result

    def test_kill_on_a_call_that_ended_unpolled_only_reaps_it(self):
        call = self.colab("quick").start_cell("s", "print(1)", None, timeout=60)
        call.proc.wait(timeout=30)
        res = call.kill()
        self.assertEqual(res.rc, 0)
        self.assertFalse(call.killed)

    def test_kill_ends_a_running_call(self):
        call = self.colab("hang").start_cell("s", "print(1)", None, timeout=60)
        self.assertIsNone(call.poll())
        t = time.monotonic()
        res = call.kill()
        self.assertLess(time.monotonic() - t, 10)
        self.assertTrue(call.killed)
        self.assertNotEqual(res.rc, 0)
        self.assertFalse(Path(call._tmp.name).exists())

    def test_a_call_past_its_deadline_is_killed_by_poll(self):
        call = self.colab("hang").start_cell("s", "print(1)", None, timeout=0.5)
        time.sleep(0.8)
        res = call.poll()
        self.assertIsNotNone(res)
        self.assertEqual(res.rc, TIMEOUT_RC)
        self.assertTrue(call.killed)

    def test_a_cli_that_cannot_start_is_a_finished_call(self):
        call = Colab([str(Path(self._tmp.name) / "no-such-cli")]).start_cell("s", "print(1)", None, timeout=5)
        self.assertEqual(call.poll().rc, 127)
        self.assertEqual(call.kill().rc, 127)


if __name__ == "__main__":
    unittest.main()

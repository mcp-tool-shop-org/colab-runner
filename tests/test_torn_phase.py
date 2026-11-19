"""0.1.2: a torn phase.txt read after setup never falls back to setup, and a genuine return resets the setup clock."""

import tempfile
import unittest

from tests.fakes import FakeColab, make, rec


class TornPhaseReadTest(unittest.TestCase):
    def test_torn_read_during_run_does_not_end_setup_stall(self):
        def make_poll(torn_value):
            def poll(n):
                if n <= 3:
                    return rec(phase="setup", setup_bytes=n * 100)
                if n == 16:
                    return rec(phase=torn_value, progress=n, setup_bytes=300)
                return rec(phase="run", progress=n, setup_bytes=300, rc=0 if n == 18 else None)

            return poll

        for torn_value in ("", None):
            for resident in (False, True):
                with self.subTest(torn_value=torn_value, resident=resident), tempfile.TemporaryDirectory() as tmp:
                    poll_fn = make_poll(torn_value)
                    if resident:
                        colab = FakeColab(vm_poll_file=poll_fn)
                    else:
                        colab = FakeColab(polls=lambda n, env, fn=poll_fn: fn(n))
                    runner, _ = make(colab, tmp)
                    res = runner.run(script="/content/x.sh", wall_s=36000, setup_stall_s=600, resident=resident)
                    self.assertEqual((res.reason, res.rc, res.polls_ok), ("done", 0, 18))

    def test_torn_read_during_run_preserves_progress_stall(self):
        def polls(n, env):
            if n <= 2:
                return rec(phase="setup", setup_bytes=n * 100)
            if n == 3:
                return rec(phase="run", progress=5, setup_bytes=200)
            return rec(phase="", progress=5, setup_bytes=200)

        with tempfile.TemporaryDirectory() as tmp:
            colab = FakeColab(polls=polls)
            runner, _ = make(colab, tmp)
            res = runner.run(script="/content/x.sh", wall_s=36000, setup_stall_s=120, progress_stall_s=300)
            self.assertEqual(res.reason, "progress_stall")
            self.assertEqual(res.elapsed_s, 420.0)

    def test_torn_read_during_setup_keeps_setup_behaviour(self):
        def stalled_polls(n, env):
            phases = {1: "setup", 2: "", 3: None, 4: "setup", 5: "", 6: None}
            return rec(phase=phases.get(n, "setup"), setup_bytes=500)

        with self.subTest(case="stalled_setup"), tempfile.TemporaryDirectory() as tmp:
            colab = FakeColab(polls=stalled_polls)
            runner, _ = make(colab, tmp)
            res = runner.run(script="/content/x.sh", wall_s=36000, setup_stall_s=300, progress_stall_s=900)
            self.assertEqual(res.reason, "setup_stall")
            self.assertEqual(res.elapsed_s, 300.0)
            self.assertEqual(res.polls_ok, 6)
            self.assertIn("setup.log unchanged for 300s", res.detail)

        def growing_polls(n, env):
            phases = {1: None, 2: "", 3: "setup", 4: "", 5: "run", 6: "run"}
            return rec(
                phase=phases[n],
                setup_bytes=n * 50,
                progress=max(0, n - 4),
                rc=0 if n == 6 else None,
            )

        with self.subTest(case="growing_setup"), tempfile.TemporaryDirectory() as tmp:
            colab = FakeColab(polls=growing_polls)
            runner, _ = make(colab, tmp)
            res = runner.run(script="/content/x.sh", wall_s=36000, setup_stall_s=300)
            self.assertEqual((res.reason, res.rc), ("done", 0))

    def test_genuine_return_to_setup_after_run_resets_setup_clock(self):
        def recovers_polls(n, env):
            if n <= 2:
                return rec(phase="setup", setup_bytes=500)
            if n <= 15:
                return rec(phase="run", progress=n, setup_bytes=500)
            if n <= 17:
                return rec(phase="setup", setup_bytes=500)
            if n == 18:
                return rec(phase="setup", setup_bytes=600)
            return rec(phase="run", progress=20, setup_bytes=600, rc=0)

        with self.subTest(case="recovers_after_reentry"), tempfile.TemporaryDirectory() as tmp:
            colab = FakeColab(polls=recovers_polls)
            runner, _ = make(colab, tmp)
            res = runner.run(script="/content/x.sh", wall_s=36000, setup_stall_s=300)
            self.assertEqual((res.reason, res.rc), ("done", 0))

        def stalls_after_reentry_polls(n, env):
            if n <= 2:
                return rec(phase="setup", setup_bytes=500)
            if n <= 15:
                return rec(phase="run", progress=n, setup_bytes=500)
            if n == 17:
                return rec(phase="", setup_bytes=500)
            return rec(phase="setup", setup_bytes=500)

        with self.subTest(case="stalls_after_full_window_from_reentry"), tempfile.TemporaryDirectory() as tmp:
            colab = FakeColab(polls=stalls_after_reentry_polls)
            runner, _ = make(colab, tmp)
            res = runner.run(script="/content/x.sh", wall_s=36000, setup_stall_s=300)
            self.assertEqual(res.reason, "setup_stall")
            self.assertEqual(res.polls_ok, 21)
            self.assertEqual(res.elapsed_s, 1200.0)
            self.assertIn("setup.log unchanged for 300s", res.detail)


if __name__ == "__main__":
    unittest.main()

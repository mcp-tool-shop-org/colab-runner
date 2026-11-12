"""The VM cells and the runner against a fake `colab` CLI that runs the cells locally. POSIX only (the cells use
setsid/nohup/bash, as the VM does)."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

from colab_runner.colab_cli import Colab
from colab_runner.runner import Runner, cell_source

POSIX = os.name == "posix"
FAKE = str(Path(__file__).with_name("fake_colab.py"))


def run_cell(name, env, timeout=60):
    full = dict(os.environ)
    full.update(env)
    proc = subprocess.run([sys.executable, "-c", cell_source(name)], env=full, capture_output=True, text=True, timeout=timeout)
    return proc


def wait_for(path, secs=20):
    end = time.time() + secs
    while time.time() < end:
        if Path(path).exists():
            return True
        time.sleep(0.05)
    return False


@unittest.skipUnless(POSIX, "the cells run on a POSIX VM")
class VmCellTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.vm = self._tmp.name
        self.env = {"CR_CONTENT": self.vm, "CR_JOB": "j"}
        self.addCleanup(self._tmp.cleanup)

    def script(self, body):
        p = Path(self.vm) / "job.sh"
        p.write_text(body)
        return {"CR_MODE": "script", "CR_SCRIPT": str(p)}

    def poll(self, offset=0):
        proc = run_cell("poll", dict(self.env, CR_POLL_LOG_OFFSET=str(offset)))
        rec, logs = None, []
        for ln in proc.stdout.splitlines():
            if ln.startswith("CR_POLL "):
                rec = json.loads(ln[8:])
            if ln.startswith("CR_LOG "):
                logs.append(ln[7:])
        return rec, logs

    def test_start_runs_the_job_detached_and_writes_the_status_after_it_ends(self):
        env = dict(self.env, **self.script("echo hello; sleep 0.3; exit 3\n"))
        out = run_cell("start", env).stdout
        self.assertIn("CR_STARTED", out)
        self.assertTrue(wait_for(Path(self.vm) / "j.status.json"))
        rec, logs = self.poll()
        self.assertEqual(rec["rc"], 3)
        self.assertIn("hello", logs)
        self.assertFalse(rec["alive"])

    def test_second_start_never_launches_a_second_job(self):
        env = dict(self.env, **self.script("sleep 2\n"))
        first = run_cell("start", env).stdout
        second = run_cell("start", env).stdout
        self.assertIn("CR_STARTED", first)
        self.assertIn("CR_ALREADY_RUNNING", second)
        pid = int((Path(self.vm) / "j.pid").read_text())
        self.assertIn("pid=%d" % pid, second)
        # also after the job has ended: a finished job is not restarted by a retried start
        self.assertTrue(wait_for(Path(self.vm) / "j.status.json"))
        self.assertIn("CR_ALREADY_RUNNING", run_cell("start", env).stdout)

    def test_a_stale_pid_without_status_is_restarted(self):
        (Path(self.vm) / "j.pid").write_text("999999\n")
        env = dict(self.env, **self.script("exit 0\n"))
        self.assertIn("CR_STARTED", run_cell("start", env).stdout)

    def test_poll_reports_phase_progress_units_setup_bytes_and_log_chunks(self):
        out = Path(self.vm) / "j_out"
        (out / "progress").mkdir(parents=True)
        (out / "phase.txt").write_text("run\n")
        (out / "remote_step.txt").write_text("translate\n")
        (out / "setup.log").write_text("x" * 42)
        (out / "progress" / "a").write_text("5\n")
        (out / "progress" / "b").write_text("segments 7 done\n")
        units = Path(self.vm) / "summaries"
        units.mkdir()
        (units / "s1.json").write_text("{}")
        (units / "s2.json").write_text("{}")
        (Path(self.vm) / "j.run.out").write_text("line one\nline two with hf_" + "A" * 30 + "\n")
        env = dict(self.env, CR_UNITS_GLOB=str(units / "*.json"))
        proc = run_cell("poll", dict(env, CR_POLL_LOG_OFFSET="0"))
        rec = json.loads([ln for ln in proc.stdout.splitlines() if ln.startswith("CR_POLL ")][0][8:])
        self.assertEqual((rec["phase"], rec["step"], rec["setup_bytes"]), ("run", "translate", 42))
        self.assertEqual((rec["progress"], rec["units_done"]), (12, 2))
        self.assertFalse(rec["alive"])
        self.assertIsNone(rec["rc"])
        self.assertIn("line one", proc.stdout)
        self.assertNotIn("A" * 30, proc.stdout)  # a token in the job log never leaves the VM
        self.assertIn("hf_[REDACTED]", proc.stdout)
        # the offset resumes after what was read; an offset past the end restarts from zero
        again = run_cell("poll", dict(env, CR_POLL_LOG_OFFSET=str(rec["log_offset"])))
        self.assertNotIn("line one", again.stdout)
        past = run_cell("poll", dict(env, CR_POLL_LOG_OFFSET="99999"))
        self.assertIn("line one", past.stdout)

    def test_large_log_is_cut_at_a_line_boundary(self):
        (Path(self.vm) / "j.run.out").write_text(("x" * 99 + "\n") * 1000)
        rec, logs = self.poll(0)
        self.assertFalse(rec["log_eof"])
        self.assertEqual(rec["log_offset"] % 100, 0)
        self.assertTrue(all(len(ln) == 99 for ln in logs))

    def test_hold_job_writes_heartbeats_and_ends_with_rc_0(self):
        env = dict(self.env, CR_MODE="hold", CR_HOLD_S="0.4", CR_HEARTBEAT_S="0.1")
        self.assertIn("CR_STARTED", run_cell("start", env).stdout)
        self.assertTrue(wait_for(Path(self.vm) / "j.status.json"))
        rec, _ = self.poll()
        self.assertEqual(rec["rc"], 0)
        self.assertGreaterEqual(rec["hold_beats"], 3)
        self.assertIsNotNone(rec["hold_heartbeat_epoch"])

    def test_resident_cell_stays_busy_until_the_job_ends_and_writes_the_poll_file(self):
        env = dict(self.env, **self.script("sleep 0.5\n"), CR_POLL_EVERY_S="0.1")
        proc = run_cell("resident", env)
        self.assertIn("CR_STARTED", proc.stdout)
        self.assertIn("CR_RESIDENT_DONE", proc.stdout)
        self.assertGreaterEqual(proc.stdout.count("CR_RESIDENT_BEAT"), 3)
        final = json.loads((Path(self.vm) / "j.poll.json").read_text())
        self.assertEqual(final["rc"], 0)

    def test_resident_cell_gives_up_at_its_max(self):
        env = dict(self.env, **self.script("sleep 5\n"), CR_POLL_EVERY_S="0.1", CR_RESIDENT_MAX_S="0.3")
        proc = run_cell("resident", env)
        self.assertIn("CR_RESIDENT_DONE", proc.stdout)
        self.addCleanup(lambda: subprocess.run(["pkill", "-f", "j.status.json"], check=False))


@unittest.skipUnless(POSIX, "the cells run on a POSIX VM")
class EndToEndTest(unittest.TestCase):
    """The runner, the Colab wrapper and the cells, through a fake `colab` that loses replies and calls."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        root = Path(self._tmp.name)
        self.vm, self.out, self.state = root / "vm", root / "out", root / "state"
        self.vm.mkdir()
        self.faults = root / "faults.json"
        env = dict(os.environ, FAKE_COLAB_STATE=str(self.state), FAKE_COLAB_VM=str(self.vm), FAKE_COLAB_FAULTS=str(self.faults))
        self.colab = Colab([sys.executable, FAKE], env=env)

    def runner(self, **kw):
        return Runner(self.colab, "s", self.out, content=str(self.vm), poll_s=0.2, poll_timeout_s=10, backoff_max_s=0.4,
                      start_attempts=4, **kw)

    def job(self, body):
        p = self.vm / "job.sh"
        p.write_text(body)
        return str(p)

    def test_lost_start_reply_and_lost_polls_still_complete_with_one_job(self):
        self.faults.write_text(json.dumps({"lose_reply": {"exec": [1]}, "drop_call": {"exec": [3, 4, 5, 6]}}))
        script = self.job("mkdir -p $CR_OUT_DIR/progress; echo run > $CR_OUT_DIR/phase.txt; echo 3 > $CR_OUT_DIR/progress/a; "
                          "sleep 1; echo done-line\n")
        res = self.runner().run(script=script, env={"CR_OUT_DIR": str(self.vm / "j_out")}, wall_s=120)
        self.assertEqual((res.reason, res.rc), ("done", 0))
        self.assertGreaterEqual(res.max_consecutive_failed, 1)
        calls = (self.state / "calls.log").read_text()
        self.assertGreaterEqual(calls.count("exec"), 5)
        self.assertEqual(len(list(self.vm.glob("*.pid"))), 1)
        self.assertIn("done-line", (self.out / "job.run.out").read_text())

    def test_a_hung_poll_is_killed_by_its_timeout_and_counts_as_a_failed_poll(self):
        self.faults.write_text(json.dumps({"hang": {"exec": [2]}}))
        script = self.job("sleep 1\n")
        runner = Runner(self.colab, "s", self.out, content=str(self.vm), poll_s=0.2, poll_timeout_s=1, backoff_max_s=0.3)
        res = runner.run(script=script, wall_s=120)
        self.assertEqual(res.reason, "done")
        self.assertGreaterEqual(res.polls_failed, 1)

    def test_dead_job_is_found_and_salvaged(self):
        script = self.job("echo started\nkill -9 $$\n")
        got = []
        res = self.runner().run(script=script, wall_s=120, salvage=lambda why: got.append(why) or True)
        # the wrapper survives its child's kill and writes a status with rc 137: a done job, not a dead one
        self.assertEqual(res.reason, "done")
        self.assertEqual(got, [])
        self.assertNotEqual(res.rc, 0)

    def test_hold_job_end_to_end(self):
        res = self.runner().run(hold_s=0.6, heartbeat_s=0.1, wall_s=120, progress_stall_s=30)
        self.assertEqual((res.reason, res.rc), ("done", 0))
        self.assertGreaterEqual(res.last["hold_beats"], 4)

    def test_stop_goes_through_the_cli_and_a_failed_stop_leaves_it_pending(self):
        self.faults.write_text(json.dumps({"fail": {"stop": [1, 2, 3]}}))
        runner = self.runner()
        self.assertFalse(runner.stop_session(attempts=3))
        self.assertTrue((self.out / "stop_pending.json").is_file())
        self.assertTrue(runner.stop_session(attempts=1))
        self.assertTrue((self.out / "stop_confirmed.json").is_file())

    def test_download_and_unknown_subcommand_and_missing_cli(self):
        self.assertFalse(self.colab.download("s", "/content/none.json", str(self.out / "x")).ok)
        (self.vm / "f.json").write_text("{}")
        self.out.mkdir(exist_ok=True)
        self.assertTrue(self.colab.download("s", "/content/f.json", str(self.out / "f.json")).ok)
        self.assertEqual(self.colab.run(["nope"], 10).rc, 2)
        self.assertEqual(self.colab.restart_kernel("s").rc, 2)
        self.assertEqual(self.colab.usage().rc, 2)
        self.assertEqual(Colab(["/no/such/colab"]).run(["x"], 5).rc, 127)

    def test_cli_entry_point_runs_a_hold_job(self):
        env = dict(os.environ, FAKE_COLAB_STATE=str(self.state), FAKE_COLAB_VM=str(self.vm),
                   COLAB_RUNNER_CLI="%s %s" % (sys.executable, FAKE))
        proc = subprocess.run(
            [sys.executable, "-m", "colab_runner", "run", "--session", "s", "--out-dir", str(self.out), "--content", str(self.vm),
             "--hold-s", "0.4", "--heartbeat-s", "0.1", "--wall-s", "60", "--poll-s", "0.2", "--max-rate", "0.26", "--cap", "0.9",
             "--env", "X=1"],
            env=env, capture_output=True, text=True, timeout=120,
        )
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("RUN_END reason=done rc=0", proc.stdout)


if __name__ == "__main__":
    unittest.main()

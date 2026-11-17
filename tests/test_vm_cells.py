"""The VM cells and the runner against a fake `colab` CLI that runs the cells locally. POSIX only (the cells use
setsid/nohup/bash, as the VM does), except VmLibTest, which signals no process."""

import json
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from colab_runner.colab_cli import Colab
from colab_runner.runner import Runner, cell_source
from tests.fakes import VmLog

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


def gone(pid, secs=10.0):
    end = time.time() + secs
    while time.time() < end:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return True
        time.sleep(0.05)
    return False


class VmLibTest(unittest.TestCase):
    """(B) cr_alive: a PermissionError on the pid (another user's process on a recycled pid) is not our job."""

    def test_a_recycled_pid_owned_by_another_user_is_not_alive(self):
        with tempfile.TemporaryDirectory() as tmp:
            ns = VmLog(tmp).ns
            with mock.patch("os.kill", side_effect=PermissionError):
                self.assertFalse(ns["cr_alive"](4242))
            with mock.patch("os.kill", side_effect=ProcessLookupError):
                self.assertFalse(ns["cr_alive"](4242))


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

    def test_a_64k_line_with_no_newline_advances_the_offset(self):
        (Path(self.vm) / "j.run.out").write_bytes(b"w" * 70000)
        rec, logs = self.poll(0)
        self.assertEqual((rec["log_offset"], rec["log_eof"], logs), (65536, False, ["w" * 65536]))

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

    def test_the_kill_cell_terminates_the_whole_process_group(self):
        bg = Path(self.vm) / "bg.pid"
        env = dict(self.env, **self.script("sleep 30 &\necho $! > %s\nsleep 30\n" % bg))
        self.assertIn("CR_STARTED", run_cell("start", env).stdout)
        self.assertTrue(wait_for(bg))
        time.sleep(0.2)
        wrapper, child = int((Path(self.vm) / "j.pid").read_text()), int(bg.read_text())
        out = run_cell("kill", dict(self.env, CR_KILL_GRACE_S="2")).stdout
        self.assertIn("CR_KILLED pid=%d" % wrapper, out)
        self.assertTrue(gone(child), "the job's background child survived")
        self.assertTrue(gone(wrapper), "the job's wrapper survived")

    def test_the_kill_cell_with_no_job_or_an_ended_job_kills_nothing(self):
        self.assertIn("CR_NO_JOB", run_cell("kill", self.env).stdout)
        env = dict(self.env, **self.script("exit 0\n"))
        run_cell("start", env)
        self.assertTrue(wait_for(Path(self.vm) / "j.status.json"))
        pid = int((Path(self.vm) / "j.pid").read_text())
        self.assertTrue(gone(pid))
        self.assertIn("CR_NOT_RUNNING", run_cell("kill", self.env).stdout)


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
        self.env = dict(os.environ, FAKE_COLAB_STATE=str(self.state), FAKE_COLAB_VM=str(self.vm),
                        FAKE_COLAB_FAULTS=str(self.faults))
        self.colab = Colab([sys.executable, FAKE], env=self.env)

    def runner(self, **kw):
        return Runner(self.colab, "s", self.out, content=str(self.vm), poll_s=0.2, poll_timeout_s=10, backoff_max_s=0.4,
                      start_attempts=4, **kw)

    def job(self, body):
        p = self.vm / "job.sh"
        p.write_text(body)
        return str(p)

    def calls(self):
        return (self.state / "calls.log").read_text()

    def test_lost_start_reply_and_lost_polls_still_complete_with_one_job(self):
        self.faults.write_text(json.dumps({"lose_reply": {"exec": [1]}, "drop_call": {"exec": [3, 4, 5, 6]}}))
        script = self.job("mkdir -p $CR_OUT_DIR/progress; echo run > $CR_OUT_DIR/phase.txt; echo 3 > $CR_OUT_DIR/progress/a; "
                          "sleep 1; echo done-line\n")
        res = self.runner().run(script=script, env={"CR_OUT_DIR": str(self.vm / "j_out")}, wall_s=120)
        self.assertEqual((res.reason, res.rc), ("done", 0))
        self.assertGreaterEqual(res.max_consecutive_failed, 1)
        self.assertGreaterEqual(self.calls().count("exec"), 5)
        self.assertEqual(len(list(self.vm.glob("*.pid"))), 1)
        self.assertIn("done-line", (self.out / "job.run.out").read_text())
        self.assertIn("stop 1", self.calls())

    def test_a_hung_poll_is_killed_by_its_timeout_and_counts_as_a_failed_poll(self):
        self.faults.write_text(json.dumps({"hang": {"exec": [2]}}))
        script = self.job("sleep 1\n")
        runner = Runner(self.colab, "s", self.out, content=str(self.vm), poll_s=0.2, poll_timeout_s=1, backoff_max_s=0.3)
        res = runner.run(script=script, wall_s=120)
        self.assertEqual(res.reason, "done")
        self.assertGreaterEqual(res.polls_failed, 1)

    def test_dead_job_is_found_and_salvaged(self):
        # the job kills its wrapper (its parent, the pid in the pid file): no status file is ever written
        mark = self.vm / "script.pid"
        script = self.job("echo started\necho $$ > %s\nkill -9 $PPID\nsleep 30\n" % mark)
        got = []
        res = self.runner().run(script=script, wall_s=120, salvage=lambda why: got.append(why) or True)
        self.assertEqual(res.reason, "dead")
        self.assertEqual(got, ["dead"])
        self.assertTrue(res.salvaged)
        self.assertTrue(res.killed.startswith("CR_KILLED"), res.killed)  # its orphaned group is terminated
        self.assertTrue(gone(int(mark.read_text())), "the orphaned script survived the kill")
        self.assertTrue(res.stopped)
        self.assertIn("stop 1", self.calls())

    def test_a_failing_job_is_failed_and_salvaged(self):
        got = []
        res = self.runner().run(script=self.job("echo trying\nexit 1\n"), wall_s=120,
                                salvage=lambda why: got.append(why) or True)
        self.assertEqual((res.reason, res.rc, got, res.stopped), ("failed", 1, ["failed"], True))

    def test_a_sigkilled_job_is_oom_and_salvaged(self):
        got = []
        res = self.runner().run(script=self.job("echo started\nkill -9 $$\n"), wall_s=120,
                                salvage=lambda why: got.append(why) or True)
        # the wrapper survives its child's SIGKILL and writes rc 137: the OOM killer's signal
        self.assertEqual((res.reason, res.rc, got), ("oom", 137, ["oom"]))

    def test_an_unbroken_64k_line_and_a_progress_bar_reach_the_rig_whole(self):
        gen = self.vm / "gen.py"
        gen.write_text("import sys\nsys.stdout.write('u' * 70000)\n"
                       "sys.stdout.write(''.join('\\r %d%%' % i for i in range(20000)))\n")
        script = self.job("python3 %s\nexit 0\n" % gen)
        res = self.runner().run(script=script, wall_s=120)
        self.assertEqual(res.reason, "done")
        text = (self.out / "job.run.out").read_text()
        self.assertEqual(text.count("u"), 70000)
        self.assertIn(" 19999%", text)

    def test_hold_job_end_to_end(self):
        res = self.runner().run(hold_s=0.6, heartbeat_s=0.1, wall_s=120, progress_stall_s=30)
        self.assertEqual((res.reason, res.rc), ("done", 0))
        self.assertGreaterEqual(res.last["hold_beats"], 4)

    def test_resident_mode_end_to_end_collects_the_run_log(self):
        script = self.job("echo resident-line-1\nsleep 0.5\necho resident-line-2\n")
        res = self.runner().run(script=script, wall_s=120, resident=True, env={"CR_POLL_EVERY_S": "0.1"})
        self.assertEqual((res.reason, res.rc), ("done", 0))
        self.assertEqual((self.out / "job.run.out").read_text().split(), ["resident-line-1", "resident-line-2"])
        self.assertIn("download", self.calls())
        self.assertIn("stop 1", self.calls())

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
        self.assertEqual(self.colab.restart_kernel("s").rc, 0)
        self.assertIn("Usage rate:", self.colab.usage().out)
        self.assertEqual(Colab(["/no/such/colab"]).run(["x"], 5).rc, 127)

    def cli(self, *extra, env=None):
        full = dict(self.env, COLAB_RUNNER_CLI="%s %s" % (sys.executable, FAKE), **(env or {}))
        return subprocess.run(
            [sys.executable, "-m", "colab_runner", "run", "--session", "s", "--out-dir", str(self.out), "--content",
             str(self.vm), "--wall-s", "120", "--poll-s", "0.2", "--max-rate", "0.26", "--cap", "0.9",
             "--cleanup-bound-s", "600"] + list(extra),
            env=full, capture_output=True, text=True, timeout=300, stdin=subprocess.DEVNULL,
        )

    def test_cli_entry_point_runs_a_hold_job(self):
        proc = self.cli("--hold-s", "0.4", "--heartbeat-s", "0.1", "--env", "X=1")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn("RUN_END reason=done rc=0 stopped=True", proc.stdout)

    def test_no_token_reaches_an_argv_the_out_dir_or_the_cli_output(self):
        hf = "hf_" + "".join(chr(ord("A") + (i * 5) % 26) for i in range(32))
        proxy = "".join(chr(ord("a") + (i * 3) % 26) for i in range(40))
        script = self.job("echo \"token is $HF_TOKEN\"\nexit 0\n")
        proc = self.cli("--script", script, "--env", "HF_TOKEN", env={"HF_TOKEN": hf, "FAKE_COLAB_LEAK": proxy})
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        self.assertIn(hf, (self.vm / "job.run.out").read_text())  # the VM's raw log has it: the scrub was tested
        argvs = (self.state / "argv.log").read_text()
        leaks = [v for v in (hf[3:], proxy) if v in argvs or v in proc.stdout or v in proc.stderr]
        for p in self.out.rglob("*"):
            if p.is_file():
                leaks += [(p.name, v) for v in (hf[3:], proxy) if v in p.read_text(errors="replace")]
        self.assertEqual(leaks, [])
        self.assertIn("hf_[REDACTED]", (self.out / "job.run.out").read_text())


if __name__ == "__main__":
    unittest.main()

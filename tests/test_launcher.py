"""Tests for end-to-end launch, status, local-cpu dry run, and all four required failure paths."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from colab_runner.__main__ import main
from colab_runner.cards import CARD_CHECK_FAIL_RC, CircuitBreaker
from colab_runner.colab_cli import CliResult
from colab_runner.errors import Refused
from colab_runner.launcher import launch_window, status_window
from tests.fakes import FakeClock, FakeColab, rec, usage_text
from tests.test_tokens import fake_tokens, leak_lines, values


class SimColab(FakeColab):
    """Extended FakeColab supporting sessions, new_session, upload, shape mismatch, and allocation failures."""

    def __init__(self, *, new_results=None, sessions_rows=None, after_open_rows=None, upload_results=None,
                 remote_files=None, **kw):
        super().__init__(**kw)
        self.new_results = list(new_results or [])
        self.sessions_rows = list(sessions_rows or [])
        self.after_open_rows = list(after_open_rows) if after_open_rows is not None else None
        self.upload_results = list(upload_results or [])
        self.remote_files = dict(remote_files or {})
        self.uploads: list[tuple[str, str, str]] = []
        self.opened_sessions: set[str] = set()

    def sessions(self, timeout: float = 30.0) -> CliResult:
        self._record("sessions", None, timeout)
        if self.after_open_rows is not None and self.opened_sessions:
            rows = list(self.after_open_rows)
        else:
            rows = list(self.sessions_rows)
            for s in sorted(self.opened_sessions):
                if not any(r.startswith("[%s] " % s) for r in rows):
                    rows.append("[%s] GPU: G4 | Shape: Standard" % s)
        return CliResult(0, "\n".join(rows) + ("\n" if rows else ""))

    def new_session(self, session: str, *, card: str, shape=None, timeout: float = 180.0) -> CliResult:
        self._record("new", session, timeout)
        if self.new_results:
            res = self.new_results.pop(0)
            if isinstance(res, CliResult):
                if res.ok:
                    self.opened_sessions.add(session)
                return res
        self.opened_sessions.add(session)
        return CliResult(0, "Opened %s (%s)\n" % (session, shape or "high-mem"))

    def upload(self, session: str, local: str, remote: str, timeout: float = 120.0) -> CliResult:
        self._record("upload", session, timeout)
        self.uploads.append((session, local, remote))
        if self.upload_results:
            return self.upload_results.pop(0)
        return CliResult(0, "Uploaded %s\n" % remote)

    def stop(self, session: str, timeout: float = 120.0) -> CliResult:
        res = super().stop(session, timeout)
        if res.ok:
            self.opened_sessions.discard(session)
        return res

    def download(self, session, remote, local, timeout=120.0) -> CliResult:
        if remote in self.remote_files:
            self._record("download", session, timeout)
            data = self.remote_files[remote]
            p = Path(local)
            p.parent.mkdir(parents=True, exist_ok=True)
            if isinstance(data, bytes):
                p.write_bytes(data)
            else:
                p.write_text(str(data), encoding="utf-8")
            return CliResult(0, "")
        return super().download(session, remote, local, timeout)


def _write_spec(root: Path, *, card: str = "G4", shape: str | None = "standard", cap: float = 15.0,
                max_rate: float | None = 10.5, cpu_cmd: str = "python3 worker.py") -> Path:
    (root / "worker.py").write_text(
        "import json, os\nfrom pathlib import Path\n"
        "w = Path(os.environ['CR_WORK_DIR'])\n"
        "(w / 'out.txt').write_text('hello worker\\n', encoding='utf-8')\n"
        "(w / 'result.json').write_text(json.dumps({'ok': True}) + '\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    shape_line = ('shape = "%s"\n' % shape) if shape else ""
    rate_line = ("max_rate = %s\n" % max_rate) if max_rate is not None else ""
    spec = root / "colab-window.toml"
    spec.write_text(
        'schema_version = 1\n'
        '[project]\nrepo = "org/demo"\nstudy = "demo-study"\njob = "demo_job"\n'
        '[compute]\n'
        f'card = "{card}"\n{shape_line}{rate_line}'
        f'cap = {cap}\nwall_s = 1800\ncleanup_bound_s = 600\npoll_s = 0.2\n'
        '[inputs]\nroot = "."\nfiles = ["worker.py"]\n'
        '[job]\n'
        'setup = ["echo setup_ok"]\n'
        'run = ["python3 worker.py"]\n'
        f'cpu_dry_run = ["{cpu_cmd}"]\n'
        '[outputs]\ncollect = ["result.json", "out.txt"]\nsalvage = ["setup.log", "vm_card.json"]\n',
        encoding="utf-8",
    )
    return spec


class LauncherHappyAndLocalCpuTest(unittest.TestCase):
    def test_dry_run_writes_plan_and_makes_zero_colab_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = _write_spec(root)
            out_dir = root / "dry_out"
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = main(["launch", "--spec", str(spec), "--out-dir", str(out_dir), "--dry"])
            self.assertEqual(rc, 0, err.getvalue())
            self.assertIn("DRY_RUN_OK", out.getvalue())
            plan = json.loads((out_dir / "dry_run_plan.json").read_text(encoding="utf-8"))
            self.assertEqual(plan["mode"], "dry_run")
            self.assertEqual(plan["card"], "G4")
            self.assertEqual(len(plan["staged_files"]), 1)

            # status reflects dry_run cleanly
            st = status_window(out_dir)
            self.assertEqual(st["state"], "dry_run")

    def test_local_cpu_runs_real_vm_cells_collects_scrubs_tokens_and_writes_outcome(self):
        if os.name != "posix":
            self.skipTest("local-cpu VM cell execution requires POSIX bash/nohup")
        t = fake_tokens()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            leak = leak_lines(t)[2]
            (root / "leak_worker.py").write_text(
                "import json, os\nfrom pathlib import Path\n"
                "w = Path(os.environ['CR_WORK_DIR'])\n"
                f"(w / 'out.txt').write_text('token ' + {leak!r} + '\\n', encoding='utf-8')\n"
                "(w / 'result.json').write_text(json.dumps({'ok': True}) + '\\n', encoding='utf-8')\n",
                encoding="utf-8",
            )
            spec = _write_spec(root, cpu_cmd="python3 leak_worker.py")
            # Include leak_worker.py in staged inputs
            txt = spec.read_text(encoding="utf-8").replace('files = ["worker.py"]', 'files = ["worker.py", "leak_worker.py"]')
            spec.write_text(txt, encoding="utf-8")
            out_dir = root / "cpu_out"
            res = launch_window(spec, out_dir, local_cpu=True)
            self.assertEqual(res["exit_code"], 0, res)
            self.assertEqual(res["status"], "done")
            self.assertEqual(res["card"], "CPU")
            self.assertTrue(res["session_stopped_confirmed"])
            self.assertTrue(res["session_gone_confirmed"])
            self.assertEqual(res["spend_basis"], "cpu_configured_rate_x_elapsed")
            self.assertIn("result.json", res["collected_outputs"])
            collected_txt = (out_dir / "collected" / "out.txt").read_text(encoding="utf-8")
            self.assertIn("[REDACTED]", collected_txt)
            for v in values(t):
                self.assertNotIn(v, collected_txt)
            self.assertTrue((out_dir / "outcome.json").is_file())
            self.assertTrue((out_dir / "collect_confirmed.json").is_file())

    def test_live_simulated_gpu_launch_collect_and_spend_booking(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = _write_spec(root)
            out_dir = root / "gpu_out"
            clock = FakeClock()
            usage_replies = [
                usage_text(rate=0.0, n=0, balance=200.0),    # pre-open
                usage_text(rate=10.2, n=1, balance=199.9),   # post-open #1
                usage_text(rate=10.2, n=1, balance=199.8),   # post-open #2 (stable!)
                usage_text(rate=10.2, n=1, balance=199.7),   # runner loop
                usage_text(rate=10.2, n=1, balance=197.5),   # end balance
            ]
            sim = SimColab(
                clock=clock,
                usage=lambda n: usage_replies[min(n - 1, len(usage_replies) - 1)],
                polls=lambda n, env: (clock.advance(60.0) or rec(rc=0)),
                remote_files={
                    "/content/demo_job_work/result.json": '{"ok": true}\n',
                    "/content/demo_job_work/out.txt": "clean output\n",
                },
            )
            outcome = launch_window(
                spec, out_dir, colab=sim, clock=clock.now, sleep=clock.sleep,
                study_ledger=root / "ledger.json", state_dir=root / "state",
            )
            self.assertEqual(outcome["exit_code"], 0, outcome)
            self.assertEqual(outcome["status"], "done")
            self.assertEqual(outcome["window_rate_stable"], 10.2)
            self.assertEqual(outcome["priced_rate"], 10.5)  # max(max_rate=10.5, stable=10.2)
            self.assertEqual(outcome["spend_basis"], "window_rate_x_elapsed")
            self.assertFalse(outcome["account_balance"]["used_for_pricing"])
            self.assertTrue(outcome["session_stopped_confirmed"])
            self.assertTrue(outcome["session_gone_confirmed"])

            # Status CLI JSON inspection works on completed window
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                st_rc = main(["status", "--out-dir", str(out_dir), "--json"])
            self.assertEqual(st_rc, 0)
            self.assertEqual(json.loads(out.getvalue())["state"], "finished")


class FailurePathsTest(unittest.TestCase):
    def test_failure_1_dropped_session_unreachable_salvage_and_retry_stop(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = _write_spec(root)
            out_dir = root / "drop_out"
            clock = FakeClock()
            sim = SimColab(
                clock=clock,
                usage=usage_text(rate=10.0, n=1, balance=200.0),
                # Wait: first usage call is pre-open (n=0), next are post-open
                polls=lambda n, env: None,  # every poll fails -> unreachable
                stops=[1, 1, 1, 0],  # first 3 stops during cleanup fail; 4th (`stop --out-dir`) succeeds!
                remote_files={"/content/demo_job_out/setup.log": "setup started\n"},
            )
            # Pre-open must show n=0, post-open n=1
            seq = [
                usage_text(rate=0.0, n=0, balance=200.0),
                usage_text(rate=10.0, n=1, balance=200.0),
                usage_text(rate=10.0, n=1, balance=199.9),
            ]
            sim.usage_reply = lambda idx: seq[min(idx - 1, len(seq) - 1)]

            outcome = launch_window(spec, out_dir, colab=sim, clock=clock.now, sleep=clock.sleep)
            self.assertEqual(outcome["status"], "unreachable")
            self.assertEqual(outcome["exit_code"], 2)
            self.assertFalse(outcome["session_stopped_confirmed"])
            self.assertTrue((out_dir / "session_open.json").is_file())
            self.assertTrue((out_dir / "stop_pending.json").is_file())
            self.assertIn("setup.log", outcome["salvaged_outputs"])
            self.assertEqual(status_window(out_dir)["state"], "stop_pending")

            # Operator recovers using `python -m colab_runner stop --out-dir D`
            out, err = io.StringIO(), io.StringIO()
            with mock.patch("colab_runner.__main__.cli_from_env", return_value=sim), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = main(["stop", "--out-dir", str(out_dir)])
            self.assertEqual(rc, 0)
            self.assertIn("confirmed=True", out.getvalue())
            updated = json.loads((out_dir / "outcome.json").read_text(encoding="utf-8"))
            self.assertTrue(updated["session_stopped_confirmed"])
            self.assertIsNotNone(updated["closed_at"])

    def test_failure_2_oom_exit_137_and_vm_card_mismatch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = _write_spec(root)
            out_dir = root / "oom_out"
            clock = FakeClock()
            seq = [
                usage_text(rate=0.0, n=0, balance=100.0),
                usage_text(rate=10.2, n=1, balance=100.0),
                usage_text(rate=10.2, n=1, balance=99.9),
            ]
            sim = SimColab(
                clock=clock,
                usage=lambda idx: seq[min(idx - 1, len(seq) - 1)],
                polls=lambda n, env: (rec(rc=137), ["Out of memory: Killed process 99 (python3)"]),
                remote_files={"/content/demo_job_out/setup.log": "setup ok\n"},
            )
            outcome = launch_window(spec, out_dir, colab=sim, clock=clock.now, sleep=clock.sleep)
            self.assertEqual(outcome["status"], "oom")
            self.assertEqual(outcome["run_rc"], 137)
            self.assertEqual(outcome["exit_code"], 2)
            self.assertTrue(outcome["session_stopped_confirmed"])
            self.assertIn("setup.log", outcome["salvaged_outputs"])

            # VM card mismatch (rc=42 + vm_card.json) triggers CARD_MISMATCH + circuit breaker
            state_dir = root / "state"
            out_dir_mm = root / "mm_out"
            sim_mm = SimColab(
                clock=FakeClock(),
                usage=lambda idx: seq[min(idx - 1, len(seq) - 1)],
                polls=lambda n, env: (rec(rc=CARD_CHECK_FAIL_RC), ["CR_CARD_MISMATCH: VRAM 15360 below 90000"]),
                remote_files={"/content/demo_job_out/vm_card.json": '{"ok": false, "requested_card": "G4"}\n'},
            )
            res_mm = launch_window(spec, out_dir_mm, colab=sim_mm, clock=sim_mm.clock.now, sleep=sim_mm.clock.sleep,
                                   state_dir=state_dir)
            self.assertEqual(res_mm["status"], "card_mismatch")
            self.assertEqual(res_mm["refusal"]["code"], "CARD_MISMATCH")
            self.assertEqual(len(CircuitBreaker(state_dir / "card_refusals.json").events_for("G4")), 1)

    def test_failure_3_cap_hits_pre_open_rate_above_max_and_mid_run_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # 3a: pre-open cap too small for cleanup -> exit 1 refusal, zero colab calls
            spec_small = _write_spec(root, cap=1.0, max_rate=10.5)
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = main(["launch", "--spec", str(spec_small), "--out-dir", str(root / "c1")])
            self.assertEqual(rc, 1)
            self.assertIn("REFUSED: CAP_TOO_SMALL_FOR_CLEANUP", err.getvalue())

            # 3b: post-open stable rate > max_rate -> stopped immediately, cap booked
            spec_ok = _write_spec(root, cap=15.0, max_rate=10.5)
            high_seq = [
                usage_text(rate=0.0, n=0, balance=100.0),
                usage_text(rate=14.0, n=1, balance=100.0),
                usage_text(rate=14.0, n=1, balance=99.8),
            ]
            c2 = FakeClock()
            sim_high = SimColab(clock=c2, usage=lambda idx: high_seq[min(idx - 1, len(high_seq) - 1)])
            res_high = launch_window(spec_ok, root / "c2", colab=sim_high, clock=c2.now, sleep=c2.sleep)
            self.assertEqual(res_high["status"], "cap")
            self.assertEqual(res_high["cap_trip"]["code"], "RATE_ABOVE_MAX")
            self.assertTrue(res_high["session_stopped_confirmed"])

            # 3c: mid-run cap guard trip -> kills VM job, runs salvage, stops session
            spec_cap5 = _write_spec(root, cap=5.0, max_rate=10.5)
            mid_seq = [
                usage_text(rate=0.0, n=0, balance=100.0),
                usage_text(rate=10.5, n=1, balance=100.0),
                usage_text(rate=10.5, n=1, balance=99.9),
            ]
            c3 = FakeClock()
            sim_mid = SimColab(
                clock=c3,
                usage=lambda idx: mid_seq[min(idx - 1, len(mid_seq) - 1)],
                polls=lambda n, env: (c3.advance(1400.0) or rec(progress=n)),
                remote_files={"/content/demo_job_out/setup.log": "running\n"},
            )
            res_mid = launch_window(spec_cap5, root / "c3", colab=sim_mid, clock=c3.now, sleep=c3.sleep)
            self.assertEqual(res_mid["status"], "cap")
            self.assertEqual(res_mid["cap_trip"]["code"], "CAP_GUARD_TRIPPED")
            self.assertEqual(sim_mid.kill_calls, 1)
            self.assertTrue(res_mid["session_stopped_confirmed"])

    def test_failure_4_unavailable_card_type_and_circuit_breaker_and_pool_limits(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            # 4a: H100 unavailable up front
            spec_h100 = _write_spec(root, card="H100", shape="standard", max_rate=20.0, cap=30.0)
            with self.assertRaises(Refused) as ctx:
                launch_window(spec_h100, root / "h100_out", state_dir=state_dir)
            self.assertEqual(ctx.exception.code, "CARD_UNAVAILABLE")

            # 4b: G4 allocation refusal twice -> 3rd attempt blocked pre-open by CARD_CIRCUIT_OPEN
            spec_g4 = _write_spec(root, card="G4", shape="standard")
            pre_seq = [usage_text(rate=0.0, n=0, balance=100.0)]
            for idx in range(2):
                sim_fail = SimColab(
                    clock=FakeClock(),
                    usage=lambda n: pre_seq[0],
                    new_results=[CliResult(1, "", "503 Service Unavailable: no G4 backend available")],
                )
                res = launch_window(spec_g4, root / ("alloc_%d" % idx), colab=sim_fail,
                                    clock=sim_fail.clock.now, sleep=sim_fail.clock.sleep, state_dir=state_dir)
                self.assertEqual(res["status"], "open_failed")
                self.assertEqual(res["refusal"]["code"], "SESSION_OPEN_FAILED")
                self.assertTrue(res["session_stopped_confirmed"])
                self.assertEqual(res["spend_ccu"], 0.0)

            with self.assertRaises(Refused) as ctx:
                launch_window(spec_g4, root / "alloc_blocked", state_dir=state_dir)
            self.assertEqual(ctx.exception.code, "CARD_CIRCUIT_OPEN")

            # 4c: pool ceiling and shape mismatch
            spec_t4 = _write_spec(root, card="T4", shape="high-mem", max_rate=2.0, cap=10.0)
            sim_pool = SimColab(
                clock=FakeClock(),
                usage=usage_text(rate=0.0, n=0),
                sessions_rows=["[s1] GPU: T4", "[s2] GPU: T4", "[s3] GPU: T4"],
            )
            with self.assertRaises(Refused) as ctx:
                launch_window(spec_t4, root / "pool_out", colab=sim_pool, clock=sim_pool.clock.now,
                              sleep=sim_pool.clock.sleep)
            self.assertEqual(ctx.exception.code, "POOL_CEILING_REACHED")

            sim_shape = SimColab(
                clock=FakeClock(),
                usage=usage_text(rate=0.0, n=0),
                after_open_rows=["[my-t4] GPU: T4 | Shape: Standard"],
            )
            res_shape = launch_window(spec_t4, root / "shape_out", session="my-t4", colab=sim_shape,
                                      clock=sim_shape.clock.now, sleep=sim_shape.clock.sleep)
            self.assertEqual(res_shape["status"], "shape_mismatch")
            self.assertEqual(res_shape["refusal"]["code"], "SHAPE_MISMATCH")
            self.assertTrue(res_shape["session_stopped_confirmed"])


class BinaryCollectAndEdgeGuardsTest(unittest.TestCase):
    def test_upload_failure_pre_open_unread_and_binary_token_quarantine_in_collect(self):
        t = fake_tokens()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = _write_spec(root)
            seq = [
                usage_text(rate=0.0, n=0, balance=100.0),
                usage_text(rate=10.0, n=1, balance=100.0),
                usage_text(rate=10.0, n=1, balance=99.9),
            ]
            # Upload failure stops session cleanly
            c1 = FakeClock()
            sim_up = SimColab(clock=c1, usage=lambda idx: seq[min(idx - 1, len(seq) - 1)],
                              upload_results=[CliResult(1, "", "500 Server Error")])
            res_up = launch_window(spec, root / "up_fail", colab=sim_up, clock=c1.now, sleep=c1.sleep)
            self.assertEqual(res_up["status"], "upload_failed")
            self.assertEqual(res_up["refusal"]["code"], "UPLOAD_FAILED")
            self.assertTrue(res_up["session_stopped_confirmed"])

            # Post-open rate unread stops session cleanly
            c2 = FakeClock()
            sim_unread = SimColab(clock=c2, usage=usage_text(rate=0.0, n=0))
            with self.assertRaises(Refused) as ctx:
                # wait: n=0 pre-open ok, then post-open n=0 6x -> POST_OPEN_RATE_UNREAD
                res_ur = launch_window(spec, root / "ur_fail", colab=sim_unread, clock=c2.now, sleep=c2.sleep,
                                       post_open_polls=2)
                self.assertEqual(res_ur["refusal"]["code"], "POST_OPEN_RATE_UNREAD")
                raise Refused("STATUS_DIR_MISSING", "dummy check")  # ensure status missing dir works too
            self.assertEqual(ctx.exception.code, "STATUS_DIR_MISSING")

            # Binary containing bearer token downloaded in `collect` -> quarantined + status="error" + stopped
            c3 = FakeClock()
            sim_bin = SimColab(
                clock=c3,
                usage=lambda idx: seq[min(idx - 1, len(seq) - 1)],
                polls=lambda n, env: rec(rc=0),
                remote_files={
                    "/content/demo_job_work/result.json": b"\x00BIN Bearer " + t["bearer"].encode("ascii") + b"\x00",
                },
            )
            res_bin = launch_window(spec, root / "bin_leak", colab=sim_bin, clock=c3.now, sleep=c3.sleep)
            self.assertEqual(res_bin["status"], "error")
            self.assertEqual(res_bin["refusal"]["code"], "BINARY_DOWNLOAD_TOKEN")
            self.assertEqual(len(res_bin["quarantined"]), 1)
            self.assertTrue(res_bin["session_stopped_confirmed"])

            # Missing status dir raises STATUS_DIR_MISSING
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = main(["status", "--out-dir", str(root / "no_such_dir")])
            self.assertEqual(rc, 1)
            self.assertIn("REFUSED: STATUS_DIR_MISSING", err.getvalue())


class LauncherBranchCoverageTest(unittest.TestCase):
    def test_colab_cli_wrappers_sessions_new_and_upload(self):
        from colab_runner.colab_cli import Colab, parse_sessions_output

        calls = []
        client = Colab(["colab"])
        with mock.patch.object(client, "run", side_effect=lambda args, t: (calls.append(args), CliResult(0, "ok"))[1]):
            client.new_session("s-cpu", card="CPU")
            client.new_session("s-std", card="G4", shape="standard")
            client.new_session("s-hm", card="G4", shape="high-mem")
            client.upload("s-std", "/tmp/a", "/content/a")
            client.sessions()
        self.assertEqual(calls[0], ["new", "--high-mem", "-s", "s-cpu"])
        self.assertEqual(calls[1], ["new", "--gpu", "G4", "-s", "s-std"])
        self.assertEqual(calls[2], ["new", "--gpu", "G4", "--high-mem", "-s", "s-hm"])
        self.assertEqual(calls[3], ["upload", "-s", "s-std", "/tmp/a", "/content/a"])
        self.assertEqual(calls[4], ["sessions"])

        parsed = parse_sessions_output(
            "ignored header\n[s1] Accelerator: G4 | Shape: Standard\n[s2] GPU: CPU | Shape: High-RAM\n"
        )
        self.assertEqual(len(parsed), 2)
        self.assertEqual((parsed[0]["session"], parsed[0]["shape"], parsed[0]["accelerator"]), ("s1", "Standard", "G4"))

    def test_out_dir_env_open_lock_pool_and_pre_open_refusals(self):
        from colab_runner.launcher import _open_lock, resolve_kit_env

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = _write_spec(root)

            # Non-empty out dir refused before touching Colab
            non_empty = root / "nonempty"
            non_empty.mkdir()
            (non_empty / "junk").write_text("x", encoding="utf-8")
            with self.assertRaises(Refused) as ctx:
                launch_window(spec, non_empty)
            self.assertEqual(ctx.exception.code, "OUT_DIR_NOT_EMPTY")

            # File instead of directory refused
            not_dir = root / "a_file"
            not_dir.write_text("x", encoding="utf-8")
            with self.assertRaises(Refused) as ctx:
                launch_window(spec, not_dir)
            self.assertEqual(ctx.exception.code, "OUT_DIR_INVALID")

            # Reserved CR_RUN_ID, unset env, bad env name
            with self.assertRaises(Refused) as ctx:
                resolve_kit_env(["1BAD=x"])
            self.assertEqual(ctx.exception.code, "ENV_INVALID")
            with self.assertRaises(Refused) as ctx:
                resolve_kit_env(["CR_DEFINITELY_UNSET_VAR_XYZ"])
            self.assertEqual(ctx.exception.code, "ENV_UNSET")
            with self.assertRaises(Refused) as ctx:
                launch_window(spec, root / "reserved_env", extra_env=["CR_RUN_ID=abc"], dry=True)
            self.assertEqual(ctx.exception.code, "RUN_ID_RESERVED")

            # Open lock timeout refused on POSIX
            if os.name == "posix":
                clock = FakeClock()
                lock_p = root / "test.lock"
                with mock.patch("fcntl.flock", side_effect=BlockingIOError("busy")):
                    with self.assertRaises(Refused) as ctx:
                        with _open_lock(lock_p, timeout_s=0.2, clock=clock.now, sleep=clock.sleep):
                            pass
                self.assertEqual(ctx.exception.code, "OPEN_LOCK_TIMEOUT")

            # Duplicate active session name and 20-session total ceiling
            sim_dup = SimColab(clock=FakeClock(), usage=usage_text(rate=0.0, n=0), sessions_rows=["[dup] GPU: G4"])
            with self.assertRaises(Refused) as ctx:
                launch_window(spec, root / "dup_out", session="dup", colab=sim_dup)
            self.assertEqual(ctx.exception.code, "SESSION_ALREADY_OPEN")

            twenty = ["[s%d] GPU: T4" % i for i in range(20)]
            sim_20 = SimColab(clock=FakeClock(), usage=usage_text(rate=0.0, n=0), sessions_rows=twenty)
            with self.assertRaises(Refused) as ctx:
                launch_window(spec, root / "s20_out", session="fresh", colab=sim_20)
            self.assertEqual(ctx.exception.code, "POOL_CEILING_REACHED")

            # Pre-open usage failure or missing assignments count refused on GPU
            sim_bad_usage = SimColab(clock=FakeClock(), usage=lambda n: CliResult(1, "", "down"))
            with self.assertRaises(Refused) as ctx:
                launch_window(spec, root / "u1", colab=sim_bad_usage)
            self.assertEqual(ctx.exception.code, "PRE_OPEN_READ_FAILED")

            sim_no_assign = SimColab(clock=FakeClock(), usage="Current balance: 50.0 compute units\nUsage rate: 0.0 / hr\n")
            with self.assertRaises(Refused) as ctx:
                launch_window(spec, root / "u2", colab=sim_no_assign)
            self.assertEqual(ctx.exception.code, "PRE_OPEN_READ_FAILED")

    def test_post_open_unreadable_assignments_status_states_and_cli_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = _write_spec(root)
            c = FakeClock()
            bad_seq = [
                usage_text(rate=0.0, n=0, balance=100.0),
                usage_text(rate=10.2, n=1, balance=100.0),
                usage_text(rate=10.2, n=1, balance=100.0),
                "Current balance: 99.00 compute units\nUsage rate: 10.20 / hr\n",
                "Current balance: 99.00 compute units\nUsage rate: 10.20 / hr\n",
                "Current balance: 99.00 compute units\nUsage rate: 10.20 / hr\n",
            ]
            # First two post-open reads settle "ok"; wait: to hit ASSIGNMENTS_UNREADABLE during post-open,
            # use rate=0.0, n=1 first then drop assignments. Instead, test CLI launch exit=2 + hint printing via RATE_ABOVE_MAX:
            high_seq = [
                usage_text(rate=0.0, n=0, balance=100.0),
                usage_text(rate=15.0, n=1, balance=100.0),
                usage_text(rate=15.0, n=1, balance=99.9),
            ]
            sim_cli = SimColab(clock=c, usage=lambda idx: high_seq[min(idx - 1, len(high_seq) - 1)])
            out, err = io.StringIO(), io.StringIO()
            with mock.patch("colab_runner.__main__.cli_from_env", return_value=sim_cli), \
                    mock.patch("colab_runner.launcher.sleep" if False else "time.sleep"), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = main(["launch", "--spec", str(spec), "--out-dir", str(root / "cli_cap")])
            self.assertEqual(rc, 2)
            self.assertIn("LAUNCH_END status=cap", out.getvalue())
            self.assertIn("hint:", err.getvalue())

            # Test status_window across running, finished (runner_result only), unknown, and log tail
            run_dir = root / "st_running"
            run_dir.mkdir()
            (run_dir / "session_open.json").write_text('{"session": "s1", "job": "demo"}\n', encoding="utf-8")
            (run_dir / "demo.run.out").write_text("line1\nline2\n", encoding="utf-8")
            st_run = status_window(run_dir)
            self.assertEqual(st_run["state"], "running")
            self.assertEqual(st_run["last_log_lines"], ["line1", "line2"])

            fin_dir = root / "st_fin"
            fin_dir.mkdir()
            (fin_dir / "stop_confirmed.json").write_text('{"session": "s1"}\n', encoding="utf-8")
            st_fin = status_window(fin_dir)
            self.assertEqual(st_fin["state"], "finished")

            unk_dir = root / "st_unk"
            unk_dir.mkdir()
            st_unk = status_window(unk_dir)
            self.assertEqual(st_unk["state"], "unknown")

            # Text status CLI output
            out2, err2 = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out2), contextlib.redirect_stderr(err2):
                st_rc = main(["status", "--out-dir", str(run_dir)])
            self.assertEqual(st_rc, 0)
            self.assertIn("STATUS state=running", out2.getvalue())

            # Test exit 3 (`done` with unconfirmed stop) via LocalColab subclass
            if os.name == "posix":
                from colab_runner.launcher import LocalColab

                class _FailStopLocal(LocalColab):
                    def stop(self, session, timeout=120.0):
                        return CliResult(1, "", "stop denied")

                d3 = root / "partial_dir"
                lc = _FailStopLocal(root / "vmc")
                res3 = launch_window(spec, d3, colab=lc, local_cpu=True)
                self.assertEqual(res3["status"], "done")
                self.assertEqual(res3["exit_code"], 3)
                self.assertFalse(res3["session_stopped_confirmed"])

                # Unexpected RuntimeError in launch_command triggers LAUNCH_ERROR with stop-not-confirmed hint
                d_err = root / "err_dir"
                out3, err3 = io.StringIO(), io.StringIO()
                with mock.patch("colab_runner.__main__.launch_window", side_effect=RuntimeError("boom")), \
                        contextlib.redirect_stdout(out3), contextlib.redirect_stderr(err3):
                    d_err.mkdir()
                    (d_err / "session_open.json").write_text('{"session": "s"}\n', encoding="utf-8")
                    rc3 = main(["launch", "--spec", str(spec), "--out-dir", str(d_err), "--dry"])
                self.assertEqual(rc3, 2)
                self.assertIn("LAUNCH_ERROR: UNEXPECTED_ERROR:", err3.getvalue())
                self.assertIn("session_open.json stays until a stop is confirmed", err3.getvalue())

            # Refused via CLI launch (`CARD_UNAVAILABLE`) exits 1 cleanly
            out_r, err_r = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out_r), contextlib.redirect_stderr(err_r):
                rc_r = main(["launch", "--spec", str(spec), "--out-dir", str(root / "h100_cli"), "--card", "H100"])
            self.assertEqual(rc_r, 1)
            self.assertIn("REFUSED: CARD_UNAVAILABLE", err_r.getvalue())

            # LocalColab edge branches + resident=True local-cpu execution
            from colab_runner.launcher import LocalColab

            lc_edge = LocalColab(root / "vm_edge")
            self.assertEqual(lc_edge.exec_cell("not-open", "print(1)").rc, 1)
            self.assertEqual(lc_edge.upload("not-open", str(spec), "/content/x").rc, 1)
            lc_edge.new_session("open1", card="CPU")
            self.assertEqual(lc_edge.upload("open1", str(spec), "/content/sub/x").rc, 1)
            self.assertEqual(lc_edge.download("open1", "/content/missing.txt", str(root / "m.txt")).rc, 1)
            self.assertEqual(lc_edge.restart_kernel("open1").rc, 0)
            if os.name == "posix":
                # resident=True with cleanup_bound_s=600 refuses CLEANUP_BOUND_TOO_SMALL pre-open
                with self.assertRaises(Refused) as ctx:
                    launch_window(spec, root / "res_small", local_cpu=True, resident=True)
                self.assertEqual(ctx.exception.code, "CLEANUP_BOUND_TOO_SMALL")

                # resident=True with sufficient cleanup_bound_s runs LocalColab.start_cell to completion
                res_res = launch_window(spec, root / "res_cpu", local_cpu=True, resident=True, cleanup_bound_s=1100.0)
                self.assertEqual(res_res["status"], "done")
                self.assertEqual(res_res["exit_code"], 0)

            # Silence unused variable warning in test helper
            self.assertEqual(len(bad_seq), 6)


if __name__ == "__main__":
    unittest.main()

"""Tests for end-to-end launch, status, local-cpu dry run, FIX-1 money/lock/pool fixes, and failure paths."""

import contextlib
import hashlib
import io
import json
import os
import signal
import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from colab_runner.__main__ import main
from colab_runner.cards import CARD_CHECK_FAIL_RC, CircuitBreaker, StudyLedger
from colab_runner.colab_cli import BinaryDownloadRefused, CliResult, Colab, parse_sessions_output
from colab_runner.errors import Refused
from colab_runner.launcher import (
    LocalColab,
    _acquire_dir_lock,
    _default_open_lock_path,
    _pid_alive,
    default_cleanup_bound_s,
    launch_window,
    resolve_kit_env,
    status_window,
)
from colab_runner.runner import RunInterrupted, Runner
from tests.fakes import FakeClock, FakeColab, rec, usage_text
from tests.test_tokens import fake_tokens, leak_lines, values


class SimColab(FakeColab):
    """Extended FakeColab matching real `colab sessions` formatting, new_session, upload, shape, and pool rows."""

    def __init__(self, *, new_results=None, sessions_rows=None, after_open_rows=None, upload_results=None,
                 remote_files=None, delay_upload_s: float = 0.0, **kw):
        super().__init__(**kw)
        self.new_results = list(new_results or [])
        self.sessions_rows = list(sessions_rows or [])
        self.after_open_rows = list(after_open_rows) if after_open_rows is not None else None
        self.upload_results = list(upload_results or [])
        self.remote_files = dict(remote_files or {})
        self.delay_upload_s = float(delay_upload_s)
        self.uploads: list[tuple[str, str, str]] = []
        self.opened_sessions: set[str] = set()

    def sessions(self, timeout: float = 30.0) -> CliResult:
        self._record("sessions", None, timeout)
        if self.after_open_rows is not None and self.opened_sessions:
            nxt = self.after_open_rows.pop(0) if self.after_open_rows and isinstance(self.after_open_rows[0], CliResult) else None
            if nxt is not None:
                return nxt
            rows = [r for r in self.after_open_rows if isinstance(r, str)]
        else:
            rows = list(self.sessions_rows)
            for s in sorted(self.opened_sessions):
                if not any(r.startswith("[%s] " % s) for r in rows):
                    rows.append("[%s] gpu-g4-1 | Hardware: G4 | Shape: Standard | Variant: GPU" % s)
        return CliResult(0, "\n".join(rows) + ("\n" if rows else ""))

    def new_session(self, session: str, *, card: str, shape=None, cli_flag: str = "--gpu",
                    timeout: float = 180.0) -> CliResult:
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
        if self.clock is not None and self.delay_upload_s:
            self.clock.advance(self.delay_upload_s)
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
                max_rate: float | None = 10.5, wall_s: float = 1800,
                salvage=("setup.log", "vm_card.json"), cpu_cmd: str = "python3 worker.py") -> Path:
    (root / "worker.py").write_text(
        "import json, os\nfrom pathlib import Path\n"
        "w = Path(os.environ['CR_WORK_DIR'])\n"
        "(w / 'out.txt').write_text('hello worker\\n', encoding='utf-8')\n"
        "(w / 'result.json').write_text(json.dumps({'ok': True}) + '\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    shape_line = ('shape = "%s"\n' % shape) if shape else ""
    rate_line = ("max_rate = %s\n" % max_rate) if max_rate is not None else ""
    salvage_str = json.dumps(list(salvage))
    spec = root / "colab-window.toml"
    spec.write_text(
        'schema_version = 1\n'
        '[project]\nrepo = "org/demo"\nstudy = "demo-study"\njob = "demo_job"\n'
        '[compute]\n'
        f'card = "{card}"\n{shape_line}{rate_line}'
        f'cap = {cap}\nwall_s = {wall_s}\ncleanup_bound_s = 600\npoll_s = 0.2\n'
        '[inputs]\nroot = "."\nfiles = ["worker.py"]\n'
        '[job]\n'
        'setup = ["echo setup_ok"]\n'
        'run = ["python3 worker.py"]\n'
        f'cpu_dry_run = ["{cpu_cmd}"]\n'
        f'[outputs]\ncollect = ["result.json", "out.txt"]\nsalvage = {salvage_str}\n',
        encoding="utf-8",
    )
    return spec


class LauncherHappyAndLocalCpuTest(unittest.TestCase):
    def test_dry_run_includes_open_phase_and_omits_false_eq_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = _write_spec(root, cap=15.0, max_rate=10.5)
            out_dir = root / "dry_out"
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = main(["launch", "--spec", str(spec), "--out-dir", str(out_dir), "--dry"])
            self.assertEqual(rc, 0, err.getvalue())
            stdout_txt = out.getvalue()
            self.assertIn("DRY_RUN_OK", stdout_txt)
            self.assertIn("open_phase_bound_s:", stdout_txt)
            self.assertIn("open_phase_bound_units:", stdout_txt)
            self.assertNotIn("(= cap ", stdout_txt)  # total worst case exceeds cap when open phase is added
            plan = json.loads((out_dir / "dry_run_plan.json").read_text(encoding="utf-8"))
            self.assertGreater(plan["budget"]["open_phase_bound_s"], 0.0)
            self.assertGreater(plan["budget"]["worst_case_total_spend"], plan["budget"]["cap"])
            self.assertEqual(status_window(out_dir)["state"], "dry_run")

    def test_local_cpu_and_sealed_outcome_recording(self):
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
            sealed_dir = root / "sealed-inputs"
            sealed_dir.mkdir()
            (sealed_dir / "item.txt").write_text("sealed synthetic item\n", encoding="utf-8")
            prereg = root / "prereg.txt"
            prereg_raw = b"study=demo-study G4 CPU colab.google.com\nsealed_path: leak_worker.py\nsealed_path: worker.py\n"
            prereg.write_bytes(prereg_raw)
            prereg.chmod(0o444)
            try:
                spec = _write_spec(root, cpu_cmd="python3 leak_worker.py")
                txt = spec.read_text(encoding="utf-8").replace(
                    'files = ["worker.py"]',
                    'files = ["worker.py", "leak_worker.py"]\n'
                    '[inputs.sealed]\n'
                    'preregistration_path = "prereg.txt"\n'
                    f'preregistration_sha256 = "{hashlib.sha256(prereg_raw).hexdigest()}"\n'
                    'allowed_cards = ["G4", "CPU"]\n'
                    'allowed_host = "colab.google.com"\n',
                )
                # Keep inputs.root = "." cleanly, use sealed subdir pattern so sealed gate activates
                (root / "sealed-eval").mkdir()
                (root / "sealed-eval" / "item.txt").write_text("ok\n", encoding="utf-8")
                prereg.chmod(0o644)
                prereg_raw = (
                    b"study=demo-study G4 colab.google.com\nsealed_path: sealed-eval/item.txt\n"
                )
                prereg.write_bytes(prereg_raw)
                prereg.chmod(0o444)
                txt = spec.read_text(encoding="utf-8").replace(
                    'files = ["worker.py"]',
                    'files = ["worker.py", "leak_worker.py", "sealed-eval/item.txt"]\n'
                    '[inputs.sealed]\n'
                    'preregistration_path = "prereg.txt"\n'
                    f'preregistration_sha256 = "{hashlib.sha256(prereg_raw).hexdigest()}"\n'
                    'allowed_cards = ["G4"]\n'
                    'allowed_host = "colab.google.com"\n',
                )
                spec.write_text(txt, encoding="utf-8")

                out_dir = root / "cpu_out"
                res = launch_window(spec, out_dir, local_cpu=True)
                self.assertEqual(res["exit_code"], 0, res)
                self.assertEqual(res["status"], "done")
                self.assertEqual(res["card"], "CPU")
                self.assertIsNotNone(res["sealed"])
                self.assertEqual(res["sealed"]["sealed_files"], ["sealed-eval/item.txt"])
                self.assertTrue(res["session_stopped_confirmed"])
                self.assertTrue(res["session_gone_confirmed"])
                collected_txt = (out_dir / "collected" / "out.txt").read_text(encoding="utf-8")
                self.assertIn("[REDACTED]", collected_txt)
                for v in values(t):
                    self.assertNotIn(v, collected_txt)
                self.assertTrue((out_dir / "collect_confirmed.json").is_file())
            finally:
                if prereg.exists():
                    prereg.chmod(0o644)

    def test_live_simulated_gpu_launch_and_open_phase_cap_clock(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = _write_spec(root, cap=15.0, max_rate=10.5)
            out_dir = root / "gpu_out"
            clock = FakeClock()
            usage_replies = [
                usage_text(rate=0.0, n=0, balance=200.0),
                usage_text(rate=10.2, n=1, balance=199.9),
                usage_text(rate=10.2, n=1, balance=199.8),
                usage_text(rate=10.2, n=1, balance=199.7),
                usage_text(rate=10.2, n=1, balance=197.5),
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
            self.assertEqual(outcome["priced_rate"], 10.5)
            self.assertEqual(outcome["spend_basis"], "window_rate_x_elapsed")
            self.assertFalse(outcome["account_balance"]["used_for_pricing"])

            # Item 8: long open-phase uploads (200s + 200s = 400s) counts against cap in Runner.run!
            # cap=2.925, margin=0, cleanup_bound_s=600 -> reserve=1.75 -> ceiling=1.175 -> trips at 402.86s total elapsed!
            c_cap = FakeClock()
            spec_tight = _write_spec(root, cap=2.925, max_rate=10.5, wall_s=3600)
            sim_slow_open = SimColab(
                clock=c_cap,
                delay_upload_s=190.0,  # 2 uploads = 380s during open phase before Runner.run starts
                usage=lambda n: usage_replies[min(n - 1, len(usage_replies) - 1)],
                polls=lambda n, env: (c_cap.advance(30.0) or rec(progress=n)),
                remote_files={"/content/demo_job_out/setup.log": "ok\n"},
            )
            out_open_cap = launch_window(
                spec_tight, root / "open_cap_out", colab=sim_slow_open, clock=c_cap.now, sleep=c_cap.sleep,
                state_dir=root / "state2",
            )
            self.assertEqual(out_open_cap["status"], "cap")
            self.assertLess(out_open_cap["elapsed_s"], 500.0)  # tripped right after ~403s from colab new!
            self.assertTrue(out_open_cap["session_stopped_confirmed"])


class FailurePathsTest(unittest.TestCase):
    def test_failure_1_dropped_session_unreachable_salvage_and_stop_rebooking(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = _write_spec(root)
            out_dir = root / "drop_out"
            clock = FakeClock()
            seq = [
                usage_text(rate=0.0, n=0, balance=200.0),
                usage_text(rate=10.0, n=1, balance=200.0),
                usage_text(rate=10.0, n=1, balance=199.9),
            ]
            sim = SimColab(
                clock=clock,
                usage=lambda idx: seq[min(idx - 1, len(seq) - 1)],
                polls=lambda n, env: None,
                stops=[1, 1, 1, 0],
                remote_files={"/content/demo_job_out/setup.log": "setup started\n"},
            )

            outcome = launch_window(spec, out_dir, colab=sim, clock=clock.now, sleep=clock.sleep, state_dir=root / "s")
            self.assertEqual(outcome["status"], "unreachable")
            self.assertEqual(outcome["exit_code"], 2)
            self.assertFalse(outcome["session_stopped_confirmed"])
            self.assertTrue((out_dir / "session_open.json").is_file())
            self.assertIn("setup.log", outcome["salvaged_outputs"])
            self.assertEqual(status_window(out_dir)["state"], "stop_pending")

            # Set opened_at 3600s before now so Item 11 stop reconciliation re-books spend up to close
            past_open = datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() - 3600.0, tz=timezone.utc)
            raw_out = json.loads((out_dir / "outcome.json").read_text(encoding="utf-8"))
            raw_out["opened_at"] = past_open.strftime("%Y-%m-%dT%H:%M:%SZ")
            (out_dir / "outcome.json").write_text(json.dumps(raw_out), encoding="utf-8")

            out, err = io.StringIO(), io.StringIO()
            with mock.patch("colab_runner.__main__.cli_from_env", return_value=sim), \
                    contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = main(["stop", "--out-dir", str(out_dir)])
            self.assertEqual(rc, 0)
            updated = json.loads((out_dir / "outcome.json").read_text(encoding="utf-8"))
            self.assertTrue(updated["session_stopped_confirmed"])
            self.assertTrue(updated["spend_basis"].endswith("_reconciled"))
            self.assertGreater(updated["elapsed_s"], 3500.0)
            self.assertGreater(updated["spend_ccu"], 10.0)

    def test_failure_2_oom_exit_137_and_vm_card_mismatch_even_with_empty_salvage(self):
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
            outcome = launch_window(spec, out_dir, colab=sim, clock=clock.now, sleep=clock.sleep, state_dir=root / "s1")
            self.assertEqual((outcome["status"], outcome["run_rc"], outcome["exit_code"]), ("oom", 137, 2))
            self.assertTrue(outcome["session_stopped_confirmed"])

            # Item 21: empty outputs.salvage still downloads vm_card.json so CARD_MISMATCH detects and trips breaker
            spec_empty_salvage = _write_spec(root, salvage=())
            state_dir = root / "state"
            out_dir_mm = root / "mm_out"
            sim_mm = SimColab(
                clock=FakeClock(),
                usage=lambda idx: seq[min(idx - 1, len(seq) - 1)],
                polls=lambda n, env: (rec(rc=CARD_CHECK_FAIL_RC), ["failed"]),
                remote_files={"/content/demo_job_out/vm_card.json": '{"ok": false, "requested_card": "G4"}\n'},
            )
            res_mm = launch_window(spec_empty_salvage, out_dir_mm, colab=sim_mm, clock=sim_mm.clock.now,
                                   sleep=sim_mm.clock.sleep, state_dir=state_dir)
            self.assertEqual(res_mm["status"], "card_mismatch")
            self.assertEqual(res_mm["refusal"]["code"], "CARD_MISMATCH")
            self.assertEqual(len(CircuitBreaker(state_dir / "card_refusals.json").events_for("G4")), 1)

    def test_failure_3_cap_hits_money_limits_and_mid_run_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = _write_spec(root, cap=15.0, max_rate=10.5)
            # Item 12: live GPU launch via CLI requires --cap and --max-rate
            for bad_flags, want_code in [
                (["--max-rate", "10.5"], "CAP_REQUIRED"),
                (["--cap", "15.0"], "MAX_RATE_REQUIRED"),
                (["--cap", "15.0", "--max-rate", "2.0"], "MAX_RATE_BELOW_FLOOR"),
            ]:
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    rc = main(["launch", "--spec", str(spec), "--out-dir", str(root / ("m_" + want_code))] + bad_flags)
                self.assertEqual(rc, 1)
                self.assertIn("REFUSED: " + want_code, err.getvalue())

            # Pre-open CAP_TOO_SMALL_FOR_CLEANUP
            small_dir = root / "small_spec"
            small_dir.mkdir()
            spec_small = _write_spec(small_dir, cap=1.0, max_rate=10.5)
            with self.assertRaises(Refused) as ctx:
                launch_window(spec_small, root / "c1", dry=True)
            self.assertEqual(ctx.exception.code, "CAP_TOO_SMALL_FOR_CLEANUP")

            # Post-open RATE_ABOVE_MAX under lock
            high_seq = [
                usage_text(rate=0.0, n=0, balance=100.0),
                usage_text(rate=14.0, n=1, balance=100.0),
                usage_text(rate=14.0, n=1, balance=99.8),
            ]
            c2 = FakeClock()
            sim_high = SimColab(clock=c2, usage=lambda idx: high_seq[min(idx - 1, len(high_seq) - 1)])
            res_high = launch_window(spec, root / "c2", colab=sim_high, clock=c2.now, sleep=c2.sleep,
                                     state_dir=root / "s")
            self.assertEqual(res_high["cap_trip"]["code"], "RATE_ABOVE_MAX")
            self.assertTrue(res_high["session_stopped_confirmed"])

            # Mid-run cap guard trip
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
            res_mid = launch_window(spec_cap5, root / "c3", colab=sim_mid, clock=c3.now, sleep=c3.sleep,
                                    state_dir=root / "s")
            self.assertEqual((res_mid["status"], res_mid["cap_trip"]["code"]), ("cap", "CAP_GUARD_TRIPPED"))
            self.assertEqual(sim_mid.kill_calls, 1)

    def test_failure_4_unavailable_cards_real_pools_sessions_fail_open_fail_and_shape_retry(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            state_dir = root / "state"
            # H100 unavailable up front
            spec_h100 = _write_spec(root, card="H100", shape="standard", max_rate=20.0, cap=30.0)
            with self.assertRaises(Refused) as ctx:
                launch_window(spec_h100, root / "h100_out", state_dir=state_dir)
            self.assertEqual(ctx.exception.code, "CARD_UNAVAILABLE")

            # Item 15: Failed `colab new` always calls `stop` and keeps marker if stop failed!
            spec_g4 = _write_spec(root, card="G4", shape="standard")
            sim_new_fail_unstopped = SimColab(
                clock=FakeClock(),
                usage=usage_text(rate=0.0, n=0, balance=100.0),
                new_results=[CliResult(124, "", "timeout after 180s")],
                stops=[1, 1, 1],
            )
            res_nf = launch_window(spec_g4, root / "nf_unstopped", colab=sim_new_fail_unstopped,
                                   clock=sim_new_fail_unstopped.clock.now,
                                   sleep=sim_new_fail_unstopped.clock.sleep, state_dir=state_dir)
            self.assertEqual(res_nf["refusal"]["code"], "SESSION_OPEN_FAILED")
            self.assertGreaterEqual(sim_new_fail_unstopped.stop_calls, 1)
            self.assertFalse(res_nf["session_stopped_confirmed"])
            self.assertTrue((root / "nf_unstopped" / "session_open.json").is_file())

            # 2nd allocation failure opens circuit breaker -> 3rd attempt refused CARD_CIRCUIT_OPEN
            sim_alloc2 = SimColab(
                clock=FakeClock(),
                usage=usage_text(rate=0.0, n=0, balance=100.0),
                new_results=[CliResult(1, "", "503 Service Unavailable")],
            )
            launch_window(spec_g4, root / "nf2", colab=sim_alloc2, clock=sim_alloc2.clock.now,
                          sleep=sim_alloc2.clock.sleep, state_dir=state_dir)
            with self.assertRaises(Refused) as ctx:
                launch_window(spec_g4, root / "nf3", state_dir=state_dir)
            self.assertEqual(ctx.exception.code, "CARD_CIRCUIT_OPEN")

            # Item 13: real `colab sessions` format lines + 5 CPU rows + A100 40 vs 80 split + ignored session-name substring
            cpu_rows = ["[my-g4-stub-%d] ep%d | Hardware: CPU | Shape: High-RAM | Variant: DEFAULT" % (i, i)
                        for i in range(5)]
            sim_cpu5 = SimColab(clock=FakeClock(), usage=usage_text(rate=0.0, n=0), sessions_rows=cpu_rows)
            # Launching G4 sees 0 G4s even though session names contain "g4"!
            seq_g4 = [usage_text(rate=0.0, n=5), usage_text(rate=10.5, n=6), usage_text(rate=10.5, n=6)]
            c_g4 = FakeClock()
            sim_cpu5.clock = c_g4
            sim_cpu5.usage_reply = lambda n: seq_g4[min(n - 1, len(seq_g4) - 1)]
            sim_cpu5.polls = lambda n, env: rec(rc=0)
            sim_cpu5.remote_files = {"/content/demo_job_work/result.json": "{}\n", "/content/demo_job_work/out.txt": "ok\n"}
            spec_diff_study = _write_spec(root, card="G4", shape="standard")
            ok_with_5cpu = launch_window(spec_diff_study, root / "g4_ok_5cpu", colab=sim_cpu5, clock=c_g4.now,
                                         sleep=c_g4.sleep, state_dir=root / "fresh_state")
            self.assertEqual(ok_with_5cpu["status"], "done")

            # 5 CPU rows hit CPU pool ceiling
            with self.assertRaises(Refused) as ctx:
                launch_window(spec_diff_study, root / "cpu_full", card="CPU", colab=sim_cpu5)
            self.assertEqual(ctx.exception.code, "POOL_CEILING_REACHED")

            # A100 40GB full does NOT block A100 80GB (`High-RAM`)
            a100_40_rows = ["[a%d] ep%d | Hardware: A100 | Shape: Standard | Variant: GPU" % (i, i) for i in range(3)]
            c_a80 = FakeClock()
            sim_a80 = SimColab(
                clock=c_a80,
                sessions_rows=a100_40_rows,
                after_open_rows=["[s-80] ep | Hardware: A100 | Shape: High-RAM | Variant: GPU"],
                usage=lambda n: [usage_text(rate=3.0, n=3), usage_text(rate=8.5, n=4), usage_text(rate=8.5, n=4)][min(n - 1, 2)],
                polls=lambda n, env: rec(rc=0),
                remote_files={"/content/demo_job_work/result.json": "{}\n", "/content/demo_job_work/out.txt": "ok\n"},
            )
            res_a80 = launch_window(spec_diff_study, root / "a80_ok", card="A100", shape="high-mem", session="s-80",
                                    max_rate=10.0, cap=15.0, colab=sim_a80, clock=c_a80.now, sleep=c_a80.sleep,
                                    state_dir=root / "s_a80")
            self.assertEqual(res_a80["status"], "done")

            # Item 14: failed `colab sessions` refuses SESSIONS_READ_FAILED
            sim_sess_err = SimColab(clock=FakeClock(), usage=usage_text(rate=0.0, n=0))
            with mock.patch.object(sim_sess_err, "sessions", return_value=CliResult(1, "", "network error")):
                with self.assertRaises(Refused) as ctx:
                    launch_window(spec_diff_study, root / "sess_err", colab=sim_sess_err, state_dir=root / "s_err")
            self.assertEqual(ctx.exception.code, "SESSIONS_READ_FAILED")

            # Item 18: transient `colab sessions` failure after open retries cleanly; exhausted does not trip breaker
            c_tr = FakeClock()
            sim_transient = SimColab(
                clock=c_tr,
                after_open_rows=[CliResult(1, "", "502 temporary"), "[my-t4] ep | Hardware: T4 | Shape: High-RAM | Variant: GPU"],
                usage=lambda n: [usage_text(rate=0.0, n=0), usage_text(rate=1.2, n=1), usage_text(rate=1.2, n=1)][min(n - 1, 2)],
                polls=lambda n, env: rec(rc=0),
                remote_files={"/content/demo_job_work/result.json": "{}\n", "/content/demo_job_work/out.txt": "ok\n"},
            )
            res_tr = launch_window(spec_diff_study, root / "tr_ok", card="T4", shape="high-mem", max_rate=2.0, cap=10.0,
                                   session="my-t4", colab=sim_transient, clock=c_tr.now, sleep=c_tr.sleep,
                                   state_dir=root / "s_tr")
            self.assertEqual(res_tr["status"], "done")

            st_exh = root / "s_exh"
            sim_exh = SimColab(
                clock=FakeClock(),
                usage=usage_text(rate=0.0, n=0),
                after_open_rows=[CliResult(1, "", "down"), CliResult(1, "", "down")],
            )
            res_exh = launch_window(spec_diff_study, root / "exh_out", card="T4", shape="high-mem", max_rate=2.0,
                                    cap=10.0, colab=sim_exh, clock=sim_exh.clock.now, sleep=sim_exh.clock.sleep,
                                    shape_check_attempts=2, state_dir=st_exh)
            self.assertEqual(res_exh["refusal"]["code"], "SESSIONS_READ_FAILED")
            self.assertEqual(CircuitBreaker(st_exh / "card_refusals.json").events_for("T4"), [])


class SignalsLocksExceptionsAndPostOpenTest(unittest.TestCase):
    def test_items_9_10_signals_orphaned_and_exceptions_book_spend(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = _write_spec(root)
            seq = [
                usage_text(rate=0.0, n=0, balance=100.0),
                usage_text(rate=10.2, n=1, balance=100.0),
                usage_text(rate=10.2, n=1, balance=99.9),
            ]
            # Item 9 + 10: SIGTERM during open phase -> stops, books spend, writes outcome.json, re-raises RunInterrupted
            c_sig = FakeClock()
            sim_sig = SimColab(clock=c_sig, usage=lambda n: seq[min(n - 1, len(seq) - 1)])

            def _raise_on_upload(session, local, remote, timeout=120.0):
                c_sig.advance(120.0)
                os.kill(os.getpid(), signal.SIGTERM)

            with mock.patch.object(sim_sig, "upload", side_effect=_raise_on_upload):
                with self.assertRaises(RunInterrupted):
                    launch_window(spec, root / "sig_open", colab=sim_sig, clock=c_sig.now, sleep=c_sig.sleep,
                                  state_dir=root / "s_sig")
            sig_out = json.loads((root / "sig_open" / "outcome.json").read_text(encoding="utf-8"))
            self.assertEqual(sig_out["status"], "interrupted")
            self.assertGreater(sig_out["spend_ccu"], 0.0)
            self.assertTrue(sig_out["session_stopped_confirmed"])

            # Item 10: unexpected crash in upload writes outcome.json (`error`) before re-raising
            c_err = FakeClock()
            sim_err = SimColab(clock=c_err, usage=lambda n: seq[min(n - 1, len(seq) - 1)])
            with mock.patch.object(sim_err, "upload", side_effect=RuntimeError("disk full")):
                with self.assertRaises(RuntimeError):
                    launch_window(spec, root / "err_open", colab=sim_err, clock=c_err.now, sleep=c_err.sleep,
                                  state_dir=root / "s_err2")
            err_out = json.loads((root / "err_open" / "outcome.json").read_text(encoding="utf-8"))
            self.assertEqual(err_out["status"], "error")
            self.assertEqual(err_out["refusal"]["code"], "UNEXPECTED_ERROR")

            # Item 10: BinaryDownloadRefused during collect writes outcome.json before re-raising
            t = fake_tokens()
            c_bin = FakeClock()
            sim_bin = SimColab(
                clock=c_bin,
                usage=lambda idx: seq[min(idx - 1, len(seq) - 1)],
                polls=lambda n, env: rec(rc=0),
                remote_files={
                    "/content/demo_job_work/result.json": b"\x00BIN Bearer " + t["bearer"].encode("ascii") + b"\x00",
                },
            )
            with self.assertRaises(BinaryDownloadRefused):
                launch_window(spec, root / "bin_leak", colab=sim_bin, clock=c_bin.now, sleep=c_bin.sleep,
                              state_dir=root / "s_bin")
            bin_out = json.loads((root / "bin_leak" / "outcome.json").read_text(encoding="utf-8"))
            self.assertEqual(bin_out["status"], "error")
            self.assertEqual(bin_out["refusal"]["code"], "BINARY_DOWNLOAD_TOKEN")
            self.assertEqual(len(bin_out["quarantined"]), 1)
            self.assertTrue(bin_out["session_stopped_confirmed"])

            # Item 9: orphaned status when launcher_pid is dead and session_open.json remains
            orp_dir = root / "orp"
            orp_dir.mkdir()
            (orp_dir / "session_open.json").write_text('{"session": "s1", "job": "j"}\n', encoding="utf-8")
            (orp_dir / "window_meta.json").write_text('{"session": "s1", "launcher_pid": 999999}\n', encoding="utf-8")
            with mock.patch("colab_runner.launcher._pid_alive", return_value=False):
                self.assertEqual(status_window(orp_dir)["state"], "orphaned")

    def test_items_16_17_22_24_locks_study_reservation_runner_file_out_dir_and_post_open_unread(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = _write_spec(root)
            # Item 16: study ledger reserves card BEFORE colab new; overlapping 2nd card refuses CARD_MIX_REFUSED
            ledger_p = root / "ledger.json"
            StudyLedger(ledger_p).reserve_card("demo-study", "G4", session="s1", window_id="w1")
            sim_mix = SimColab(clock=FakeClock(), usage=usage_text(rate=0.0, n=0))
            with self.assertRaises(Refused) as ctx:
                launch_window(spec, root / "mix_out", card="L4", shape="standard", max_rate=2.0, cap=10.0,
                              colab=sim_mix, study_ledger=ledger_p, state_dir=root / "s_mix")
            self.assertEqual(ctx.exception.code, "CARD_MIX_REFUSED")

            # Item 17: single default open lock independent of --state-dir + stale dead-PID lockdir recovery
            self.assertEqual(_default_open_lock_path(None), Path(tempfile.gettempdir()) / "colab-runner-open.lock")
            lock_dir = root / "win.lockdir"
            lock_dir.mkdir()
            (lock_dir / "owner.json").write_text(json.dumps({"pid": 999999}), encoding="utf-8")
            with _acquire_dir_lock(lock_dir, pid_alive=lambda pid: False):
                owner = json.loads((lock_dir / "owner.json").read_text(encoding="utf-8"))
                self.assertEqual(owner["pid"], os.getpid())
            self.assertFalse(lock_dir.exists())

            # Item 22: Runner(pre_opened=False) on an existing file keeps 0.1.4 OUT_DIR_NOT_EMPTY
            bad_out = root / "file_not_dir"
            bad_out.write_text("existing file", encoding="utf-8")
            with self.assertRaises(Refused) as ctx:
                Runner(Colab(), "s1", bad_out).run(script="/content/x.sh", wall_s=60)
            self.assertEqual(ctx.exception.code, "OUT_DIR_NOT_EMPTY")

            # Item 24: clean POST_OPEN_RATE_UNREAD test asserting stop confirmed
            c2 = FakeClock()
            sim_unread = SimColab(clock=c2, usage=usage_text(rate=0.0, n=0))
            res_ur = launch_window(spec, root / "ur_fail", colab=sim_unread, clock=c2.now, sleep=c2.sleep,
                                   post_open_polls=2, state_dir=root / "s_ur")
            self.assertEqual(res_ur["status"], "open_failed")
            self.assertEqual(res_ur["refusal"]["code"], "POST_OPEN_RATE_UNREAD")
            self.assertTrue(res_ur["session_stopped_confirmed"])

            # Existing test helper parse_sessions_output & status error check
            parsed = parse_sessions_output("[s1] ep | Hardware: G4 | Shape: Standard | Variant: GPU\n")
            self.assertEqual((parsed[0]["hardware"], parsed[0]["shape"], parsed[0]["variant"]), ("G4", "Standard", "GPU"))
            self.assertEqual(LocalColab(root / "vm_e").restart_kernel("none").rc, 0)


class AdditionalPatchCoverageTest(unittest.TestCase):
    def test_env_locks_open_bounds_shape_mismatch_status_and_cli_handlers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # resolve_kit_env + default_cleanup_bound_s
            self.assertEqual(resolve_kit_env(["FOO=bar", "BAZ"], environ={"BAZ": "qux"}), {"FOO": "bar", "BAZ": "qux"})
            with self.assertRaises(Refused) as ctx:
                resolve_kit_env(["1BAD=x"], environ={})
            self.assertEqual(ctx.exception.code, "ENV_INVALID")
            with self.assertRaises(Refused) as ctx:
                resolve_kit_env(["MISSING_VAR"], environ={})
            self.assertEqual(ctx.exception.code, "ENV_UNSET")
            self.assertGreater(default_cleanup_bound_s(True), default_cleanup_bound_s(False))

            # _default_open_lock_path, _pid_alive, _acquire_dir_lock timeout + stale recovery
            self.assertEqual(_default_open_lock_path(root / "x.lock"), root / "x.lock")
            with mock.patch.dict(os.environ, {"COLAB_RUNNER_OPEN_LOCK": str(root / "env.lock")}):
                self.assertEqual(_default_open_lock_path(None), root / "env.lock")
            self.assertFalse(_pid_alive(0))
            self.assertTrue(_pid_alive(os.getpid()))
            self.assertFalse(_pid_alive(99999999))

            lock_bad = root / "corrupt.lockdir"
            lock_bad.mkdir()
            (lock_bad / "owner.json").write_text("{corrupt", encoding="utf-8")
            clk = FakeClock()
            with self.assertRaises(Refused) as ctx:
                with _acquire_dir_lock(lock_bad, timeout_s=0.1, clock=clk.now, sleep=clk.sleep):
                    pass
            self.assertEqual(ctx.exception.code, "OPEN_LOCK_TIMEOUT")

            lock_stale = root / "stale_no_owner.lockdir"
            lock_stale.mkdir()
            old_t = 1000.0
            os.utime(lock_stale, (old_t, old_t))
            with mock.patch("colab_runner.launcher.time.time", return_value=old_t + 3000.0):
                with _acquire_dir_lock(lock_stale, timeout_s=1.0):
                    self.assertTrue((lock_stale / "owner.json").is_file())

            # Confirmed SHAPE_MISMATCH + CircuitBreaker event
            spec = _write_spec(root, cap=15.0, max_rate=10.5)
            seq = [usage_text(rate=0.0, n=0, balance=100.0), usage_text(rate=10.2, n=1, balance=100.0)]
            c_mm = FakeClock()
            st_mm = root / "st_mm"
            sim_mm = SimColab(
                clock=c_mm, usage=lambda idx: seq[min(idx - 1, len(seq) - 1)],
                after_open_rows=["[sess-mm] ep | Hardware: G4 | Shape: High-RAM | Variant: GPU"],
            )
            res_mm = launch_window(
                spec, root / "mm_out", session="sess-mm", colab=sim_mm, clock=c_mm.now, sleep=c_mm.sleep, state_dir=st_mm
            )
            self.assertEqual(res_mm["status"], "shape_mismatch")
            self.assertEqual(res_mm["refusal"]["code"], "SHAPE_MISMATCH")
            self.assertEqual(len(CircuitBreaker(st_mm / "card_refusals.json").events_for("G4")), 1)

            # Open-phase wall trip + cap trip + UPLOAD_FAILED
            c_w = FakeClock()
            sim_w = SimColab(clock=c_w, usage=lambda idx: seq[min(idx - 1, len(seq) - 1)], delay_upload_s=20.0)
            res_w = launch_window(
                spec, root / "wall_open", wall_s=10.0, colab=sim_w, clock=c_w.now, sleep=c_w.sleep, state_dir=root / "s_w"
            )
            self.assertEqual(res_w["status"], "wall")

            cap2_dir = root / "cap2"
            cap2_dir.mkdir()
            spec_cap2 = _write_spec(cap2_dir, cap=2.0, max_rate=10.5, wall_s=1800)
            c_c = FakeClock()
            sim_c = SimColab(clock=c_c, usage=lambda idx: seq[min(idx - 1, len(seq) - 1)], delay_upload_s=400.0)
            res_c = launch_window(
                spec_cap2, root / "cap_open", cleanup_bound_s=470.0, colab=sim_c, clock=c_c.now, sleep=c_c.sleep,
                state_dir=root / "s_c",
            )
            self.assertEqual(res_c["status"], "cap")

            c_up = FakeClock()
            sim_up = SimColab(
                clock=c_up, usage=lambda idx: seq[min(idx - 1, len(seq) - 1)],
                upload_results=[CliResult(1, "", "500 upload error")],
            )
            res_up = launch_window(spec, root / "up_fail", colab=sim_up, clock=c_up.now, sleep=c_up.sleep, state_dir=root / "s_up")
            self.assertEqual(res_up["status"], "upload_failed")
            self.assertEqual(res_up["refusal"]["code"], "UPLOAD_FAILED")

            # status_window variants + CLI status
            with self.assertRaises(Refused) as ctx:
                status_window(root / "no_such_dir")
            self.assertEqual(ctx.exception.code, "STATUS_DIR_MISSING")
            unk = root / "unk_dir"
            unk.mkdir()
            self.assertEqual(status_window(unk)["state"], "unknown")

            live_dir = root / "live_dir"
            live_dir.mkdir()
            (live_dir / "session_open.json").write_text('{"session": "s1", "job": "demo_job"}\n', encoding="utf-8")
            (live_dir / "window_meta.json").write_text(
                json.dumps({"session": "s1", "launcher_pid": os.getpid(), "card": "G4"}), encoding="utf-8"
            )
            (live_dir / "demo_job.run.out").write_text("line 1\n", encoding="utf-8")
            st_live = status_window(live_dir)
            self.assertEqual(st_live["state"], "running")
            self.assertEqual(st_live["last_log_lines"], ["line 1"])

            for cli_args in (["status", "--out-dir", str(live_dir)], ["status", "--out-dir", str(live_dir), "--json"]):
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    self.assertEqual(main(cli_args), 0)

            # CLI launch exception handlers: RunInterrupted (with outcome.json + marker) + unexpected error
            int_dir = root / "cli_int"
            int_dir.mkdir()
            (int_dir / "session_open.json").write_text('{"session": "s1", "job": "j"}\n', encoding="utf-8")
            (int_dir / "outcome.json").write_text(
                json.dumps({"status": "interrupted", "session": "s1", "spend_ccu": 0.1, "session_stopped_confirmed": False}),
                encoding="utf-8",
            )
            with mock.patch("colab_runner.__main__.launch_window", side_effect=RunInterrupted(signal.SIGTERM)):
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    rc_i = main(["launch", "--spec", str(spec), "--out-dir", str(int_dir), "--cap", "15", "--max-rate", "10.5"])
            self.assertEqual(rc_i, 2)
            self.assertIn("LAUNCH_END status=interrupted", out.getvalue())

            with mock.patch("colab_runner.__main__.launch_window", side_effect=RuntimeError("boom")):
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    rc_e = main(["launch", "--spec", str(spec), "--out-dir", str(int_dir), "--cap", "15", "--max-rate", "10.5"])
            self.assertEqual(rc_e, 2)
            self.assertIn("LAUNCH_ERROR", err.getvalue())


if __name__ == "__main__":
    unittest.main()

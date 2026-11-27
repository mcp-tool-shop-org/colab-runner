"""Tests for end-to-end launch, status, local-cpu dry run, FIX-1 money/lock/pool fixes, and failure paths."""

import contextlib
import hashlib
import io
import json
import os
import signal
import tempfile
import threading
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest import mock

from colab_runner.__main__ import main
from colab_runner.cards import CARD_CHECK_FAIL_RC, CircuitBreaker, resolve_card
from colab_runner.colab_cli import KILL_WAIT_S, BinaryDownloadRefused, CliResult, Colab, parse_sessions_output
from colab_runner.errors import Refused
from colab_runner.launcher import (
    LocalColab,
    _acquire_dir_lock,
    _check_empty_out_dir,
    _default_open_lock_path,
    _download_declared_outputs,
    _parse_tar_parts,
    _pid_alive,
    _read_pre_open_usage,
    _state_paths,
    _try_acquire_reclaim_guard,
    default_cleanup_bound_s,
    launch_window,
    reconcile_stopped_outcome,
    resolve_kit_env,
    status_window,
)
from colab_runner.runner import RunInterrupted, Runner, _guard_call
from tests.fakes import FakeClock, FakeColab, rec, usage_text
from tests.test_tokens import fake_tokens, leak_lines, values


class SimColab(FakeColab):
    """Extended FakeColab matching real `colab sessions` formatting, new_session, upload, shape, and pool rows."""

    def __init__(self, *, new_results=None, sessions_rows=None, after_open_rows=None, upload_results=None,
                 remote_files=None, delay_upload_s: float = 0.0, delay_new_s: float = 0.0,
                 delay_kill_s: float = 0.0, hang_downloads: bool = False, **kw):
        super().__init__(**kw)
        self.new_results = list(new_results or [])
        self.sessions_rows = list(sessions_rows or [])
        self.after_open_rows = list(after_open_rows) if after_open_rows is not None else None
        self.upload_results = list(upload_results or [])
        self.remote_files = dict(remote_files or {})
        self.delay_upload_s = float(delay_upload_s)
        self.delay_new_s = float(delay_new_s)
        self.delay_kill_s = float(delay_kill_s)
        self.hang_downloads = bool(hang_downloads)
        self.uploads: list[tuple[str, str, str]] = []
        self.opened_sessions: set[str] = set()

    def exec_cell(self, session, source, env=None, timeout=60.0) -> CliResult:
        if "CR_KILL_GRACE_S" in (env or {}) and self.delay_kill_s and self.clock is not None:
            self.clock.advance(self.delay_kill_s)
        if "CR_TAR_REL" in (env or {}):
            self._record("tar", session, timeout)
            self.envs.append(dict(env or {}))
            if self.hang_downloads:
                if self.clock is not None:
                    self.clock.advance(max(0.0, float(timeout)) + 2.0 * KILL_WAIT_S)
                return CliResult(124, "", "tar timed out after %.1fs" % timeout)
            key = "/content/%s_out/%s.tar" % (env.get("CR_JOB", "job"), env["CR_TAR_REL"])
            if key in self.remote_files:
                payload = self.remote_files[key]
                part_key = key + ".part0000"
                self.remote_files[part_key] = payload
                sha = hashlib.sha256(payload).hexdigest()
                return CliResult(0, "CR_TAR_READY\nCR_TAR_PART %s %d %s\n" % (part_key, len(payload), sha))
            return CliResult(0, "CR_TAR_MISSING\n")
        return super().exec_cell(session, source, env=env, timeout=timeout)

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
        if self.clock is not None and self.delay_new_s:
            if self.delay_new_s > timeout:
                self.clock.advance(max(0.0, float(timeout)))
                return CliResult(124, "", "colab new timed out after %.1fs" % timeout)
            self.clock.advance(self.delay_new_s)
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
            if self.delay_upload_s > timeout:
                self.clock.advance(max(0.0, float(timeout)))
                return CliResult(124, "", "upload timed out after %.1fs" % timeout)
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
        name = remote.rsplit("/", 1)[-1]
        if self.hang_downloads and not name.endswith((".poll.json", ".refused.json", ".run.out")):
            self._record("download", session, timeout)
            if self.clock is not None:
                self.clock.advance(max(0.0, float(timeout)) + 2.0 * KILL_WAIT_S)
            return CliResult(124, "", "download timed out after %.1fs" % timeout)
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
                collect=("result.json", "out.txt"), salvage=("setup.log", "vm_card.json"),
                cleanup_bound_s: float | None = None, cpu_cmd: str = "python3 worker.py") -> Path:
    (root / ".git").mkdir(exist_ok=True)
    (root / "worker.py").write_text(
        "import json, os\nfrom pathlib import Path\n"
        "w = Path(os.environ['CR_WORK_DIR'])\n"
        "(w / 'out.txt').write_text('hello worker\\n', encoding='utf-8')\n"
        "(w / 'result.json').write_text(json.dumps({'ok': True}) + '\\n', encoding='utf-8')\n",
        encoding="utf-8",
    )
    shape_line = ('shape = "%s"\n' % shape) if shape else ""
    rate_line = ("max_rate = %s\n" % max_rate) if max_rate is not None else ""
    cleanup_line = ("cleanup_bound_s = %s\n" % cleanup_bound_s) if cleanup_bound_s is not None else ""
    collect_str = json.dumps(list(collect))
    salvage_str = json.dumps(list(salvage))
    spec = root / "colab-window.toml"
    spec.write_text(
        'schema_version = 1\n'
        '[project]\nrepo = "org/demo"\nstudy = "demo-study"\njob = "demo_job"\n'
        '[compute]\n'
        f'card = "{card}"\n{shape_line}{rate_line}'
        f'cap = {cap}\nwall_s = {wall_s}\n{cleanup_line}poll_s = 0.2\n'
        '[inputs]\nroot = "."\nfiles = ["worker.py"]\n'
        '[job]\n'
        'setup = ["echo setup_ok"]\n'
        'run = ["python3 worker.py"]\n'
        f'cpu_dry_run = ["{cpu_cmd}"]\n'
        f'[outputs]\ncollect = {collect_str}\nsalvage = {salvage_str}\n',
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
            self.assertIn("(= cap 15.0)", stdout_txt)
            self.assertIn("worst_case_launcher_death_path: unbounded until stop --out-dir", stdout_txt)
            plan = json.loads((out_dir / "dry_run_plan.json").read_text(encoding="utf-8"))
            self.assertGreater(plan["budget"]["open_phase_bound_s"], 0.0)
            self.assertLessEqual(plan["budget"]["worst_case_total_spend"], plan["budget"]["cap"])
            self.assertEqual(plan["budget"]["worst_case_launcher_death_path"], "unbounded until stop --out-dir")
            self.assertEqual(status_window(out_dir)["state"], "dry_run")

            # With non-zero margin, cap_path still equals cap (= margin + effective_trip + cleanup_reserve) <= cap
            dry_m = root / "dry_margin"
            out_m, err_m = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out_m), contextlib.redirect_stderr(err_m):
                rc_m = main(["launch", "--spec", str(spec), "--out-dir", str(dry_m), "--dry", "--margin", "0.25"])
            self.assertEqual(rc_m, 0, err_m.getvalue())
            self.assertIn("(= cap 15.0)", out_m.getvalue())
            plan_m = json.loads((dry_m / "dry_run_plan.json").read_text(encoding="utf-8"))
            self.assertLessEqual(plan_m["budget"]["worst_case_total_spend"], plan_m["budget"]["cap"])

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
            prereg = root / "prereg.txt"
            (root / "sealed-eval").mkdir()
            sealed_file = root / "sealed-eval" / "item.txt"
            sealed_file.write_text("ok\n", encoding="utf-8")
            prereg_raw = (
                b"study: demo-study\n"
                b"allowed_cards: G4\n"
                b"allowed_host: colab.google.com\n"
                b"sealed_path: sealed-eval/item.txt\n"
            )
            prereg.write_bytes(prereg_raw)
            prereg.chmod(0o444)
            try:
                spec = _write_spec(root, cpu_cmd="python3 leak_worker.py")
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
                self.assertEqual(res["sealed"]["resolved_paths"], [str(sealed_file.resolve())])
                self.assertEqual(len(res["sealed"]["files"]), 1)
                self.assertEqual(
                    res["sealed"]["files"][0]["sha256"],
                    hashlib.sha256(sealed_file.read_bytes()).hexdigest(),
                )
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

            # Item 3 (FIX-2): long open-phase uploads (115s + 115s = 230s) counts against cap in Runner.run;
            # booked spend_ccu stays strictly <= cap (2.925) because cap counts from before colab new.
            c_cap = FakeClock()
            spec_tight = _write_spec(root, cap=2.925, max_rate=10.5, wall_s=3600, collect=(), salvage=())
            sim_slow_open = SimColab(
                clock=c_cap,
                delay_upload_s=115.0,  # 2 uploads = 230s during open phase before Runner.run starts
                usage=lambda n: usage_replies[min(n - 1, len(usage_replies) - 1)],
                polls=lambda n, env: (c_cap.advance(90.0) or rec(progress=n)),
            )
            out_open_cap = launch_window(
                spec_tight, root / "open_cap_out", colab=sim_slow_open, clock=c_cap.now, sleep=c_cap.sleep,
                cleanup_bound_s=600.0, state_dir=root / "state2",
            )
            self.assertEqual(out_open_cap["status"], "cap")
            self.assertLess(out_open_cap["elapsed_s"], 500.0)
            self.assertLessEqual(out_open_cap["spend_ccu"], 2.925)
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
            ]:
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    rc = main(["launch", "--spec", str(spec), "--out-dir", str(root / ("m_" + want_code))] + bad_flags)
                self.assertEqual(rc, 1)
                self.assertIn("REFUSED: " + want_code, err.getvalue())

            # Addendum 3: max-rate below catalog floor logs a note instead of refusing
            floor_logs = []
            plan_floor = launch_window(spec, root / "m_floor_ok", dry=True, cap=15.0, max_rate=2.0, log=floor_logs.append)
            self.assertEqual(plan_floor["mode"], "dry_run")
            self.assertTrue(any("catalog floor" in ln for ln in floor_logs))

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
                launch_window(spec_diff_study, root / "cpu_full", card="CPU", colab=sim_cpu5, state_dir=root / "s_cpu")
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
            logs_exh: list[str] = []
            sim_exh = SimColab(
                clock=FakeClock(),
                usage=usage_text(rate=0.0, n=0),
                after_open_rows=[CliResult(1, "", "down"), CliResult(1, "", "down")],
                polls=lambda n, env: rec(rc=0),
                remote_files={"/content/demo_job_work/result.json": "{}\n", "/content/demo_job_work/out.txt": "ok\n"},
            )
            res_exh = launch_window(spec_diff_study, root / "exh_out", card="T4", shape="high-mem", max_rate=2.0,
                                    cap=10.0, colab=sim_exh, clock=sim_exh.clock.now, sleep=sim_exh.clock.sleep,
                                    shape_check_attempts=2, state_dir=st_exh, log=logs_exh.append)
            self.assertEqual(res_exh["status"], "done")
            self.assertTrue(any("inconclusive" in ln for ln in logs_exh))
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
            # Item 13 (FIX-2): real two-window test proving reserve_card runs under lock BEFORE record_window
            ledger_p = root / "ledger.json"
            lock_16 = root / "item16.lock"
            entered_run = threading.Event()
            release_run = threading.Event()
            seq_16 = [usage_text(rate=0.0, n=0), usage_text(rate=10.2, n=1), usage_text(rate=10.2, n=1)]

            def _blocking_polls(n, env):
                entered_run.set()
                release_run.wait(timeout=5.0)
                return rec(rc=0)

            c_w1 = FakeClock()
            sim_w1 = SimColab(
                clock=c_w1,
                usage=lambda n: seq_16[min(n - 1, len(seq_16) - 1)],
                polls=_blocking_polls,
                remote_files={"/content/demo_job_work/result.json": "{}\n", "/content/demo_job_work/out.txt": "ok\n"},
            )
            w1_box: list[dict] = []
            w1_err: list[BaseException] = []

            def _run_w1():
                try:
                    w1_box.append(launch_window(
                        spec, root / "w1_res_out", colab=sim_w1, clock=c_w1.now, sleep=c_w1.sleep,
                        study_ledger=ledger_p, state_dir=root / "s_w1", open_lock=lock_16,
                    ))
                except BaseException as exc:
                    w1_err.append(exc)

            t1 = threading.Thread(target=_run_w1)
            t1.start()
            self.assertTrue(entered_run.wait(timeout=5.0), "window 1 never reached Runner.run")
            try:
                # Window 1 is mid-run (record_window has NOT run yet), so G4 is locked solely via reserve_card.
                ledger_snap = json.loads(ledger_p.read_text(encoding="utf-8"))
                self.assertEqual(ledger_snap["studies"]["demo-study"]["card"], "G4")
                self.assertEqual(len(ledger_snap["studies"]["demo-study"]["windows"]), 0)
                sim_mix = SimColab(clock=FakeClock(), usage=usage_text(rate=0.0, n=0))
                with self.assertRaises(Refused) as ctx:
                    launch_window(
                        spec, root / "mix_out", card="L4", shape="standard", max_rate=2.0, cap=10.0,
                        colab=sim_mix, study_ledger=ledger_p, state_dir=root / "s_mix", open_lock=lock_16,
                    )
                self.assertEqual(ctx.exception.code, "CARD_MIX_REFUSED")
            finally:
                release_run.set()
                t1.join(timeout=5.0)
            self.assertFalse(w1_err, w1_err)
            self.assertEqual(w1_box[0]["status"], "done")

            # Item 11 + Item 17: default user state & open lock live outside tempdir; stale lockdir recovery works
            def_state, _ = _state_paths(None, None, live=True)
            self.assertIsNotNone(def_state)
            self.assertNotIn(Path(tempfile.gettempdir()), def_state.parents)
            self.assertNotEqual(def_state, Path(tempfile.gettempdir()) / "colab-runner-state")
            self.assertEqual(_default_open_lock_path(None), def_state / "open.lock")
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

            # Addendum 3: unsettled post-open usage rate logs and continues (no POST_OPEN_RATE_UNREAD kill)
            c2 = FakeClock()
            unread_logs = []
            sim_unread = SimColab(
                clock=c2,
                usage=usage_text(rate=0.0, n=0),
                polls=lambda n, env: rec(rc=0, progress=1),
                remote_files={
                    "/content/demo_job_work/result.json": json.dumps({"ok": True}).encode("utf-8"),
                    "/content/demo_job_work/out.txt": b"done\n",
                    "/content/demo_job_out/vm_card.json": json.dumps({"ok": True}).encode("utf-8"),
                },
            )
            res_ur = launch_window(spec, root / "ur_ok", colab=sim_unread, clock=c2.now, sleep=c2.sleep,
                                   post_open_polls=2, state_dir=root / "s_ur", log=unread_logs.append)
            self.assertEqual(res_ur["status"], "done")
            self.assertTrue(res_ur["session_stopped_confirmed"])
            self.assertTrue(any("not settled" in ln for ln in unread_logs))

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
            spec_cap2 = _write_spec(cap2_dir, cap=2.5, max_rate=10.5, wall_s=1800, collect=(), salvage=())
            c_c = FakeClock()
            sim_c = SimColab(clock=c_c, usage=lambda idx: seq[min(idx - 1, len(seq) - 1)], delay_upload_s=115.0)
            res_c = launch_window(
                spec_cap2, root / "cap_open", cleanup_bound_s=600.0, colab=sim_c, clock=c_c.now, sleep=c_c.sleep,
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

            # LocalColab edge branches (_translate_remote, closed session upload/exec, nested upload, start_cell + kill)
            lc = LocalColab(root / "lc_cov")
            self.assertEqual(lc._translate_remote("/content"), lc.content_dir)
            self.assertEqual(lc._translate_remote("rel.txt"), (lc.content_dir / "rel.txt").resolve())
            with self.assertRaises(ValueError):
                lc._translate_remote("/content/../outside")
            with self.assertRaises(ValueError):
                lc._translate_remote("../outside")
            with self.assertRaises(ValueError):
                lc._translate_remote(str((root / "outside").resolve()))
            self.assertEqual(lc.upload("closed", __file__, "/content/a").rc, 1)
            self.assertEqual(lc.exec_cell("closed", "print(1)").rc, 1)
            lc.new_session("s", card="CPU")
            self.assertEqual(lc.upload("s", __file__, "/content/sub/a").rc, 1)
            bg = lc.start_cell("s", "print('ok')", timeout=5.0)
            bg.kill()

            # Launcher helper edges (_check_empty_out_dir, _read_pre_open_usage, reconcile_stopped_outcome)
            f_out = root / "f_out"
            f_out.write_text("x", encoding="utf-8")
            with self.assertRaises(Refused) as ctx:
                _check_empty_out_dir(f_out)
            self.assertEqual(ctx.exception.code, "OUT_DIR_INVALID")

            c_fail = SimColab(clock=FakeClock(), usage=lambda n: CliResult(1, "", "err"))
            pre_logs: list[str] = []
            self.assertEqual(_read_pre_open_usage(c_fail, card_spec=resolve_card("G4"), log=pre_logs.append), (None, None, None))
            self.assertEqual(_read_pre_open_usage(c_fail, card_spec=resolve_card("CPU"), log=pre_logs.append), (None, None, None))
            self.assertEqual(
                _read_pre_open_usage(SimColab(clock=FakeClock(), usage="unparseable"), card_spec=resolve_card("G4"), log=pre_logs.append),
                (None, None, None),
            )
            self.assertEqual(
                _read_pre_open_usage(
                    SimColab(clock=FakeClock(), usage="Current balance: 100.00 compute units\nUsage rate: 0.00 / hr\n"),
                    card_spec=resolve_card("G4"),
                    log=pre_logs.append,
                ),
                (None, None, 100.0),
            )
            self.assertGreaterEqual(len(pre_logs), 4)

            self.assertIsNone(reconcile_stopped_outcome(root / "missing_reconcile", c_fail, "s1"))
            bad_rec_dir = root / "bad_rec"
            bad_rec_dir.mkdir()
            (bad_rec_dir / "outcome.json").write_text("{bad", encoding="utf-8")
            self.assertIsNone(reconcile_stopped_outcome(bad_rec_dir, c_fail, "s1"))
            (bad_rec_dir / "outcome.json").write_text("[1]", encoding="utf-8")
            self.assertIsNone(reconcile_stopped_outcome(bad_rec_dir, c_fail, "s1"))

            # CLI __main__ branches: STATUS_DIR_MISSING, empty out_dir RunInterrupted, refusal+cap_trip+marker hints
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                self.assertEqual(main(["status", "--out-dir", str(root / "missing_status_dir")]), 1)
            self.assertIn("REFUSED: STATUS_DIR_MISSING", err.getvalue())

            empty_cli = root / "empty_cli"
            empty_cli.mkdir()
            with mock.patch("colab_runner.__main__.launch_window", side_effect=RunInterrupted(signal.SIGTERM)):
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    self.assertEqual(
                        main(["launch", "--spec", str(spec), "--out-dir", str(empty_cli), "--cap", "15", "--max-rate", "10.5"]),
                        2,
                    )
            self.assertIn("LAUNCH_ERROR", err.getvalue())

            hint_cli = root / "hint_cli"
            hint_cli.mkdir()
            (hint_cli / "session_open.json").write_text('{"session": "s1", "job": "j"}\n', encoding="utf-8")
            fake_res = {
                "status": "cap",
                "session": "s1",
                "spend_ccu": 0.2,
                "session_stopped_confirmed": False,
                "exit_code": 2,
                "refusal": {"code": "SESSION_OPEN_FAILED", "hint": "h1"},
                "cap_trip": {"code": "CAP_GUARD_TRIPPED", "hint": "h2"},
            }
            with mock.patch("colab_runner.__main__.launch_window", return_value=fake_res):
                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    self.assertEqual(
                        main(["launch", "--spec", str(spec), "--out-dir", str(hint_cli), "--cap", "15", "--max-rate", "10.5"]),
                        2,
                    )
            self.assertIn("hint: h1", err.getvalue())
            self.assertIn("hint: h2", err.getvalue())


class Fix2CapTimingOrphanAndLockTest(unittest.TestCase):
    def test_failed_open_books_spend_and_releases_card_to_allow_retry_on_a100(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = _write_spec(root, card="G4", shape="standard", cap=15.0, max_rate=10.5)
            ledger_p = root / "ledger.json"
            lock_p = root / "lock"

            # Item 1 + Item 9: G4 `colab new` runs 36s then fails; spends >0 from pre-new clock and releases card!
            c1 = FakeClock()
            sim_fail = SimColab(
                clock=c1, delay_new_s=36.0, usage=usage_text(rate=0.0, n=0, balance=100.0),
                new_results=[CliResult(1, "", "capacity exhausted")],
            )
            res_fail = launch_window(
                spec, root / "g4_fail", colab=sim_fail, clock=c1.now, sleep=c1.sleep,
                study_ledger=ledger_p, state_dir=root / "st", open_lock=lock_p,
            )
            self.assertEqual(res_fail["status"], "open_failed")
            self.assertFalse(res_fail["opened"])
            self.assertIsNotNone(res_fail["opened_at"])
            self.assertGreater(res_fail["spend_ccu"], 0.0)
            self.assertTrue(res_fail["session_stopped_confirmed"])

            # Now the SAME study reserves and succeeds on A100!
            c2 = FakeClock()
            seq_a = [usage_text(rate=0.0, n=0, balance=100.0), usage_text(rate=10.0, n=1, balance=99.9)]
            sim_a100 = SimColab(
                clock=c2,
                after_open_rows=["[s-a100] ep | Hardware: A100 | Shape: Standard | Variant: GPU"],
                usage=lambda idx: seq_a[min(idx - 1, len(seq_a) - 1)],
                polls=lambda n, env: (c2.advance(10.0) or rec(rc=0)),
                remote_files={"/content/demo_job_work/result.json": "{}\n", "/content/demo_job_work/out.txt": "ok\n"},
            )
            res_ok = launch_window(
                spec, root / "a100_ok", card="A100", shape="standard", max_rate=12.0, cap=15.0,
                session="s-a100", colab=sim_a100, clock=c2.now, sleep=c2.sleep,
                study_ledger=ledger_p, state_dir=root / "st", open_lock=lock_p,
            )
            self.assertEqual(res_ok["status"], "done")
            ledger_data = json.loads(ledger_p.read_text(encoding="utf-8"))
            self.assertEqual(ledger_data["studies"]["demo-study"]["card"], "A100")

            # Item 1 reconcile even when opened=False (failed open whose stop later confirms)
            unstopped_dir = root / "fail_unstopped"
            c3 = FakeClock()
            sim_us = SimColab(
                clock=c3, delay_new_s=36.0, usage=usage_text(rate=0.0, n=0, balance=100.0),
                new_results=[CliResult(124, "", "timeout")], stops=[1, 1, 1, 0],
            )
            res_us = launch_window(
                spec, unstopped_dir, colab=sim_us, clock=c3.now, sleep=c3.sleep, state_dir=root / "st2", open_lock=lock_p
            )
            self.assertFalse(res_us["opened"])
            self.assertFalse(res_us["session_stopped_confirmed"])
            self.assertGreater(res_us["spend_ccu"], 0.0)
            past_open = datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() - 600.0, tz=timezone.utc)
            raw_us = json.loads((unstopped_dir / "outcome.json").read_text(encoding="utf-8"))
            raw_us["opened_at"] = past_open.strftime("%Y-%m-%dT%H:%M:%SZ")
            (unstopped_dir / "outcome.json").write_text(json.dumps(raw_us), encoding="utf-8")
            with mock.patch("colab_runner.__main__.cli_from_env", return_value=sim_us):
                self.assertEqual(main(["stop", "--out-dir", str(unstopped_dir), "--quiet"]), 0)
            rec_us = json.loads((unstopped_dir / "outcome.json").read_text(encoding="utf-8"))
            self.assertTrue(rec_us["spend_basis"].endswith("_reconciled"))
            self.assertGreater(rec_us["elapsed_s"], 500.0)

    def test_item_2_open_phase_timeout_clamping_never_overruns_cleanup_reserve(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # cap=2.5, cleanup=600 (own 466 + vm_card 130 = 596) -> reserve=1.75 -> ceiling=0.75 -> 257.14s at 10.5/h
            spec = _write_spec(root, card="G4", shape="standard", cap=2.5, max_rate=10.5, wall_s=1800,
                               collect=(), salvage=())
            c = FakeClock()
            seq = [usage_text(rate=0.0, n=0, balance=100.0), usage_text(rate=10.5, n=1, balance=99.9)]
            # Upload 1 completes in 115s; Upload 2 gets clamped (< 120s) and hits cap before overrunning reserve.
            sim = SimColab(clock=c, delay_upload_s=115.0, usage=lambda idx: seq[min(idx - 1, len(seq) - 1)])
            res = launch_window(
                spec, root / "clamp_out", cleanup_bound_s=600.0, colab=sim, clock=c.now, sleep=c.sleep,
                state_dir=root / "st",
            )
            self.assertEqual(res["status"], "cap")
            reserve = 600.0 / 3600.0 * 10.5
            self.assertLessEqual(res["spend_ccu"] + reserve, 2.5 + 1e-5)

    def test_item_5_orphan_booking_from_window_meta_and_marker_mtime(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ledger_p = root / "ledger.json"
            lock_p = root / "lock"
            orp = root / "orphan_win"
            orp.mkdir()
            started = datetime.fromtimestamp(datetime.now(timezone.utc).timestamp() - 1800.0, tz=timezone.utc)
            (orp / "window_meta.json").write_text(json.dumps({
                "window_id": "job-orp1",
                "session": "s-orp1",
                "launcher_pid": 999999,
                "project": {"repo": "org/r", "study": "orp-study", "job": "job", "description": ""},
                "card": "G4",
                "card_class": "gpu",
                "shape": "standard",
                "mode": "live",
                "started_at": started.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "cap_ccu": 15.0,
                "max_rate": 10.5,
                "study_ledger": str(ledger_p),
                "open_lock": str(lock_p),
            }), encoding="utf-8")
            (orp / "session_open.json").write_text('{"session": "s-orp1", "job": "job"}\n', encoding="utf-8")
            sim = SimColab(clock=FakeClock(), usage=usage_text(rate=0.0, n=0, balance=95.0))
            with mock.patch("colab_runner.__main__.cli_from_env", return_value=sim):
                self.assertEqual(main(["stop", "--out-dir", str(orp), "--quiet"]), 0)
            out = json.loads((orp / "outcome.json").read_text(encoding="utf-8"))
            self.assertEqual(out["status"], "orphaned")
            self.assertEqual(out["spend_basis"], "max_rate_x_elapsed_orphan_reconciled")
            self.assertGreater(out["elapsed_s"], 1700.0)
            self.assertGreater(out["spend_ccu"], 4.5)
            ledger_data = json.loads(ledger_p.read_text(encoding="utf-8"))
            self.assertEqual(ledger_data["studies"]["orp-study"]["windows"][0]["status"], "orphaned")

            # Fallback to session_open.json mtime when window_meta lacks started_at
            orp2 = root / "orphan_mtime"
            orp2.mkdir()
            (orp2 / "window_meta.json").write_text(json.dumps({
                "window_id": "job-orp2", "session": "s-orp2", "card": "CPU", "card_class": "cpu", "max_rate": 0.26,
            }), encoding="utf-8")
            marker2 = orp2 / "session_open.json"
            marker2.write_text('{"session": "s-orp2", "job": "job"}\n', encoding="utf-8")
            past_m = datetime.now(timezone.utc).timestamp() - 900.0
            os.utime(marker2, (past_m, past_m))
            with mock.patch("colab_runner.__main__.cli_from_env", return_value=sim):
                self.assertEqual(main(["stop", "--out-dir", str(orp2), "--quiet"]), 0)
            out2 = json.loads((orp2 / "outcome.json").read_text(encoding="utf-8"))
            self.assertEqual(out2["spend_basis"], "max_rate_x_elapsed_orphan_reconciled")
            self.assertGreater(out2["elapsed_s"], 800.0)

    def test_item_10_and_12_atomic_lock_takeover_and_gapless_cleanup_signals(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Item 10: two waiters racing to reclaim one dead-PID lockdir -> exactly one acquires, one times out
            lock_dir = root / "race.lockdir"
            lock_dir.mkdir()
            (lock_dir / "owner.json").write_text(json.dumps({"pid": 999999}), encoding="utf-8")
            barrier = threading.Barrier(2)
            results: list[str] = []
            res_lock = threading.Lock()

            def _waiter():
                barrier.wait(timeout=5.0)
                try:
                    with _acquire_dir_lock(lock_dir, timeout_s=0.15, pid_alive=lambda pid: pid == os.getpid()):
                        with res_lock:
                            results.append("acquired")
                        threading.Event().wait(0.25)
                except Refused as exc:
                    with res_lock:
                        results.append(exc.code)

            t1 = threading.Thread(target=_waiter)
            t2 = threading.Thread(target=_waiter)
            t1.start()
            t2.start()
            t1.join(timeout=5.0)
            t2.join(timeout=5.0)
            self.assertEqual(sorted(results), ["OPEN_LOCK_TIMEOUT", "acquired"])

            # Item 12a: SIGTERM arriving during open-phase cleanup (stop) is deferred until outcome.json is saved
            spec = _write_spec(root)
            seq = [usage_text(rate=0.0, n=0, balance=100.0), usage_text(rate=10.2, n=1, balance=99.9)]
            c_def = FakeClock()
            sim_def = SimColab(clock=c_def, usage=lambda idx: seq[min(idx - 1, len(seq) - 1)])

            def _stop_with_sig(session, timeout=120.0):
                sim_def.stops.append((session, 0))
                os.kill(os.getpid(), signal.SIGTERM)
                return CliResult(0, "")

            with mock.patch.object(sim_def, "new_session", return_value=CliResult(1, "", "503")), \
                    mock.patch.object(sim_def, "stop", side_effect=_stop_with_sig):
                with self.assertRaises(RunInterrupted):
                    launch_window(
                        spec, root / "def_sig_out", colab=sim_def, clock=c_def.now, sleep=c_def.sleep,
                        state_dir=root / "st_def",
                    )
            self.assertTrue((root / "def_sig_out" / "outcome.json").is_file())

            # Item 12b: SIGTERM in the gap before Runner._arm_signals is still caught and books spend
            class GapSignalRunner(Runner):
                def run(self, *args, **kwargs):
                    os.kill(os.getpid(), signal.SIGTERM)

            c_gap = FakeClock()
            sim_gap = SimColab(clock=c_gap, usage=lambda idx: seq[min(idx - 1, len(seq) - 1)])
            with self.assertRaises(RunInterrupted):
                launch_window(
                    spec, root / "gap_sig_out", colab=sim_gap, runner_cls=GapSignalRunner,
                    clock=c_gap.now, sleep=c_gap.sleep, state_dir=root / "st_gap",
                )
            gap_out = json.loads((root / "gap_sig_out" / "outcome.json").read_text(encoding="utf-8"))
            self.assertEqual(gap_out["status"], "interrupted")
            self.assertTrue(gap_out["session_stopped_confirmed"])


class Fix3CleanupCapCardLockAndReclaimTest(unittest.TestCase):
    EXAMPLE_SPEC = Path(__file__).resolve().parents[1] / "examples" / "merchant_league_seats" / "colab-window.toml"

    def test_item_1_cleanup_bound_covers_outputs_and_hanging_salvage_or_collect_stay_under_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Item 1a: T4 smoke spec (vm_card + 3 salvage outputs) refuses explicit --cleanup-bound-s below 1376.0s
            for bad_bound in (600.0, 1375.0):
                with self.assertRaises(Refused) as ctx:
                    launch_window(
                        self.EXAMPLE_SPEC, root / ("small_%d" % int(bad_bound)), dry=True,
                        card="T4", shape="standard", max_rate=1.5, cap=2.5, cleanup_bound_s=bad_bound,
                    )
                self.assertEqual(ctx.exception.code, "CLEANUP_BOUND_TOO_SMALL")

            # Dry run on T4 smoke spec reports >= 1376.0 cleanup_bound_s and worst_case_total_spend <= 2.5
            dry_plan = launch_window(
                self.EXAMPLE_SPEC, root / "t4_dry", dry=True,
                card="T4", shape="standard", max_rate=1.5, wall_s=180.0, cap=2.5,
            )
            self.assertGreaterEqual(dry_plan["budget"]["cleanup_bound_s"], 1976.0)
            self.assertGreaterEqual(dry_plan["budget"]["cleanup_outputs_bound_s"], 1510.0)
            self.assertLessEqual(dry_plan["budget"]["worst_case_total_spend"], 2.5)

            t4_seq = [usage_text(rate=0.0, n=0, balance=100.0), usage_text(rate=1.5, n=1, balance=99.9)]

            # Item 1b: cap trip with every salvage + vm_card download hanging (plus 2 failed stops before stop 3 ok)
            c_trip = FakeClock()
            sim_trip = SimColab(
                clock=c_trip,
                after_open_rows=["[t4-hang] ep | Hardware: T4 | Shape: Standard | Variant: GPU"],
                usage=lambda idx: t4_seq[min(idx - 1, len(t4_seq) - 1)],
                polls=lambda n, env: (c_trip.advance(600.0) or rec(progress=n)),
                stops=[1, 1, 0],
                hang_downloads=True,
            )
            out_trip = launch_window(
                self.EXAMPLE_SPEC, root / "t4_trip_hang", session="t4-hang",
                card="T4", shape="standard", max_rate=1.5, cap=2.5, wall_s=7200.0,
                colab=sim_trip, clock=c_trip.now, sleep=c_trip.sleep,
                state_dir=root / "st_trip", open_lock=root / "trip.lock",
            )
            self.assertEqual(out_trip["status"], "cap")
            self.assertTrue(out_trip["session_stopped_confirmed"])
            self.assertLessEqual(out_trip["spend_ccu"], 2.5)

            # Item 1c: `done` path (with margin=0.0 and polls right up near ceiling) with every collect download hanging
            c_done = FakeClock()
            poll_ticks = {"n": 0}

            def _done_polls(n, env):
                poll_ticks["n"] += 1
                c_done.advance(1160.0)
                return rec(progress=n, rc=0 if poll_ticks["n"] >= 4 else None)

            sim_done = SimColab(
                clock=c_done,
                after_open_rows=["[t4-done] ep | Hardware: T4 | Shape: Standard | Variant: GPU"],
                usage=lambda idx: t4_seq[min(idx - 1, len(t4_seq) - 1)],
                polls=_done_polls,
                stops=[1, 1, 0],
                hang_downloads=True,
            )
            out_done = launch_window(
                self.EXAMPLE_SPEC, root / "t4_done_hang", session="t4-done",
                card="T4", shape="standard", max_rate=1.5, cap=2.5, wall_s=7200.0, margin=0.0,
                colab=sim_done, clock=c_done.now, sleep=c_done.sleep,
                state_dir=root / "st_done", open_lock=root / "done.lock",
            )
            self.assertEqual(out_done["status"], "partial")
            self.assertEqual(out_done["exit_code"], 2)
            self.assertEqual(out_done["missing_outputs"], ["turn_summary.json", "from-seat"])
            self.assertTrue(out_done["session_stopped_confirmed"])
            self.assertLessEqual(out_done["spend_ccu"], 2.5)
            dl_calls = [c for c in sim_done.calls if c["kind"] == "download"]
            self.assertGreaterEqual(len(dl_calls), 1)

    def test_item_2_unstopped_failed_open_keeps_card_locked_and_item_4_dead_reclaim_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = _write_spec(root, card="G4", shape="standard", cap=15.0, max_rate=10.5)
            ledger_p = root / "ledger.json"
            lock_p = root / "lock"

            # Item 2 (FIX-3): failed open whose stops all fail leaves session_stopped_confirmed=False -> card stays locked
            c_fail = FakeClock()
            sim_unstopped = SimColab(
                clock=c_fail, delay_new_s=36.0, usage=usage_text(rate=0.0, n=0, balance=100.0),
                new_results=[CliResult(124, "", "timeout")], stops=[1, 1, 1],
            )
            res_us = launch_window(
                spec, root / "unstopped_g4", colab=sim_unstopped, clock=c_fail.now, sleep=c_fail.sleep,
                study_ledger=ledger_p, state_dir=root / "st_us", open_lock=lock_p,
            )
            self.assertEqual(res_us["status"], "open_failed")
            self.assertFalse(res_us["session_stopped_confirmed"])
            ledger_snap = json.loads(ledger_p.read_text(encoding="utf-8"))
            self.assertEqual(ledger_snap["studies"]["demo-study"]["card"], "G4")
            self.assertEqual(len(ledger_snap["studies"]["demo-study"]["reservations"]), 1)

            with self.assertRaises(Refused) as ctx:
                launch_window(
                    spec, root / "a100_blocked", card="A100", shape="standard", max_rate=12.0, cap=15.0,
                    colab=SimColab(clock=FakeClock(), usage=usage_text(rate=0.0, n=0)),
                    study_ledger=ledger_p, state_dir=root / "st_us", open_lock=lock_p,
                )
            self.assertEqual(ctx.exception.code, "CARD_MIX_REFUSED")

            # Item 4 (FIX-3): dead-PID `.reclaim` guard left by a crashed reclaimer is recovered, not leaked forever
            lock_dir = root / "reclaim_race.lockdir"
            lock_dir.mkdir()
            (lock_dir / "owner.json").write_text(json.dumps({"pid": 999999, "at": "2026-11-26T00:00:00Z"}), encoding="utf-8")
            reclaim_guard = lock_dir.with_suffix(".reclaim")
            reclaim_guard.mkdir()
            (reclaim_guard / "owner.json").write_text(
                json.dumps({"pid": 888888, "at": "2026-11-26T00:00:00Z"}), encoding="utf-8"
            )
            with _acquire_dir_lock(lock_dir, timeout_s=1.0, pid_alive=lambda pid: pid == os.getpid()):
                self.assertEqual(json.loads((lock_dir / "owner.json").read_text(encoding="utf-8"))["pid"], os.getpid())
                self.assertFalse(reclaim_guard.exists())
            self.assertFalse(lock_dir.exists())

            # A live PID holding `.reclaim` is respected and never stolen -> OPEN_LOCK_TIMEOUT
            lock_dir2 = root / "reclaim_live.lockdir"
            lock_dir2.mkdir()
            (lock_dir2 / "owner.json").write_text(json.dumps({"pid": 999999, "at": "2026-11-26T00:00:00Z"}), encoding="utf-8")
            reclaim_guard2 = lock_dir2.with_suffix(".reclaim")
            reclaim_guard2.mkdir()
            (reclaim_guard2 / "owner.json").write_text(
                json.dumps({"pid": os.getpid(), "at": "2026-11-26T00:00:00Z"}), encoding="utf-8"
            )
            clk2 = FakeClock()
            with self.assertRaises(Refused) as ctx:
                with _acquire_dir_lock(
                    lock_dir2, timeout_s=0.1, clock=clk2.now, sleep=clk2.sleep,
                    pid_alive=lambda pid: pid == os.getpid(),
                ):
                    pass
            self.assertEqual(ctx.exception.code, "OPEN_LOCK_TIMEOUT")


class FollowupFenceReclaimRaceAndClampTest(unittest.TestCase):
    EXAMPLE_SPEC = Path(__file__).resolve().parents[1] / "examples" / "merchant_league_seats" / "colab-window.toml"

    def test_fu_1_indented_fence_inside_markdown_prereg_refuses_a100_on_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            (repo / ".git").mkdir(parents=True)
            (repo / "worker.py").write_text("print('ok')\n", encoding="utf-8")
            (repo / "sealed-eval").mkdir()
            (repo / "sealed-eval" / "item.json").write_text("{}\n", encoding="utf-8")
            prereg = repo / "prereg.md"
            prereg_raw = (
                "study: demo-study\n"
                "allowed_host: colab.google.com\n"
                "sealed_path: sealed-eval/item.json\n\n"
                "## Example of another study's config\n"
                "```markdown\n"
                "    ```\n"
                "allowed_cards: A100\n"
                "```\n\n"
                "allowed_cards: G4\n"
            ).encode("utf-8")
            prereg.write_bytes(prereg_raw)
            prereg.chmod(0o444)
            try:
                spec = repo / "colab-window.toml"
                spec.write_text(
                    'schema_version = 1\n'
                    '[project]\nrepo = "org/demo"\nstudy = "demo-study"\njob = "demo_job"\n'
                    '[compute]\ncard = "G4"\nshape = "standard"\nmax_rate = 10.5\ncap = 15.0\nwall_s = 600\n'
                    '[inputs]\nroot = "."\nfiles = ["worker.py", "sealed-eval/item.json"]\n'
                    '[inputs.sealed]\npreregistration_path = "prereg.md"\n'
                    f'preregistration_sha256 = "{hashlib.sha256(prereg_raw).hexdigest()}"\n'
                    'allowed_cards = ["G4", "A100"]\nallowed_host = "colab.google.com"\n'
                    '[job]\nrun = ["python3 worker.py"]\n',
                    encoding="utf-8",
                )
                with self.assertRaises(Refused) as ctx:
                    launch_window(
                        spec, root / "a100_out", card="A100", shape="standard", max_rate=12.0, cap=15.0,
                        dry=True, allow_sealed=True,
                    )
                self.assertEqual(ctx.exception.code, "SEALED_DESTINATION_REFUSED")
            finally:
                prereg.chmod(0o644)

    def test_fu_2_reclaim_guard_race_does_not_steal_first_contenders_live_guard(self):
        with tempfile.TemporaryDirectory() as tmp:
            reclaim_guard = Path(tmp) / "open.reclaim"
            reclaim_guard.mkdir()
            (reclaim_guard / "owner.json").write_text(
                json.dumps({"pid": 888888, "at": "2026-11-26T00:00:00Z"}), encoding="utf-8"
            )
            barrier = threading.Barrier(2)
            c1_acquired = threading.Event()
            outcomes: dict[str, bool] = {}

            def _contender_1():
                def _pid_alive_1(pid: int) -> bool:
                    if pid == 888888:
                        barrier.wait(timeout=5.0)
                    return pid == os.getpid()

                ok = _try_acquire_reclaim_guard(reclaim_guard, pid_alive=_pid_alive_1)
                outcomes["c1"] = ok
                c1_acquired.set()

            real_rename = os.rename
            c2_tid = [None]

            def _gated_rename(src, dst):
                if threading.get_ident() == c2_tid[0] and Path(src) == reclaim_guard:
                    c1_acquired.wait(timeout=5.0)
                return real_rename(src, dst)

            def _contender_2():
                c2_tid[0] = threading.get_ident()

                def _pid_alive_2(pid: int) -> bool:
                    if pid == 888888:
                        barrier.wait(timeout=5.0)
                    return pid == os.getpid()

                outcomes["c2"] = _try_acquire_reclaim_guard(reclaim_guard, pid_alive=_pid_alive_2)

            with mock.patch("colab_runner.launcher.os.rename", side_effect=_gated_rename):
                t1 = threading.Thread(target=_contender_1)
                t2 = threading.Thread(target=_contender_2)
                t1.start()
                t2.start()
                t1.join(timeout=5.0)
                t2.join(timeout=5.0)

            self.assertEqual(outcomes, {"c1": True, "c2": False})
            self.assertTrue((reclaim_guard / "owner.json").is_file())
            self.assertEqual(json.loads((reclaim_guard / "owner.json").read_text(encoding="utf-8"))["pid"], os.getpid())
            self.assertEqual([p.name for p in Path(tmp).glob("*.stale-*")], [])

            # Ownerless stale guard replaced concurrently -> contender fails cleanly without leaking stale dir.
            rg_ownerless = Path(tmp) / "ownerless.reclaim"
            rg_ownerless.mkdir()
            old_t = 1000.0
            os.utime(rg_ownerless, (old_t, old_t))
            with mock.patch("colab_runner.launcher.time.time", return_value=old_t + 3000.0):
                def _swap_during_rename(src, dst):
                    real_rename(src, dst)
                    rg_ownerless.mkdir()
                    (rg_ownerless / "owner.json").write_text(
                        json.dumps({"pid": os.getpid(), "at": "2026-11-26T00:00:00Z"}), encoding="utf-8"
                    )

                with mock.patch("colab_runner.launcher._read_owner_bytes", side_effect=[b"", b"changed"]):
                    with mock.patch("colab_runner.launcher.os.rename", side_effect=_swap_during_rename):
                        self.assertFalse(_try_acquire_reclaim_guard(rg_ownerless, pid_alive=lambda pid: pid == os.getpid()))
            self.assertTrue((rg_ownerless / "owner.json").is_file())

            # LOW 4a: ownerless stale guard replaced during rename by a *fresh* ownerless dir -> restored, not stolen.
            rg_fresh = Path(tmp) / "fresh_ownerless.reclaim"
            rg_fresh.mkdir()
            os.utime(rg_fresh, (old_t, old_t))
            now_t = old_t + 3000.0
            with mock.patch("colab_runner.launcher.time.time", return_value=now_t):
                def _swap_with_fresh_ownerless(src, dst):
                    real_rename(src, dst)
                    if Path(src) == rg_fresh:
                        rg_fresh.mkdir()
                        os.utime(rg_fresh, (now_t, now_t))
                        os.utime(dst, (now_t, now_t))

                with mock.patch("colab_runner.launcher.os.rename", side_effect=_swap_with_fresh_ownerless):
                    self.assertFalse(_try_acquire_reclaim_guard(rg_fresh, pid_alive=lambda pid: pid == os.getpid()))
            self.assertTrue(rg_fresh.is_dir())
            self.assertFalse((rg_fresh / "owner.json").exists())

    def test_fu_3_overrun_kill_triggers_cleanup_download_clamp_and_skip_under_cap(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            t4_seq = [usage_text(rate=0.0, n=0, balance=100.0), usage_text(rate=1.5, n=1, balance=99.9)]

            # Case 1: kill takes 120s (50s overrun vs 70s kill bound) -> last salvage download is clamped < 120s.
            c1 = FakeClock()
            n_poll_1 = {"n": 0}

            def _polls_1(n, env):
                n_poll_1["n"] += 1
                c1.advance(200.0 if n_poll_1["n"] <= 20 else 25.0)
                return rec(progress=n)

            sim1 = SimColab(
                clock=c1,
                after_open_rows=["[t4-clamp] ep | Hardware: T4 | Shape: Standard | Variant: GPU"],
                usage=lambda idx: t4_seq[min(idx - 1, len(t4_seq) - 1)],
                polls=_polls_1,
                delay_kill_s=500.0,
                stops=[1, 1, 0],
                hang_downloads=True,
            )
            out1 = launch_window(
                self.EXAMPLE_SPEC, root / "overrun_clamp", session="t4-clamp",
                card="T4", shape="standard", max_rate=1.5, cap=2.5, margin=0.0, wall_s=7200.0,
                colab=sim1, clock=c1.now, sleep=c1.sleep,
                state_dir=root / "st1", open_lock=root / "lock1",
            )
            self.assertEqual(out1["status"], "cap")
            self.assertTrue(out1["session_stopped_confirmed"])
            self.assertLessEqual(out1["spend_ccu"], 2.5)
            dl_timeouts = [c["timeout"] for c in sim1.calls if c["kind"] == "download"]
            self.assertTrue(any(t < 120.0 for t in dl_timeouts), dl_timeouts)

            # Case 2: huge kill overrun (900s) -> later download hits `cleanup download skipped` and still stays <= cap.
            c2 = FakeClock()
            n_poll_2 = {"n": 0}

            def _polls_2(n, env):
                n_poll_2["n"] += 1
                c2.advance(200.0 if n_poll_2["n"] <= 20 else 25.0)
                return rec(progress=n)

            sim2 = SimColab(
                clock=c2,
                after_open_rows=["[t4-skip] ep | Hardware: T4 | Shape: Standard | Variant: GPU"],
                usage=lambda idx: t4_seq[min(idx - 1, len(t4_seq) - 1)],
                polls=_polls_2,
                delay_kill_s=1300.0,
                stops=[1, 1, 0],
                hang_downloads=True,
            )
            logs: list[str] = []
            out2 = launch_window(
                self.EXAMPLE_SPEC, root / "overrun_skip", session="t4-skip",
                card="T4", shape="standard", max_rate=1.5, cap=2.5, margin=0.0, wall_s=7200.0,
                colab=sim2, clock=c2.now, sleep=c2.sleep, log=logs.append,
                state_dir=root / "st2", open_lock=root / "lock2",
            )
            self.assertEqual(out2["status"], "cap")
            self.assertTrue(out2["session_stopped_confirmed"])
            self.assertLessEqual(out2["spend_ccu"], 2.5)
            self.assertTrue(any("cleanup download skipped" in msg for msg in logs), logs)
            self.assertLess(len([c for c in sim2.calls if c["kind"] == "download"]), 7)

    def test_fix1_h1_cr_env_reserved_in_cli_and_job_env(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = _write_spec(root)
            for flag in ("CR_DRY_RUN=1", "CR_OUT_DIR=/tmp/x", "CR_SMOKE_STUB=1"):
                with self.subTest(flag=flag):
                    with self.assertRaises(Refused) as ctx:
                        launch_window(spec, root / ("cli_" + flag.split("=")[0]), extra_env=[flag], dry=True)
                    self.assertEqual(ctx.exception.code, "ENV_RESERVED")
                    out, err = io.StringIO(), io.StringIO()
                    with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                        rc = main(["launch", "--spec", str(spec), "--out-dir", str(root / ("m_" + flag.split("=")[0])),
                                   "--dry", "--env", flag])
                    self.assertEqual(rc, 1)
                    self.assertIn("REFUSED: ENV_RESERVED", err.getvalue())

            with self.assertRaises(Refused) as ctx_run:
                launch_window(spec, root / "cli_run_id", extra_env=["CR_RUN_ID=abc"], dry=True)
            self.assertEqual(ctx_run.exception.code, "RUN_ID_RESERVED")

            spec_job_env = root / "spec_cr_env.toml"
            spec_job_env.write_text(
                spec.read_text(encoding="utf-8").replace(
                    'cpu_dry_run = ["python3 worker.py"]',
                    'cpu_dry_run = ["python3 worker.py"]\nenv = ["CR_OUT_DIR=/tmp/override"]',
                ),
                encoding="utf-8",
            )
            with self.assertRaises(Refused) as ctx_job:
                launch_window(spec_job_env, root / "job_env_cr", dry=True)
            self.assertEqual(ctx_job.exception.code, "ENV_RESERVED")

    def test_fix1_h2_poll_mode_progress_glob_reports_progress_across_polls(self):
        if os.name != "posix":
            self.skipTest("LocalColab poll-mode execution requires POSIX bash/nohup")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            (root / "worker.py").write_text(
                "import json, os, time\nfrom pathlib import Path\n"
                "w = Path(os.environ['CR_WORK_DIR'])\n"
                "pdir = w / 'custom_prog'\nudir = w / 'custom_units'\n"
                "pdir.mkdir(exist_ok=True)\nudir.mkdir(exist_ok=True)\n"
                "time.sleep(0.25)\n"
                "(pdir / 'a.cnt').write_text('3\\n', encoding='utf-8')\n"
                "(udir / 'u1.done').write_text('ok\\n', encoding='utf-8')\n"
                "time.sleep(0.25)\n"
                "(pdir / 'b.cnt').write_text('7\\n', encoding='utf-8')\n"
                "(udir / 'u2.done').write_text('ok\\n', encoding='utf-8')\n"
                "time.sleep(0.25)\n"
                "(w / 'result.json').write_text(json.dumps({'ok': True}) + '\\n', encoding='utf-8')\n"
                "(w / 'out.txt').write_text('done\\n', encoding='utf-8')\n",
                encoding="utf-8",
            )
            spec = root / "colab-window.toml"
            spec.write_text(
                'schema_version = 1\n'
                '[project]\nrepo = "org/demo"\nstudy = "demo-study"\njob = "demo_job"\n'
                '[compute]\ncard = "CPU"\npoll_s = 0.15\nprogress_stall_s = 0.5\nwall_s = 30\ncap = 8.0\nresident = false\n'
                '[inputs]\nroot = "."\nfiles = ["worker.py"]\n'
                '[job]\n'
                'run = ["python3 worker.py"]\n'
                'progress_glob = "custom_prog/*.cnt"\n'
                'units_glob = "custom_units/*.done"\n'
                '[outputs]\ncollect = ["result.json", "out.txt"]\n',
                encoding="utf-8",
            )
            seen_envs: list[dict[str, str]] = []
            orig_exec = LocalColab.exec_cell

            def _spy_exec(lc_self, session, source, env=None, timeout=60.0):
                seen_envs.append(dict(env or {}))
                return orig_exec(lc_self, session, source, env=env, timeout=timeout)

            out_dir = root / "poll_h2_out"
            with mock.patch.object(LocalColab, "exec_cell", autospec=True, side_effect=_spy_exec):
                outcome = launch_window(spec, out_dir, local_cpu=True)

            self.assertEqual(outcome["status"], "done", outcome)
            poll_envs = [e for e in seen_envs if "CR_POLL_LOG_OFFSET" in e]
            self.assertGreaterEqual(len(poll_envs), 2)
            for pe in poll_envs:
                self.assertEqual(pe.get("CR_PROGRESS_GLOB"), "custom_prog/*.cnt")
                self.assertEqual(pe.get("CR_UNITS_GLOB"), "custom_units/*.done")
            rr = json.loads((out_dir / "runner_result.json").read_text(encoding="utf-8"))
            self.assertGreaterEqual(rr["last"]["progress"], 3)
            self.assertGreaterEqual(rr["last"]["units_done"], 1)

    def test_fix1_m1_directory_tar_collect_and_missing_outputs_partial(self):
        if os.name != "posix":
            self.skipTest("LocalColab tar-output execution requires POSIX bash/nohup")
        t = fake_tokens()
        leak = leak_lines(t)[2]
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # LocalColab.download and download_unscrubbed reject missing files & raw directories like live colab download
            lc = LocalColab(root / "lc_dir_check")
            (lc.content_dir / "raw_dir").mkdir()
            (lc.content_dir / "raw_dir" / "a.txt").write_text("hi\n", encoding="utf-8")
            self.assertNotEqual(lc.download("s", "/content/raw_dir", str(root / "dst_dir")).rc, 0)
            self.assertNotEqual(lc.download_unscrubbed("s", "/content/raw_dir", str(root / "dst_dir")).rc, 0)
            self.assertNotEqual(lc.download_unscrubbed("s", "/content/no_such.tar", str(root / "dst_miss")).rc, 0)

            (root / ".git").mkdir()
            (root / "worker.py").write_text(
                "from pathlib import Path\nimport os\n"
                "w = Path(os.environ['CR_WORK_DIR'])\n"
                "d = w / 'dir_out' / 'nested'\n"
                "d.mkdir(parents=True, exist_ok=True)\n"
                f"(d / 'a.txt').write_text('url https://colab.example/x?colab-runtime-proxy-token=' + {t['proxy']!r} + ' ' + {leak!r} + '\\n', encoding='utf-8')\n"
                "(w / 'dir_out' / 'crlf.txt').write_bytes(b'a\\r\\nb\\r\\n')\n"
                "(w / 'result.json').write_text('{\"ok\": true}\\n', encoding='utf-8')\n",
                encoding="utf-8",
            )
            spec = root / "spec_dir.toml"
            spec.write_text(
                'schema_version = 1\n'
                '[project]\nrepo = "org/demo"\nstudy = "demo-study"\njob = "demo_job"\n'
                '[compute]\ncard = "CPU"\npoll_s = 0.15\nwall_s = 30\ncap = 8.0\n'
                '[inputs]\nroot = "."\nfiles = ["worker.py"]\n'
                '[job]\nrun = ["python3 worker.py"]\n'
                '[outputs]\ncollect = ["result.json", "dir_out"]\n',
                encoding="utf-8",
            )
            out_dir = root / "m1_ok"
            res = launch_window(spec, out_dir, local_cpu=True)
            self.assertEqual(res["status"], "done", res)
            self.assertEqual(res["exit_code"], 0)
            self.assertEqual(res["collected_outputs"], ["result.json", "dir_out"])
            self.assertEqual(res["missing_outputs"], [])
            self.assertEqual(res["output_errors"], {})
            self.assertTrue((out_dir / "_local_vm_content" / "demo_job_out" / "dir_out.tar.part0000").is_file())
            txt = (out_dir / "collected" / "dir_out" / "nested" / "a.txt").read_text(encoding="utf-8")
            self.assertIn("colab-runtime-proxy-token=[REDACTED]", txt)
            self.assertIn("[REDACTED]", txt)
            for v in values(t):
                self.assertNotIn(v, txt)
            self.assertEqual((out_dir / "collected" / "dir_out" / "crlf.txt").read_bytes(), b"a\r\nb\r\n")

            # Binary token inside a directory output quarantines that file and sets runner._download_refusal,
            # while continuing to collect later declared outputs -> raises BinaryDownloadRefused and records
            # outcome status="error", exit_code=2, refusal.code="BINARY_DOWNLOAD_TOKEN", and output_errors.
            (root / "bad_dir_worker.py").write_text(
                "from pathlib import Path\nimport os\n"
                "w = Path(os.environ['CR_WORK_DIR'])\n"
                "d = w / 'bad_dir'\n"
                "d.mkdir(parents=True, exist_ok=True)\n"
                f"(d / 'leak.png').write_bytes(b'\\x89PNG\\r\\n\\x1a\\n\\x00colab-runtime-proxy-token=' + {t['proxy'].encode('ascii')!r})\n"
                "(w / 'result.json').write_text('{\"ok\": true}\\n', encoding='utf-8')\n",
                encoding="utf-8",
            )
            spec_bin_dir = root / "spec_bin_dir.toml"
            spec_bin_dir.write_text(
                'schema_version = 1\n'
                '[project]\nrepo = "org/demo"\nstudy = "demo-study"\njob = "demo_job"\n'
                '[compute]\ncard = "CPU"\npoll_s = 0.15\nwall_s = 30\ncap = 8.0\n'
                '[inputs]\nroot = "."\nfiles = ["bad_dir_worker.py"]\n'
                '[job]\nrun = ["python3 bad_dir_worker.py"]\n'
                '[outputs]\ncollect = ["bad_dir", "result.json"]\n',
                encoding="utf-8",
            )
            out_bin_dir = root / "m1_bin_dir"
            with self.assertRaises(BinaryDownloadRefused) as ctx_bin_dir:
                launch_window(spec_bin_dir, out_bin_dir, local_cpu=True)
            self.assertEqual(ctx_bin_dir.exception.code, "BINARY_DOWNLOAD_TOKEN")
            res_b = json.loads((out_bin_dir / "outcome.json").read_text(encoding="utf-8"))
            self.assertEqual(res_b["status"], "error", res_b)
            self.assertEqual(res_b["exit_code"], 2)
            self.assertIsNotNone(res_b["refusal"])
            self.assertEqual(res_b["refusal"]["code"], "BINARY_DOWNLOAD_TOKEN")
            self.assertEqual(res_b["collected_outputs"], ["result.json"])
            self.assertEqual(res_b["missing_outputs"], ["bad_dir"])
            self.assertEqual(res_b["output_errors"], {"bad_dir": "BINARY_DOWNLOAD_TOKEN"})
            self.assertEqual(len(res_b["quarantined"]), 1)
            self.assertTrue(
                res_b["quarantined"][0]["path"].replace("\\", "/").endswith("/collected/bad_dir/leak.png"),
                res_b["quarantined"],
            )
            self.assertTrue((out_bin_dir / "collected" / "result.json").is_file())
            self.assertFalse((out_bin_dir / "collected" / "bad_dir").exists())

            # Non-refusal OSError during directory scrub records OUTPUT_SCRUB_FAILED and still collects next output.
            (root / "two_dirs_worker.py").write_text(
                "from pathlib import Path\nimport os\n"
                "w = Path(os.environ['CR_WORK_DIR'])\n"
                "for name in ('dir_a', 'dir_b'):\n"
                "    d = w / name\n"
                "    d.mkdir(parents=True, exist_ok=True)\n"
                "    (d / 'item.txt').write_text(name + '\\n', encoding='utf-8')\n",
                encoding="utf-8",
            )
            spec_scrub_err = root / "spec_scrub_err.toml"
            spec_scrub_err.write_text(
                'schema_version = 1\n'
                '[project]\nrepo = "org/demo"\nstudy = "demo-study"\njob = "demo_job"\n'
                '[compute]\ncard = "CPU"\npoll_s = 0.15\nwall_s = 30\ncap = 8.0\n'
                '[inputs]\nroot = "."\nfiles = ["two_dirs_worker.py"]\n'
                '[job]\nrun = ["python3 two_dirs_worker.py"]\n'
                '[outputs]\ncollect = ["dir_a", "dir_b"]\n',
                encoding="utf-8",
            )
            out_scrub_err = root / "m1_scrub_err"
            import colab_runner.launcher as launcher_mod
            real_scrub_local = launcher_mod.scrub_local

            def _fail_first_dir_scrub(target, *args, **kwargs):
                if Path(target).name == "dir_a" and ".stage-" in str(target):
                    raise PermissionError("simulated permission denied")
                return real_scrub_local(target, *args, **kwargs)

            with mock.patch.object(launcher_mod, "scrub_local", side_effect=_fail_first_dir_scrub):
                res_scrub = launch_window(spec_scrub_err, out_scrub_err, local_cpu=True)
            self.assertEqual(res_scrub["status"], "partial", res_scrub)
            self.assertEqual(res_scrub["exit_code"], 2)
            self.assertIsNone(res_scrub["refusal"])
            self.assertEqual(res_scrub["collected_outputs"], ["dir_b"])
            self.assertEqual(res_scrub["missing_outputs"], ["dir_a"])
            self.assertEqual(res_scrub["output_errors"], {"dir_a": "OUTPUT_SCRUB_FAILED"})
            self.assertTrue((out_scrub_err / "collected" / "dir_b" / "item.txt").is_file())
            self.assertFalse((out_scrub_err / "collected" / "dir_a").exists())

            spec_missing = root / "spec_missing.toml"
            spec_missing.write_text(
                spec.read_text(encoding="utf-8").replace(
                    'collect = ["result.json", "dir_out"]',
                    'collect = ["result.json", "does_not_exist.json"]',
                ),
                encoding="utf-8",
            )
            out_partial = root / "m1_partial"
            res_p = launch_window(spec_missing, out_partial, local_cpu=True)
            self.assertEqual(res_p["status"], "partial", res_p)
            self.assertEqual(res_p["exit_code"], 2)
            self.assertEqual(res_p["collected_outputs"], ["result.json"])
            self.assertEqual(res_p["missing_outputs"], ["does_not_exist.json"])
            self.assertFalse((out_partial / "collect_confirmed.json").exists())

    def test_fix1_edge_coverage_reclaim_tar_and_vm_glob(self):
        import tarfile
        from colab_runner.launcher import (
            _check_lock_dir_stale,
            _claim_reclaim_guard,
            _download_declared_outputs,
            _extract_dir_tar,
            _read_owner_bytes,
        )
        from colab_runner.window_lib import launch_guards as lg

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)

            # vm/lib.py relative cr_glob_files + empty pattern + cr_progress + cr_units
            vm_root = root / "vm"
            work_dir = vm_root / "j_work"
            out_dir = vm_root / "j_out"
            (work_dir / "rel").mkdir(parents=True)
            (out_dir / "rel").mkdir(parents=True)
            (work_dir / "rel" / "a.cnt").write_text("5\n", encoding="utf-8")
            (out_dir / "rel" / "b.done").write_text("1\n", encoding="utf-8")
            lib_path = Path(__file__).resolve().parents[1] / "colab_runner" / "vm" / "lib.py"
            with mock.patch.dict(os.environ, {"CR_CONTENT": str(vm_root), "CR_JOB": "j", "CR_OUT_DIR": str(out_dir)}):
                ns: dict = {"scrub_colab_tokens": lg.scrub_colab_tokens}
                exec(compile(lib_path.read_text(encoding="utf-8"), str(lib_path), "exec"), ns)
                self.assertEqual(ns["cr_glob_files"](""), [])
                self.assertGreaterEqual(len(ns["cr_glob_files"]("rel/*")), 2)
                with mock.patch.dict(os.environ, {"CR_PROGRESS_GLOB": "rel/*.cnt", "CR_UNITS_GLOB": "rel/*.done"}):
                    self.assertEqual(ns["cr_progress"](), 5)
                    self.assertEqual(ns["cr_units"](), 1)

            # _read_owner_bytes / _check_lock_dir_stale / _claim_reclaim_guard / _try_acquire_reclaim_guard OSError edges
            dir_owner = root / "dir_owner"
            (dir_owner / "owner.json").mkdir(parents=True)
            self.assertIsNone(_read_owner_bytes(dir_owner))

            stat_fail = root / "stat_fail.reclaim"
            stat_fail.mkdir()
            orig_stat = Path.stat
            stat_calls = {"n": 0}

            def _flaky_stat(self_p, *a, **kw):
                if Path(self_p).name == "stat_fail.reclaim":
                    stat_calls["n"] += 1
                    if stat_calls["n"] >= 2:
                        raise OSError("stat boom")
                return orig_stat(self_p, *a, **kw)

            with mock.patch.object(Path, "stat", autospec=True, side_effect=_flaky_stat):
                self.assertEqual(_check_lock_dir_stale(stat_fail, pid_alive=lambda pid: False), (False, None))

            race_guard = root / "race.reclaim"
            with mock.patch("colab_runner.launcher.os.rename", side_effect=OSError("rename fail")):
                self.assertFalse(_claim_reclaim_guard(race_guard))
            self.assertEqual(list(root.glob("race.reclaim.prep-*")), [])

            orig_exists = Path.exists

            def _appear_before_second_exists(self_p):
                if Path(self_p) == race_guard and list(root.glob("race.reclaim.prep-*")):
                    self_p.mkdir(exist_ok=True)
                    return True
                return orig_exists(self_p)

            with mock.patch.object(Path, "exists", autospec=True, side_effect=_appear_before_second_exists):
                self.assertFalse(_claim_reclaim_guard(race_guard))
            self.assertEqual(list(root.glob("race.reclaim.prep-*")), [])

            stale_only = root / "stale_oserr.reclaim"
            stale_only.mkdir()
            os.utime(stale_only, (100.0, 100.0))
            with mock.patch("colab_runner.launcher.time.time", return_value=3000.0):
                with mock.patch("colab_runner.launcher.os.rename", side_effect=OSError("stale rename fail")):
                    self.assertFalse(_try_acquire_reclaim_guard(stale_only, pid_alive=lambda pid: False))

                def _stat_raise_on_moved(self_p, *a, **kw):
                    if ".stale-" in Path(self_p).name:
                        raise OSError("moved stat fail")
                    return orig_stat(self_p, *a, **kw)

                real_rename = os.rename

                def _fail_restore_rename(src, dst):
                    if ".stale-" in Path(src).name:
                        raise OSError("restore rename fail")
                    return real_rename(src, dst)

                with mock.patch.object(Path, "stat", autospec=True, side_effect=_stat_raise_on_moved):
                    with mock.patch("colab_runner.launcher.os.rename", side_effect=_fail_restore_rename):
                        self.assertFalse(_try_acquire_reclaim_guard(stale_only, pid_alive=lambda pid: False))

            # _extract_dir_tar missing/corrupt/unsafe/quarantine errors, and _download_declared_outputs None timeouts
            runner = Runner(FakeColab(), "s", root / "r_out", job="j")
            stage_root = root / "stage"
            stage_root.mkdir()
            self.assertEqual(
                _extract_dir_tar(root / "missing.tar", stage_root, "d", root / "dst", runner),
                "OUTPUT_TAR_CORRUPT",
            )

            corrupt_tar = root / "corrupt.tar"
            corrupt_tar.write_bytes(b"not a tar archive")
            self.assertEqual(
                _extract_dir_tar(corrupt_tar, stage_root, "corrupt_d", root / "dst_corrupt", runner),
                "OUTPUT_TAR_CORRUPT",
            )

            unsafe_tar = root / "unsafe.tar"
            with tarfile.open(unsafe_tar, "w:") as tf:
                ti = tarfile.TarInfo("../escape.txt")
                ti.size = 2
                tf.addfile(ti, io.BytesIO(b"hi"))
            self.assertEqual(
                _extract_dir_tar(unsafe_tar, stage_root, "unsafe_d", root / "dst_unsafe", runner),
                "OUTPUT_TAR_UNSAFE",
            )

            t = fake_tokens()
            poison_tar = root / "poison.tar"
            payload = b"\x89PNG\r\n\x1a\n\x00colab-runtime-proxy-token=" + t["proxy"].encode("ascii")
            with tarfile.open(poison_tar, "w:") as tf:
                ti = tarfile.TarInfo("bad.bin")
                ti.size = len(payload)
                tf.addfile(ti, io.BytesIO(payload))
            self.assertEqual(
                _extract_dir_tar(
                    poison_tar, stage_root, "poison_d", root / "poison_dst", runner, bucket="salvage",
                ),
                "BINARY_DOWNLOAD_TOKEN",
            )
            self.assertIsInstance(runner._download_refusal, BinaryDownloadRefused)
            self.assertTrue(
                any(
                    q.get("path", "").replace("\\", "/").endswith("/salvage/poison_d/bad.bin")
                    for q in runner._res.quarantined
                ),
                runner._res.quarantined,
            )
            self.assertTrue((root / "r_out.quarantine").exists())

            from colab_runner.colab_cli import QuarantineEscape
            import colab_runner.launcher as launcher_mod
            esc_tar = root / "esc.tar"
            with tarfile.open(esc_tar, "w:") as tf:
                ti = tarfile.TarInfo("x.txt")
                ti.size = 1
                tf.addfile(ti, io.BytesIO(b"x"))
            runner._download_refusal = None
            with mock.patch.object(
                launcher_mod,
                "scrub_local",
                side_effect=QuarantineEscape(root / "src", root / "dest", root / "q"),
            ):
                self.assertEqual(
                    _extract_dir_tar(esc_tar, stage_root, "esc_d", root / "esc_dst", runner),
                    "QUARANTINE_ESCAPE",
                )
            self.assertIsInstance(runner._download_refusal, QuarantineEscape)
            runner._download_refusal = None

            # Existing destination directory is cleanly replaced and QuarantineEscape returns its code
            clean_tar = root / "clean.tar"
            with tarfile.open(clean_tar, "w:") as tf:
                ti = tarfile.TarInfo("ok.txt")
                ti.size = 2
                tf.addfile(ti, io.BytesIO(b"ok"))
            existing_dst = root / "existing_dst"
            existing_dst.mkdir()
            (existing_dst / "old.txt").write_text("stale", encoding="utf-8")
            self.assertIsNone(_extract_dir_tar(clean_tar, stage_root, "clean_d", existing_dst, runner))
            self.assertTrue((existing_dst / "ok.txt").is_file())
            self.assertFalse((existing_dst / "old.txt").exists())

            sim_miss = SimColab(clock=FakeClock(), remote_files={"/content/j_out/d.tar": b""})
            runner_sim = Runner(sim_miss, "s", root / "r_sim", job="j")
            got1, ok1 = _download_declared_outputs(
                runner_sim, ["d"], root / "dl1", content="/content", job="j",
                next_timeout=lambda: 10.0, next_exec_timeout=lambda: None,
            )
            self.assertEqual((got1, ok1), ([], False))

            touts = iter([10.0, 10.0, None])
            got2, ok2 = _download_declared_outputs(
                runner_sim, ["d"], root / "dl2", content="/content", job="j",
                next_timeout=lambda: next(touts, None), next_exec_timeout=lambda: 10.0,
            )
            self.assertEqual((got2, ok2), ([], False))


class ChunkedDownloadTest(unittest.TestCase):
    def test_local_cpu_chunked_dir_direct_file_part_glob_and_scaled_bounds(self):
        if os.name != "posix":
            self.skipTest("local-cpu VM cell execution requires POSIX bash/nohup")
        t = fake_tokens()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            png_bytes = b"\x89PNG\r\n\x1a\n\x00" + (bytes(range(256)) * 10)
            nested_bytes = b"NEST\x00" + (b"xyz987654321" * 110)
            (root / "make_outputs.py").write_text(
                "import os\nfrom pathlib import Path\n"
                "w, o = Path(os.environ['CR_WORK_DIR']), Path(os.environ['CR_OUT_DIR'])\n"
                "(w / 'bundle_dir' / 'nested').mkdir(parents=True, exist_ok=True)\n"
                f"(w / 'bundle_dir' / 'clean.png').write_bytes({png_bytes!r})\n"
                f"(w / 'bundle_dir' / 'nested' / 'weights.bin').write_bytes({nested_bytes!r})\n"
                f"(w / 'bundle_dir' / 'note.txt').write_text('tok=' + {t['hf']!r} + '\\n', encoding='utf-8')\n"
                "(w / 'small.txt').write_text('hello small\\n', encoding='utf-8')\n"
                "(o / 'bundle_dir.tar.part_keep').write_text('job-file', encoding='utf-8')\n",
                encoding="utf-8",
            )
            spec = root / "colab-window.toml"
            spec_txt = (
                'schema_version = 1\n[project]\nrepo = "o/c"\nstudy = "chunk-s"\njob = "chunk_j"\n'
                '[compute]\ncard = "T4"\nshape = "standard"\nmax_rate = 1.6\ncap = 6.0\nwall_s = 900\npoll_s = 0.15\n'
                '[inputs]\nroot = "."\nfiles = ["make_outputs.py"]\n[job]\nrun = ["python3 make_outputs.py"]\n'
                '[outputs]\ncollect = ["bundle_dir", "small.txt"]\ndownload_parts = 12\n'
            )
            spec.write_text(spec_txt, encoding="utf-8")

            dry_plan = launch_window(spec, root / "dry", dry=True)
            self.assertEqual(dry_plan["outputs"]["download_parts"], 12)
            spec_p1 = root / "p1.toml"
            spec_p1.write_text(spec_txt.replace("download_parts = 12", "download_parts = 1"), encoding="utf-8")
            dry_p1 = launch_window(spec_p1, root / "dry1", dry=True)
            self.assertGreaterEqual(dry_plan["budget"]["cleanup_outputs_bound_s"] - dry_p1["budget"]["cleanup_outputs_bound_s"], 1000.0)
            import colab_runner.launcher as launcher_mod
            with mock.patch.object(launcher_mod, "DEFAULT_DOWNLOAD_CHUNK_BYTES", 1024):
                out = launch_window(spec, root / "live_cpu", local_cpu=True)
            self.assertEqual((out["status"], out["exit_code"], sorted(out["collected_outputs"])), ("done", 0, ["bundle_dir", "small.txt"]), out)
            collected = root / "live_cpu" / "collected"
            self.assertEqual((collected / "bundle_dir" / "clean.png").read_bytes(), png_bytes)
            self.assertEqual((collected / "bundle_dir" / "nested" / "weights.bin").read_bytes(), nested_bytes)
            self.assertEqual((collected / "bundle_dir" / "note.txt").read_text(encoding="utf-8"), "tok=hf_[REDACTED]\n")
            self.assertEqual((collected / "small.txt").read_text(encoding="utf-8"), "hello small\n")
            vm_out = root / "live_cpu" / "_local_vm_content" / "chunk_j_out"
            self.assertTrue((vm_out / "bundle_dir.tar.part_keep").is_file())
            self.assertGreaterEqual(len(list(vm_out.glob("bundle_dir.tar.part[0-9][0-9][0-9][0-9]"))), 2)

    def test_review_b1_to_b6_failures_isolation_and_sibling_staging(self):
        import shutil
        import tarfile

        self.assertIsNone(_parse_tar_parts("CR_TAR_READY\n"))
        self.assertIsNone(_parse_tar_parts("CR_TAR_PART bad_line\n"))
        self.assertIsNone(_parse_tar_parts("CR_TAR_PART /x.tar.part0000 nope " + "a" * 64 + "\n"))
        self.assertIsNone(_parse_tar_parts("CR_TAR_PART /x.tar.part0000 4 nothex\n"))

        class PartColab(SimColab):
            def __init__(self, cell_reply: CliResult, *, partial_direct_fail: bool = False, **kw):
                super().__init__(**kw)
                self.cell_reply, self.partial_direct_fail = cell_reply, partial_direct_fail

            def exec_cell(self, session, source, env=None, timeout=60.0) -> CliResult:
                if "CR_TAR_REL" in (env or {}):
                    self._record("tar", session, timeout)
                    return self.cell_reply
                return super().exec_cell(session, source, env=env, timeout=timeout)

            def download(self, session, remote, local, timeout=120.0) -> CliResult:
                if self.partial_direct_fail and remote.endswith("/truncated.txt"):
                    Path(local).parent.mkdir(parents=True, exist_ok=True)
                    Path(local).write_bytes(b"half-written")
                    return CliResult(1, "", "connection dropped")
                return super().download(session, remote, local, timeout)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            p0, p1 = b"aa", b"bb"
            s0, s1 = hashlib.sha256(p0).hexdigest(), hashlib.sha256(p1).hexdigest()
            col_over = PartColab(CliResult(
                0, "CR_TAR_READY\nCR_TAR_PART /content/j_out/d.tar.part0000 2 %s\nCR_TAR_PART /content/j_out/d.tar.part0001 2 %s\n" % (s0, s1),
            ))
            errs_over: dict[str, str] = {}
            self.assertEqual(_download_declared_outputs(
                Runner(col_over, "s", root / "o_over", job="j"), ["d"], root / "o_over" / "collected",
                content="/content", job="j", output_errors=errs_over, download_parts=1,
            ), ([], True))
            self.assertEqual(errs_over, {"d": "OUTPUT_PARTS_EXCEEDED"})
            self.assertEqual([e for e in col_over.events[col_over.events.index("tar") + 1:] if e == "download"], [])
            for name, reply, rfiles, want_err in (
                ("bad_hdr", CliResult(0, "CR_TAR_READY\nCR_TAR_PART bad\n"), {}, "OUTPUT_TAR_CORRUPT"),
                ("bad_sha", CliResult(0, "CR_TAR_READY\nCR_TAR_PART /content/j_out/d.tar.part0000 2 %s\n" % ("f" * 64)),
                 {"/content/j_out/d.tar.part0000": p0}, "OUTPUT_TAR_CORRUPT"),
                ("miss_part", CliResult(
                    0, "CR_TAR_READY\nCR_TAR_PART /content/j_out/d.tar.part0000 2 %s\nCR_TAR_PART /content/j_out/d.tar.part0001 2 %s\n" % (s0, s1)),
                 {"/content/j_out/d.tar.part0000": p0}, "OUTPUT_DOWNLOAD_FAILED"),
            ):
                errs: dict[str, str] = {}
                got, ok = _download_declared_outputs(
                    Runner(PartColab(reply, remote_files=rfiles), "s", root / name, job="j"),
                    ["d"], root / name / "collected", content="/content", job="j", output_errors=errs, download_parts=2,
                )
                self.assertEqual((got, ok, errs), ([], True, {"d": want_err}), name)
                self.assertFalse((root / (name + ".quarantine")).exists())
            touts = iter([10.0, 10.0, None])
            self.assertEqual(_download_declared_outputs(
                Runner(PartColab(CliResult(0, "CR_TAR_READY\nCR_TAR_PART /content/j_out/d.tar.part0000 2 %s\n" % s0),
                                 remote_files={"/content/j_out/d.tar.part0000": p0}), "s", root / "o_bud", job="j"),
                ["d"], root / "o_bud" / "collected", content="/content", job="j",
                next_timeout=lambda: next(touts, None), next_exec_timeout=lambda: 10.0,
            ), ([], False))
            dest_p = root / "o_part" / "collected"
            _download_declared_outputs(Runner(PartColab(CliResult(0, "CR_TAR_MISSING\n"), partial_direct_fail=True),
                                              "s", root / "o_part", job="j"), ["truncated.txt"], dest_p, content="/content", job="j")
            self.assertFalse((dest_p / "truncated.txt").exists())
            good_tar = root / "good.tar"
            with tarfile.open(good_tar, "w:") as tf:
                ti = tarfile.TarInfo("ok.txt"); ti.size = 2
                tf.addfile(ti, io.BytesIO(b"ok"))
            raw_tar = good_tar.read_bytes()
            tar_reply = CliResult(0, "CR_TAR_READY\nCR_TAR_PART /content/j_out/dir1.tar.part0000 %d %s\n" % (
                len(raw_tar), hashlib.sha256(raw_tar).hexdigest()))
            stage_paths_seen, real_move = [], shutil.move
            def _spy_move(src, dst, *a, **kw):
                stage_paths_seen.append(Path(src))
                if Path(src).name == "dir_bad":
                    raise OSError("disk full")
                return real_move(src, dst, *a, **kw)
            iso_list, errs_iso = [], {}
            with mock.patch("colab_runner.launcher.shutil.move", side_effect=_spy_move):
                _download_declared_outputs(
                    Runner(PartColab(tar_reply, remote_files={
                        "/content/j_work/first.txt": b"one", "/content/j_out/dir1.tar.part0000": raw_tar,
                        "/content/j_out/dir_bad.tar.part0000": raw_tar, "/content/j_work/last.txt": b"four",
                    }), "s", root / "o_iso", job="j"),
                    ["first.txt", "dir1", "dir_bad", "last.txt"], root / "o_iso" / "salvage",
                    content="/content", job="j", output_errors=errs_iso, transferred_out=iso_list,
                )
            self.assertEqual((iso_list, errs_iso), (["first.txt", "dir1", "last.txt"], {"dir_bad": "OUTPUT_DOWNLOAD_FAILED"}))
            self.assertTrue(any(p.is_relative_to(root / "o_iso") and ".salvage.stage-" in str(p) for p in stage_paths_seen))
            self.assertFalse(list((root / "o_iso").glob(".salvage.stage-*")))

    def test_migration_proofs_cap_pricing_dead_job_emitter_and_guard_failed(self):
        from colab_runner.errors import GuardFailed
        from colab_runner.spend import SpendGuard

        with self.assertRaises(Refused):
            _guard_call(lambda: (_ for _ in ()).throw(Refused("CAP_INVALID", "x")))
        g = SpendGuard(max_rate=1.6, cap=2.0, margin=0.02, cleanup_bound_s=466.0, pre_open_rate=0.0, pre_open_assignments=0)
        g.fold_usage(usage_text(rate=1.07, n=1, balance=99.0), rc=0, epoch=100.0, at="2026-11-26T00:00:00Z")
        self.assertEqual(g.priced_rate(), 1.6)
        for t in (110.0, 120.0):
            g.fold_usage(usage_text(rate=2.1, n=1, balance=98.8), rc=0, epoch=t, at="2026-11-26T00:00:10Z")
        self.assertEqual(g.priced_rate(), 2.1)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name, rec_kw, run_kw, want_reason in (
                ("dead", dict(alive=False, rc=None), dict(dead_confirm=2), "dead"),
                ("stall", dict(alive=True, rc=None, progress=1), dict(progress_stall_s=20.0), "progress_stall"),
            ):
                clk = FakeClock()
                res = Runner(FakeColab(
                    polls=lambda n, env, kw=rec_kw: (rec(phase="run", log_offset=n * 4, **kw), ["emit %d" % n]),
                    clock=clk,
                ), "s-" + name, root / ("r_" + name), poll_s=1.0, clock=clk.now, sleep=clk.sleep).run(
                    script="run.sh", wall_s=900.0, guard=g, **run_kw,
                )
                self.assertEqual(res.reason, want_reason)
                self.assertLess(res.elapsed_s, 100.0)

            # A usage read that cannot be parsed is logged and never stops the window.
            clk_r, crash_g, logged = FakeClock(), SpendGuard(max_rate=0.3, cap=2.0, cleanup_bound_s=1000.0), []
            with mock.patch.object(crash_g, "fold_usage", side_effect=RuntimeError("loop fold boom")):
                res_u = Runner(FakeColab(clock=clk_r), "s-gf", root / "r_gf", clock=clk_r.now, sleep=clk_r.sleep,
                               log=logged.append).run(script="run.sh", wall_s=900.0, guard=crash_g)
            self.assertEqual(res_u.reason, "wall")  # it kept running to its own wall, not GUARD_FAILED
            self.assertTrue(any("usage read not parsed (not a stop)" in m for m in logged))

            (root / ".git").mkdir()
            (root / "ok.sh").write_text("echo ok\n", encoding="utf-8")
            spec = root / "colab-window.toml"
            spec.write_text(
                'schema_version = 1\n[project]\nrepo = "o/c"\nstudy = "g"\njob = "g"\n'
                '[compute]\ncard = "CPU"\nmax_rate = 0.30\ncap = 2.0\nwall_s = 900\n'
                '[inputs]\nroot = "."\nfiles = ["ok.sh"]\n[job]\nrun = ["bash ok.sh"]\n',
                encoding="utf-8",
            )
            clk_g = FakeClock()
            with mock.patch("colab_runner.spend.SpendGuard.seconds_left", side_effect=RuntimeError("guard boom")):
                with self.assertRaises(GuardFailed):
                    launch_window(spec, root / "g_run", state_dir=root / "st", colab=SimColab(clock=clk_g),
                                  clock=clk_g.now, sleep=clk_g.sleep)
            out_g = json.loads((root / "g_run" / "outcome.json").read_text(encoding="utf-8"))
            self.assertEqual((out_g["status"], out_g["refusal"]["code"], out_g["cap_trip"]), ("error", "GUARD_FAILED", None))
            self.assertTrue(out_g["session_stopped_confirmed"])

            spec_cap = root / "cap.toml"
            spec_cap.write_text(spec.read_text(encoding="utf-8").replace("max_rate = 0.30\ncap = 2.0\nwall_s = 900", "max_rate = 1.8\ncap = 0.35\nmargin = 0.0\nwall_s = 3600"), encoding="utf-8")
            cap_s = launch_window(spec_cap, root / "c_dry", dry=True)["budget"]["runtime_at_priced_rate_s"]
            for tag, delay in (("sub", cap_s - 0.5), ("over", cap_s + 2.0)):
                clk_c = FakeClock()
                out_c = launch_window(spec_cap, root / ("c_" + tag), state_dir=root / "st", colab=SimColab(delay_new_s=delay, clock=clk_c), clock=clk_c.now, sleep=clk_c.sleep)
                self.assertEqual((out_c["status"], out_c["cap_trip"]["code"]), ("cap", "CAP_GUARD_TRIPPED"))


if __name__ == "__main__":
    unittest.main()

"""Tests for Continuous Card v4, PR #21 follow-ups (a)/(b), Addendum 3 (three-stop rule), and Addendum 4 (Windows)."""

from __future__ import annotations

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import colab_runner.launcher as launcher_mod
from colab_runner.__main__ import main
from colab_runner.colab_cli import BinaryDownloadRefused, CliResult
from colab_runner.errors import Refused
from colab_runner.launcher import launch_window
from colab_runner.queue import QueueSpec, classify_window_outcome, run_queue
from colab_runner.window_lib import launch_guards as lg
from tests.fakes import FakeClock, rec, usage_text
from tests.test_launcher import SimColab, _write_spec
from tests.test_tokens import fake_tokens


def _write_queue_spec(root: Path, jobs_toml: str) -> Path:
    qp = root / "queue.toml"
    qp.write_text('schema_version = 1\n' + jobs_toml, encoding="utf-8")
    return qp


class ContinuousWorkEngineTest(unittest.TestCase):
    def test_addendum4_refuses_native_windows_launch(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = _write_spec(root, card="CPU", shape=None, max_rate=0.26, cap=2.0)
            out_dir = root / "win_out"
            with mock.patch.object(launcher_mod.sys, "platform", "win32"):
                with self.assertRaises(Refused) as ctx:
                    launch_window(spec, out_dir, local_cpu=True)
                self.assertEqual(ctx.exception.code, "PLATFORM_UNSUPPORTED")
                self.assertIn("Linux or WSL", ctx.exception.message)

                out, err = io.StringIO(), io.StringIO()
                with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                    rc = main(["launch", "--spec", str(spec), "--out-dir", str(out_dir), "--local-cpu"])
                self.assertEqual(rc, 1)
                self.assertIn("REFUSED: PLATFORM_UNSUPPORTED: colab-runner runs on Linux or WSL", err.getvalue())
            self.assertFalse(out_dir.exists())

    def test_addendum3_three_stops_only_and_midrun_notes_logged(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = _write_spec(root, card="G4", shape="standard", cap=40.0, max_rate=10.5)

            # 1) Bad usage reads + missing assignments + mid-run rate rise above max_rate -> logged, completes done
            bad_no_assign = "Current balance: 90.00 compute units\nUsage rate: 10.00 / hr\n"
            usage_seq = [
                usage_text(rate=0.0, n=0, balance=100.0),      # pre-open
                usage_text(rate=10.2, n=1, balance=100.0),     # open-time check <= 10.5
                usage_text(rate=10.2, n=1, balance=99.9),      # open-time settled
                CliResult(1, "", "RuntimeError: transient"),   # mid-run failed read attempt 1
                CliResult(1, "", "RuntimeError: transient"),   # mid-run failed read attempt 2
                CliResult(1, "", "RuntimeError: transient"),   # mid-run failed read attempt 3 -> fold_usage logs
                usage_text(rate=14.0, n=1, balance=99.8),      # mid-run rise step 1
                usage_text(rate=14.0, n=1, balance=99.7),      # mid-run rise step 2 (above_max logged, not killed)
                bad_no_assign, bad_no_assign, bad_no_assign,   # assignments unreadable logged, not killed
            ]
            clk = FakeClock()

            def next_usage(idx):
                return usage_seq[min(idx - 1, len(usage_seq) - 1)]

            poll_counter = {"n": 0}

            def polls(n, env):
                poll_counter["n"] += 1
                clk.advance(70.0)
                return rec(rc=0 if poll_counter["n"] >= 7 else None, progress=poll_counter["n"])

            logs = []
            sim = SimColab(
                clock=clk,
                usage=next_usage,
                polls=polls,
                remote_files={
                    "/content/demo_job_work/result.json": json.dumps({"ok": True}).encode("utf-8"),
                    "/content/demo_job_work/out.txt": b"ok\n",
                    "/content/demo_job_out/vm_card.json": json.dumps({"ok": True}).encode("utf-8"),
                },
            )
            res = launch_window(
                spec, root / "mid_notes", colab=sim, clock=clk.now, sleep=clk.sleep,
                state_dir=root / "state", log=logs.append,
            )
            self.assertEqual(res["status"], "done", res)
            self.assertTrue(any("usage read ignored" in ln for ln in logs))
            self.assertTrue(any("mid-run rate note" in ln for ln in logs))
            self.assertTrue(any("active assignments unreadable" in ln for ln in logs))

            # 2) Dead job stops
            clk_dead = FakeClock()
            sim_dead = SimColab(
                clock=clk_dead,
                usage=usage_text(rate=10.2, n=1, balance=100.0),
                polls=lambda n, env: rec(alive=False, progress=0),
            )
            res_dead = launch_window(
                spec, root / "dead_out", colab=sim_dead, clock=clk_dead.now, sleep=clk_dead.sleep,
                state_dir=root / "state",
            )
            self.assertEqual(res_dead["status"], "dead")
            self.assertTrue(res_dead["session_stopped_confirmed"])

            # 3) Wall stops cleanly
            clk_wall = FakeClock()

            def advance_wall(n, env):
                clk_wall.advance(100.0)
                return rec(progress=n)

            sim_wall = SimColab(
                clock=clk_wall, usage=usage_text(rate=10.2, n=1), polls=advance_wall,
            )
            res_wall = launch_window(
                spec, root / "wall_out", wall_s=150.0, colab=sim_wall, clock=clk_wall.now, sleep=clk_wall.sleep,
                state_dir=root / "state",
            )
            self.assertEqual(res_wall["status"], "wall")
            self.assertTrue(res_wall["session_stopped_confirmed"])

            # 4) Cap (own rate x elapsed) stops cleanly even when usage fails mid-run
            clk_cap = FakeClock()

            def advance_cap(n, env):
                clk_cap.advance(400.0)
                return rec(progress=n)

            cap_seq = [usage_text(rate=0.0, n=0), usage_text(rate=10.2, n=1), usage_text(rate=10.2, n=1)]
            sim_cap = SimColab(
                clock=clk_cap,
                usage=lambda idx: cap_seq[idx - 1] if idx <= 3 else CliResult(1, "", "lost"),
                polls=advance_cap,
            )
            res_cap = launch_window(
                spec, root / "cap_out", cap=5.0, max_rate=10.5, colab=sim_cap, clock=clk_cap.now, sleep=clk_cap.sleep,
                state_dir=root / "state",
            )
            self.assertEqual(res_cap["status"], "cap")
            self.assertEqual(res_cap["cap_trip"]["code"], "CAP_GUARD_TRIPPED")

    def test_pr21_followups_tar_move_failure_and_resident_missing_outputs(self):
        if os.name != "posix":
            self.skipTest("LocalColab tar-output execution requires POSIX bash/nohup")
        t = fake_tokens()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            (root / "poison_worker.py").write_text(
                "from pathlib import Path\nimport os\n"
                "d = Path(os.environ['CR_WORK_DIR']) / 'bad_dir'\n"
                "d.mkdir(parents=True, exist_ok=True)\n"
                f"(d / 'leak.png').write_bytes(b'\\x89PNG\\x00' + {t['hf'].encode('ascii')!r})\n",
                encoding="utf-8",
            )
            spec = root / "spec_a.toml"
            spec.write_text(
                'schema_version = 1\n'
                '[project]\nrepo = "org/demo"\nstudy = "fu-a"\njob = "fu_a"\n'
                '[compute]\ncard = "CPU"\npoll_s = 0.15\nwall_s = 30\ncap = 8.0\n'
                '[inputs]\nroot = "."\nfiles = ["poison_worker.py"]\n'
                '[job]\nrun = ["python3 poison_worker.py"]\n'
                '[outputs]\ncollect = ["bad_dir"]\n',
                encoding="utf-8",
            )
            out_a = root / "out_a"
            with mock.patch("colab_runner.colab_cli.shutil.move", side_effect=PermissionError("move blocked")):
                with self.assertRaises(BinaryDownloadRefused) as ctx_a:
                    launch_window(spec, out_a, local_cpu=True)
            self.assertEqual(ctx_a.exception.code, "BINARY_DOWNLOAD_TOKEN")
            self.assertEqual(len(ctx_a.exception.failures), 1)
            self.assertEqual(len(ctx_a.exception.removed), 1)
            outcome_a = json.loads((out_a / "outcome.json").read_text(encoding="utf-8"))
            self.assertEqual(outcome_a["status"], "error")
            self.assertEqual(outcome_a["refusal"]["code"], "BINARY_DOWNLOAD_TOKEN")
            self.assertEqual(outcome_a["output_errors"], {"bad_dir": "BINARY_DOWNLOAD_TOKEN"})

            # Follow-up (b): resident log refusal skips _collect so missing_outputs is []
            spec_b = root / "spec_b.toml"
            spec_b.write_text(
                'schema_version = 1\n'
                '[project]\nrepo = "org/demo"\nstudy = "fu-b"\njob = "fu_b"\n'
                '[compute]\ncard = "CPU"\npoll_s = 0.1\nwall_s = 30\ncap = 8.0\nresident = true\n'
                '[inputs]\nroot = "."\nfiles = ["poison_worker.py"]\n'
                '[job]\nrun = ["python3 poison_worker.py"]\n'
                '[outputs]\ncollect = ["a.json", "b.json"]\n',
                encoding="utf-8",
            )
            sim_b = SimColab(
                vm_poll_file=lambda n: rec(rc=0, progress=1),
                run_log=b"\x89PNG\x00" + t["hf"].encode("ascii"),
            )
            out_b = root / "out_b"
            with self.assertRaises(BinaryDownloadRefused):
                launch_window(spec_b, out_b, colab=sim_b, state_dir=root / "sb")
            outcome_b = json.loads((out_b / "outcome.json").read_text(encoding="utf-8"))
            self.assertEqual(outcome_b["status"], "error")
            self.assertEqual(outcome_b["refusal"]["code"], "BINARY_DOWNLOAD_TOKEN")
            self.assertEqual(outcome_b["missing_outputs"], [])

    def test_queue_failure_isolation_and_four_way_counts(self):
        if os.name != "posix":
            self.skipTest("queue --local-cpu execution requires POSIX bash/nohup")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / ".git").mkdir()
            (root / "ok.py").write_text(
                "import json, os\nfrom pathlib import Path\n"
                "w = Path(os.environ['CR_WORK_DIR'])\n"
                "(w / 'out.txt').write_text('ok\\n', encoding='utf-8')\n"
                "(w / 'result.json').write_text(json.dumps({'ok': True}), encoding='utf-8')\n",
                encoding="utf-8",
            )
            (root / "fail.py").write_text("import sys; sys.exit(5)\n", encoding="utf-8")

            s1 = root / "j1.toml"
            s1.write_text(
                'schema_version = 1\n'
                '[project]\nrepo = "org/q"\nstudy = "q-study"\njob = "j1"\nline = "l1"\n'
                '[compute]\ncard = "CPU"\npoll_s = 0.1\nwall_s = 30\ncap = 1.0\n'
                '[inputs]\nroot = "."\nfiles = ["ok.py"]\n'
                '[job]\nrun = ["python3 ok.py"]\n[outputs]\ncollect = ["result.json"]\n',
                encoding="utf-8",
            )
            s2 = root / "j2.toml"
            s2.write_text(
                s1.read_text(encoding="utf-8").replace('job = "j1"', 'job = "j2"').replace("ok.py", "fail.py"),
                encoding="utf-8",
            )
            s3 = root / "j3.toml"
            s3.write_text(
                s1.read_text(encoding="utf-8").replace('job = "j1"', 'job = "j3"'),
                encoding="utf-8",
            )

            # Missing stated purpose rejected at queue validate
            bad_q = _write_queue_spec(
                root,
                'out_root = "q_runs"\nstate_dir = "q_state"\nline = "l1"\n'
                '[[jobs]]\nid = "w1"\nspec = "j1.toml"\n',
            )
            with self.assertRaises(Refused) as ctx_p:
                QueueSpec.from_file(bad_q)
            self.assertEqual(ctx_p.exception.code, "SPEC_INVALID")

            # Valid 3-job unattended queue: w1 done, w2 fails (isolated), w3 done
            qp = _write_queue_spec(
                root,
                'out_root = "q_runs"\nstate_dir = "q_state"\nline = "l1"\n'
                '[[jobs]]\nid = "w1"\nspec = "j1.toml"\npurpose = "First synthetic check"\n'
                '[[jobs]]\nid = "w2"\nspec = "j2.toml"\npurpose = "Intentional failure isolation check"\n'
                '[[jobs]]\nid = "w3"\nspec = "j3.toml"\npurpose = "Recovery window after failure"\n',
            )
            qres = run_queue(qp, local_cpu=True)
            self.assertEqual(qres["exit_code"], 2)
            self.assertTrue(qres["all_stopped_confirmed"])
            self.assertEqual(qres["counts"], {"done": 2, "failed-ours": 1, "failed-Google": 0, "refused-capacity": 0})
            self.assertEqual([j["status"] for j in qres["jobs"]], ["done", "failed", "done"])

            # `board` subcommand was removed
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc_board = main(["board"])
            self.assertEqual(rc_board, 1)
            self.assertIn("unknown arguments: board", err.getvalue())

            # Four-way classification checks
            self.assertEqual(classify_window_outcome({"status": "open_failed", "opened": False,
                             "refusal": {"code": "POOL_CEILING_REACHED", "message": "full"}}), "refused-capacity")
            self.assertEqual(classify_window_outcome({"status": "unreachable", "opened": True}), "failed-Google")
            self.assertFalse(lg.evaluate_wall_guard(elapsed_s=1.0, wall_s=10.0, grace_s=0.0)["tripped"])

    def test_queue_strict_keys_per_user_state_dir_and_edge_paths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = _write_spec(root, card="T4", shape="standard", cap=2.5, max_rate=1.6, wall_s=180)
            base_text = spec.read_text(encoding="utf-8")

            # Unknown keys in top-level, [queue], and [[jobs]] refuse with SPEC_INVALID
            spec_t4_ok = root / "t4_ok.toml"
            spec_t4_ok.write_text(base_text, encoding="utf-8")
            for bad_toml in (
                'out_root = "qr"\n[publish]\nrepo = "o/r"\n[[jobs]]\nid = "j1"\nspec = "t4_ok.toml"\npurpose = "p"\n',
                '[queue]\nout_root = "qr"\nextra = 1\n[[jobs]]\nid = "j1"\nspec = "t4_ok.toml"\npurpose = "p"\n',
                'out_root = "qr"\n[[jobs]]\nid = "j1"\nspec = "t4_ok.toml"\npurpose = "p"\nbogus = 2\n',
                'out_root = "qr"\nstate_dir = ""\n[[jobs]]\nid = "j1"\nspec = "t4_ok.toml"\npurpose = "p"\n',
            ):
                with self.assertRaises(Refused) as ctx_u:
                    QueueSpec.from_file(_write_queue_spec(root, bad_toml))
                self.assertEqual(ctx_u.exception.code, "SPEC_INVALID")
            with self.assertRaises(Refused) as ctx_qp:
                QueueSpec.from_file(_write_queue_spec(
                    root, 'out_root = "qr"\n[publish]\nrepo = "o/r"\n[[jobs]]\nid = "j1"\nspec = "t4_ok.toml"\npurpose = "p"\n',
                ))
            self.assertIn("hf upload", ctx_qp.exception.message)

            # Queue without state_dir defaults to launch's per-user state dir (and respects COLAB_RUNNER_STATE_DIR)
            qp_default = _write_queue_spec(
                root,
                'out_root = "qr_def"\nline = "ldef"\n'
                '[[jobs]]\nid = "d1"\nspec = "t4_ok.toml"\npurpose = "Default state dir check"\n',
            )
            q_def = QueueSpec.from_file(qp_default)
            self.assertIsNone(q_def.state_dir)
            fake_user_state = (root / "user_state").resolve()
            sim_def = SimColab(clock=FakeClock(), usage=usage_text(rate=1.4, n=1), polls=lambda n, env: rec(rc=0, progress=1))
            with mock.patch.object(launcher_mod, "_default_user_state_dir", return_value=fake_user_state):
                with mock.patch.dict(os.environ, {}, clear=False):
                    os.environ.pop("COLAB_RUNNER_STATE_DIR", None)
                    res_def = run_queue(qp_default, colab=sim_def, clock=sim_def.clock.now, sleep=sim_def.clock.sleep)
            self.assertEqual(Path(res_def["state_dir"]).resolve(), fake_user_state)
            self.assertTrue((fake_user_state / "study_ledger.json").is_file())

            env_state = (root / "env_state").resolve()
            qp_env = _write_queue_spec(
                root,
                'out_root = "qr_env"\nline = "lenv"\n'
                '[[jobs]]\nid = "e1"\nspec = "t4_ok.toml"\npurpose = "Env state dir check"\n',
            )
            sim_env = SimColab(clock=FakeClock(), usage=usage_text(rate=1.4, n=1), polls=lambda n, env: rec(rc=0, progress=1))
            with mock.patch.dict(os.environ, {"COLAB_RUNNER_STATE_DIR": str(env_state)}):
                res_env = run_queue(qp_env, colab=sim_env, clock=sim_env.clock.now, sleep=sim_env.clock.sleep)
            self.assertEqual(Path(res_env["state_dir"]).resolve(), env_state)

            # Queue validation errors, dry-run via CLI, card mix enforced by cards.py, pool ceiling, halt on unconfirmed stop
            with self.assertRaises(Refused):
                QueueSpec.from_file(root / "missing_q.toml")
            spec_g4 = root / "g4.toml"
            spec_g4.write_text(
                base_text.replace('card = "T4"', 'card = "G4"').replace('study = "demo-study"', 'study = "same-study"'),
                encoding="utf-8",
            )
            spec_t4 = root / "t4.toml"
            spec_t4.write_text(
                base_text.replace('study = "demo-study"', 'study = "same-study"'),
                encoding="utf-8",
            )
            clk_mix = FakeClock()
            sim_mix = SimColab(
                clock=clk_mix, usage=usage_text(rate=10.2, n=1), polls=lambda n, env: rec(rc=0, progress=1),
                remote_files={
                    "/content/demo_job_work/result.json": b"{}\n",
                    "/content/demo_job_work/out.txt": b"ok\n",
                    "/content/demo_job_out/vm_card.json": b"{}\n",
                },
            )
            q_mix = _write_queue_spec(
                root,
                'out_root = "qr_mix"\nstate_dir = "qs_mix"\nline = "lmix"\n'
                '[[jobs]]\nid = "a"\nspec = "g4.toml"\npurpose = "p1"\n'
                '[[jobs]]\nid = "b"\nspec = "t4.toml"\npurpose = "p2"\n',
            )
            res_mix = run_queue(q_mix, colab=sim_mix, clock=clk_mix.now, sleep=clk_mix.sleep)
            self.assertEqual(res_mix["jobs"][0]["status"], "done")
            self.assertEqual(res_mix["jobs"][1]["outcome_bucket"], "failed-ours")
            self.assertIn("CARD_MIX_REFUSED", res_mix["jobs"][1]["error"] or "")

            q_ok = _write_queue_spec(
                root,
                'out_root = "qr2"\nstate_dir = "qs2"\nline = "l2"\n'
                '[[jobs]]\nid = "dry1"\nspec = "t4.toml"\npurpose = "Dry queue check"\n',
            )
            out_q, err_q = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out_q), contextlib.redirect_stderr(err_q):
                rc_val = main(["queue", "validate", "--queue", str(q_ok)])
                rc_dry = main(["queue", "run", "--queue", str(q_ok), "--dry"])
            self.assertEqual((rc_val, rc_dry), (0, 0))
            self.assertIn("QUEUE_VALID jobs=1", out_q.getvalue())
            self.assertIn("QUEUE_END jobs=1", out_q.getvalue())

            # Pool ceiling -> refused-capacity via launch_window's _pool_check
            clk_p = FakeClock()
            full_t4 = ["[t%d] e | Hardware: T4 | Shape: Standard | Variant: GPU" % i for i in range(3)]
            sim_full = SimColab(clock=clk_p, sessions_rows=full_t4)
            q_full = run_queue(
                _write_queue_spec(root, 'out_root = "qr_full"\nstate_dir = "qs_full"\nline = "lf"\n[[jobs]]\nid = "f1"\nspec = "t4.toml"\npurpose = "Pool ceiling test"\n'),
                colab=sim_full, clock=clk_p.now, sleep=clk_p.sleep,
            )
            self.assertEqual(q_full["jobs"][0]["outcome_bucket"], "refused-capacity")

            # Unconfirmed stop halts queue cleanly
            clk_w = FakeClock()
            q_wait = _write_queue_spec(
                root,
                'out_root = "qr_wait"\nstate_dir = "qs_wait"\nline = "lw"\n'
                '[[jobs]]\nid = "w1"\nspec = "t4.toml"\npurpose = "Unconfirmed stop check"\n'
                '[[jobs]]\nid = "w2"\nspec = "t4.toml"\npurpose = "Should not run after halt"\n',
            )
            sim_wait = SimColab(clock=clk_w, stops=[1] * 20, usage=usage_text(rate=1.4, n=1), polls=lambda n, env: rec(rc=0, progress=1))
            q_halted = run_queue(q_wait, colab=sim_wait, clock=clk_w.now, sleep=clk_w.sleep)
            self.assertIn("STOP_NOT_CONFIRMED", q_halted["halted_reason"])
            self.assertEqual(q_halted["jobs"][1]["state"], "queued")

            # Native Windows inside run_queue raises PLATFORM_UNSUPPORTED; unexpected exception marks job failed-ours
            with mock.patch.object(launcher_mod.sys, "platform", "win32"):
                with self.assertRaises(Refused) as ctx_qw:
                    run_queue(q_ok, local_cpu=True)
                self.assertEqual(ctx_qw.exception.code, "PLATFORM_UNSUPPORTED")
            q_err_spec = _write_queue_spec(
                root,
                'out_root = "qr_err"\nstate_dir = "qs_err"\nline = "l2"\n'
                '[[jobs]]\nid = "e1"\nspec = "t4.toml"\npurpose = "Runtime error check"\n',
            )
            with mock.patch.object(launcher_mod, "launch_window", side_effect=RuntimeError("boom")):
                q_err = run_queue(q_err_spec, local_cpu=True)
            self.assertEqual((q_err["jobs"][0]["status"], q_err["jobs"][0]["outcome_bucket"]), ("error", "failed-ours"))


if __name__ == "__main__":
    unittest.main()

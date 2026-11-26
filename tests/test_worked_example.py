"""End-to-end worked example test for merchant-league local OpenAI-compatible seats."""

import contextlib
import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from colab_runner.__main__ import main

EXAMPLE_DIR = Path(__file__).resolve().parents[1] / "examples" / "merchant_league_seats"
EXAMPLE_SPEC = EXAMPLE_DIR / "colab-window.toml"


class WorkedExampleTest(unittest.TestCase):
    def test_merchant_league_worked_example_dry_smoke_t4_and_local_cpu(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # Item 25: proposed smoke spec on T4 (180s wall, cap 2.5, max_rate 1.5) -> min_vram=14000 + open phase bound
            smoke_dry = root / "smoke-t4-dry"
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = main([
                    "launch", "--spec", str(EXAMPLE_SPEC), "--out-dir", str(smoke_dry), "--dry",
                    "--card", "T4", "--shape", "standard", "--max-rate", "1.5", "--wall-s", "180", "--cap", "2.5",
                ])
            self.assertEqual(rc, 0, err.getvalue())
            self.assertIn("DRY_RUN_OK", out.getvalue())
            self.assertIn("open_phase_bound_s:", out.getvalue())
            self.assertIn("unbounded until stop --out-dir", out.getvalue())
            plan = json.loads((smoke_dry / "dry_run_plan.json").read_text(encoding="utf-8"))
            self.assertEqual(plan["card"], "T4")
            self.assertEqual(plan["min_vram_mib"], 14000)
            self.assertGreaterEqual(plan["budget"]["cleanup_bound_s"], 1376.0)
            self.assertLessEqual(plan["budget"]["worst_case_total_spend"], 2.5)
            self.assertEqual(len(plan["staged_files"]), 4)

            # Item 25: `run_seats.py --mode gpu-live` with `CR_SMOKE_STUB=1` runs without external model server
            smoke_work = root / "smoke_work"
            smoke_out = root / "smoke_out"
            smoke_work.mkdir()
            for fname in ("seats.json", "wrapper.md", "response_schema.json", "run_seats.py"):
                (smoke_work / fname).write_text((EXAMPLE_DIR / fname).read_text(encoding="utf-8"), encoding="utf-8")
            env = dict(os.environ, CR_WORK_DIR=str(smoke_work), CR_OUT_DIR=str(smoke_out), CR_SMOKE_STUB="1")
            proc = subprocess.run(
                [sys.executable, str(smoke_work / "run_seats.py"), "--mode", "gpu-live", "--cap-gpu-secs", "120.2"],
                cwd=str(smoke_work), env=env, stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=30,
            )
            self.assertEqual(proc.returncode, 0, proc.stderr)
            smoke_sum = json.loads((smoke_work / "turn_summary.json").read_text(encoding="utf-8"))
            self.assertTrue(smoke_sum.get("smoke_stub"))

            if os.name != "posix":
                self.skipTest("local-cpu VM execution requires POSIX bash/nohup")

            cpu_dir = root / "ml-cpu"
            out2, err2 = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out2), contextlib.redirect_stderr(err2):
                rc2 = main([
                    "launch", "--spec", str(EXAMPLE_SPEC), "--out-dir", str(cpu_dir), "--local-cpu", "--quiet",
                ])
            self.assertEqual(rc2, 0, err2.getvalue())
            self.assertIn("LAUNCH_END status=done", out2.getvalue())

            outcome = json.loads((cpu_dir / "outcome.json").read_text(encoding="utf-8"))
            self.assertEqual(outcome["status"], "done")
            self.assertEqual(outcome["card"], "CPU")
            self.assertTrue(outcome["session_stopped_confirmed"])
            self.assertEqual(outcome["spend_basis"], "cpu_configured_rate_x_elapsed")
            self.assertIn("from-seat", outcome["collected_outputs"])
            self.assertEqual(outcome["missing_outputs"], [])
            self.assertTrue((cpu_dir / "_local_vm_content" / "league_turn_01_out" / "from-seat.tar").is_file())

            summary = json.loads((cpu_dir / "collected" / "turn_summary.json").read_text(encoding="utf-8"))
            guilds = [s["guild"] for s in summary["seats"]]
            self.assertEqual(guilds, ["Foresters", "Saltworks", "Smiths", "Weavers"])
            for guild in guilds:
                reply = json.loads((cpu_dir / "collected" / "from-seat" / f"{guild}.json").read_text(encoding="utf-8"))
                self.assertEqual(reply["guild"], guild)

            # Fix-2 proof: directory output holding a text file with a fake proxy-token URL stays `done` and scrubs cleanly.
            from tests.test_tokens import fake_tokens

            proxy_tok = fake_tokens()["proxy"]
            ex_copy = root / "ex_proxy"
            (ex_copy / ".git").mkdir(parents=True)
            for fname in ("colab-window.toml", "wrapper.md", "response_schema.json", "run_seats.py"):
                (ex_copy / fname).write_text((EXAMPLE_DIR / fname).read_text(encoding="utf-8"), encoding="utf-8")
            seats_obj = json.loads((EXAMPLE_DIR / "seats.json").read_text(encoding="utf-8"))
            seats_obj["seats"][0]["note"] = f"https://colab.example/p?colab-runtime-proxy-token={proxy_tok}"
            (ex_copy / "seats.json").write_text(json.dumps(seats_obj, indent=2) + "\n", encoding="utf-8")

            proxy_dir = root / "ml-cpu-proxy"
            out_p, err_p = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out_p), contextlib.redirect_stderr(err_p):
                rc_p = main([
                    "launch", "--spec", str(ex_copy / "colab-window.toml"), "--out-dir", str(proxy_dir),
                    "--local-cpu", "--env", "ML_SMOKE_STUB=1", "--quiet",
                ])
            self.assertEqual(rc_p, 0, err_p.getvalue())
            out_proxy = json.loads((proxy_dir / "outcome.json").read_text(encoding="utf-8"))
            self.assertEqual(out_proxy["status"], "done", out_proxy)
            self.assertIn("from-seat", out_proxy["collected_outputs"])
            self.assertEqual(out_proxy["missing_outputs"], [])
            self.assertEqual(out_proxy["output_errors"], {})
            foresters = (proxy_dir / "collected" / "from-seat" / "Foresters.json").read_text(encoding="utf-8")
            self.assertIn("colab-runtime-proxy-token=[REDACTED]", foresters)
            self.assertNotIn(proxy_tok, foresters)

    def test_rendered_job_script_under_bash_u_uses_build_job_env(self):
        if os.name != "posix":
            self.skipTest("bash -u VM script test requires POSIX")
        from colab_runner.cards import resolve_card
        from colab_runner.launcher import LocalColab, build_job_env
        from colab_runner.manifest import WindowSpec, render_vm_script, stage_inputs

        self.assertFalse(hasattr(LocalColab, "_cell_env"))
        spec = WindowSpec.from_file(EXAMPLE_SPEC)
        card_spec = resolve_card("T4")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            content_dir = root / "content"
            content_dir.mkdir()
            bundle_name = f"{spec.job}_inputs.tar.gz"
            stage_inputs(spec, content_dir / bundle_name, actual_card=card_spec.name, live=False, local_cpu=True)
            script_path = content_dir / f"{spec.job}_job.sh"
            script_path.write_text(render_vm_script(spec, card_spec, bundle_filename=bundle_name), encoding="utf-8")

            job_env = build_job_env(
                spec, card_spec, content=str(content_dir), local_cpu=True, extra_env={"ML_SMOKE_STUB": "1"},
            )
            clean_env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin")}
            clean_env.update(job_env)

            proc_ok = subprocess.run(
                ["bash", "-u", str(script_path)],
                cwd=str(content_dir), env=clean_env, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                timeout=30,
            )
            self.assertEqual(proc_ok.returncode, 0, proc_ok.stderr)
            out_dir = Path(clean_env["CR_OUT_DIR"])
            self.assertEqual((out_dir / "phase.txt").read_text(encoding="utf-8").strip(), "run")

            missing_out = dict(clean_env)
            missing_out.pop("CR_OUT_DIR")
            proc_fail = subprocess.run(
                ["bash", "-u", str(script_path)],
                cwd=str(content_dir), env=missing_out, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                timeout=30,
            )
            self.assertNotEqual(proc_fail.returncode, 0)
            self.assertIn("CR_OUT_DIR", proc_fail.stderr)

            missing_dry = dict(clean_env)
            missing_dry.pop("CR_DRY_RUN")
            proc_dry_fail = subprocess.run(
                ["bash", "-u", str(script_path)],
                cwd=str(content_dir), env=missing_dry, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                timeout=30,
            )
            self.assertNotEqual(proc_dry_fail.returncode, 0)
            self.assertIn("set by colab-runner; run this script through colab-runner", proc_dry_fail.stderr)


if __name__ == "__main__":
    unittest.main()

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

            summary = json.loads((cpu_dir / "collected" / "turn_summary.json").read_text(encoding="utf-8"))
            guilds = [s["guild"] for s in summary["seats"]]
            self.assertEqual(guilds, ["Foresters", "Saltworks", "Smiths", "Weavers"])
            for guild in guilds:
                reply = json.loads((cpu_dir / "collected" / "from-seat" / f"{guild}.json").read_text(encoding="utf-8"))
                self.assertEqual(reply["guild"], guild)


if __name__ == "__main__":
    unittest.main()

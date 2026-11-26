"""End-to-end worked example test for merchant-league local OpenAI-compatible seats."""

import contextlib
import io
import json
import os
import tempfile
import unittest
from pathlib import Path

from colab_runner.__main__ import main

EXAMPLE_SPEC = Path(__file__).resolve().parents[1] / "examples" / "merchant_league_seats" / "colab-window.toml"


class WorkedExampleTest(unittest.TestCase):
    def test_merchant_league_worked_example_dry_run_and_local_cpu(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            dry_dir = root / "ml-dry"
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc = main(["launch", "--spec", str(EXAMPLE_SPEC), "--out-dir", str(dry_dir), "--dry"])
            self.assertEqual(rc, 0, err.getvalue())
            self.assertIn("DRY_RUN_OK", out.getvalue())
            plan = json.loads((dry_dir / "dry_run_plan.json").read_text(encoding="utf-8"))
            self.assertEqual(plan["project"]["repo"], "mcp-tool-shop-org/merchant-league")
            self.assertEqual(plan["card"], "G4")
            self.assertEqual(plan["shape"], "standard")
            self.assertEqual(len(plan["staged_files"]), 4)

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

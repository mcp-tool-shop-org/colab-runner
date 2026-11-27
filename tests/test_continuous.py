"""Tests for Continuous Card v4, PR #21 follow-ups (a)/(b), Addendum 3 (three-stop rule), and Addendum 4 (Windows)."""

from __future__ import annotations

import contextlib
import hashlib
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
from colab_runner.publish import publish_window_outputs, resolve_hf_token, scan_publish_files
from colab_runner.queue import LineLedger, QueueSpec, build_board, classify_window_outcome, format_board, run_queue
from colab_runner.window_lib import launch_guards as lg
from tests.fakes import FakeClock, rec, usage_text
from tests.test_launcher import SimColab, _write_spec
from tests.test_tokens import fake_tokens


class _MemoryHfTransport:
    """Offline Hugging Face REST mock recording every call and enforcing private-before-public ordering."""

    def __init__(self, *, conflict_409: bool = False):
        self.calls: list[tuple[str, str, dict | None]] = []
        self.repos: dict[str, dict] = {}
        self.conflict_409 = conflict_409
        if conflict_409:
            self.repos["datasets/mcp-tool-shop/demo-study-demo_job"] = {"private": False}

    def __call__(self, method: str, url: str, headers: dict, body: bytes | None, timeout: float = 60.0):
        parsed = None
        if body and headers.get("Content-Type") == "application/json":
            parsed = json.loads(body.decode("utf-8"))
        self.calls.append((method, url, parsed))

        if method == "POST" and url.endswith("/api/repos/create"):
            repo_id = "%s/%s" % (parsed["organization"], parsed["name"])
            key = "%ss/%s" % (parsed["type"], repo_id)
            if self.conflict_409 and key in self.repos:
                return 409, {}, json.dumps({"error": "already exists"}).encode("utf-8")
            self.repos[key] = {"private": bool(parsed["private"]), "files": {}}
            return 200, {}, json.dumps({"url": url}).encode("utf-8")

        if "/settings" in url and method == "PUT":
            key = url.split("/api/", 1)[1].rsplit("/settings", 1)[0]
            self.repos.setdefault(key, {})["private"] = bool(parsed["private"])
            return 200, {}, json.dumps(self.repos[key]).encode("utf-8")

        if "/preupload/main" in url and method == "POST":
            out_files = [
                {"path": f["path"], "uploadMode": "lfs" if f["path"].endswith(".bin") else "regular"}
                for f in parsed["files"]
            ]
            return 200, {}, json.dumps({"files": out_files}).encode("utf-8")

        if url.endswith("/info/lfs/objects/batch") and method == "POST":
            objs = []
            for o in parsed["objects"]:
                objs.append({
                    "oid": o["oid"],
                    "size": o["size"],
                    "actions": {"upload": {"href": "https://lfs.example/upload/%s" % o["oid"], "header": {}}},
                })
            return 200, {}, json.dumps({"objects": objs}).encode("utf-8")

        if url.startswith("https://lfs.example/upload/") and method == "PUT":
            return 200, {}, b""

        if "/commit/main" in url and method == "POST":
            return 200, {}, json.dumps({"commitOid": "abc123def456"}).encode("utf-8")

        if method == "GET" and "/api/" in url:
            key = url.split("/api/", 1)[1]
            return 200, {}, json.dumps(self.repos.get(key, {"private": True})).encode("utf-8")

        return 404, {}, b"{}"


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

    def test_hf_publish_private_first_identity_scan_and_sealed_block(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            spec = _write_spec(root, card="T4", shape="standard", cap=2.5, max_rate=1.6, wall_s=180)
            spec.write_text(
                spec.read_text(encoding="utf-8").replace(
                    '[project]',
                    '[project]\ndata_class = "synthetic"\npurpose = "Synthetic HF upload check"\nline = "cr-dev"',
                ) + '\n[publish]\norg = "mcp-tool-shop"\nrepo_type = "dataset"\n',
                encoding="utf-8",
            )
            # Token resolution via HF_HOME/token
            hf_dir = root / "hf_home"
            hf_dir.mkdir()
            (hf_dir / "token").write_text("hf_test_token_value\n", encoding="utf-8")
            self.assertEqual(resolve_hf_token(environ={"HF_HOME": str(hf_dir)}), "hf_test_token_value")

            # Live synthetic launch with mocked transport -> private first, LFS .bin + README, scan, flip public
            clk = FakeClock()
            tr = _MemoryHfTransport(conflict_409=True)
            sim = SimColab(
                clock=clk,
                sessions_rows=[],
                usage=usage_text(rate=1.4, n=1),
                polls=lambda n, env: rec(rc=0, progress=1),
                remote_files={
                    "/content/demo_job_work/result.json": json.dumps({"ok": True}).encode("utf-8"),
                    "/content/demo_job_work/out.txt": b"clean synthetic\n",
                    "/content/demo_job_out/vm_card.json": json.dumps({"ok": True}).encode("utf-8"),
                },
            )
            with mock.patch.dict(os.environ, {"HF_HOME": str(hf_dir)}, clear=False):
                os.environ.pop("HF_TOKEN", None)
                os.environ.pop("COLAB_RUNNER_HF_TOKEN", None)
                res = launch_window(
                    spec, root / "pub_out", colab=sim, publish_transport=tr,
                    clock=clk.now, sleep=clk.sleep, state_dir=root / "pub_state",
                )
            self.assertEqual(res["status"], "done", res)
            self.assertEqual(res["exit_code"], 0)
            self.assertEqual(res["repo_url"], "https://huggingface.co/datasets/mcp-tool-shop/demo-study-demo_job")
            self.assertTrue(res["flipped_public_at"])
            self.assertTrue(res["publish"]["uploaded_private_first"])
            self.assertEqual(res["publish"]["identity_scan"], "clean")
            methods_urls = [(m, u) for m, u, _ in tr.calls]
            create_i = methods_urls.index(("POST", "https://huggingface.co/api/repos/create"))
            commit_i = next(i for i, (m, u) in enumerate(methods_urls) if m == "POST" and u.endswith("/commit/main"))
            flip_i = max(i for i, (m, u) in enumerate(methods_urls) if m == "PUT" and u.endswith("/settings"))
            self.assertLess(create_i, commit_i)
            self.assertLess(commit_i, flip_i)

            # Identity scan blocks leaked email / homepath before public flip
            bad_out = root / "bad_pub"
            (bad_out / "collected").mkdir(parents=True)
            (bad_out / "collected" / "weights.bin").write_bytes(b"\x00/home/alice/secret\n")
            from colab_runner.manifest import WindowSpec

            wspec = WindowSpec.from_file(spec)
            tr2 = _MemoryHfTransport()
            bad_res = publish_window_outputs(
                spec=wspec, out_dir=bad_out, outcome=res, transport=tr2, environ={"HF_TOKEN": "hf_abc"},
            )
            self.assertEqual(bad_res["status"], "scan_failed")
            self.assertEqual(tr2.calls, [])
            self.assertTrue(any(f["reason"] == "homepath_posix" for f in scan_publish_files({"x": b"/home/alice/a"})))

            # Sealed + data_class = public is refused up front
            spec_sealed_pub = root / "sealed_pub.toml"
            spec_sealed_pub.write_text(
                'schema_version = 1\n'
                '[project]\nrepo = "o/r"\nstudy = "s"\njob = "j"\ndata_class = "public"\n'
                '[compute]\ncard = "CPU"\ncap = 1.0\nwall_s = 60\n'
                '[inputs]\nroot = "."\nfiles = []\n'
                '[inputs.sealed]\npreregistration_path = "p.txt"\n'
                f'preregistration_sha256 = "{hashlib.sha256(b"x").hexdigest()}"\n'
                'allowed_cards = ["CPU"]\nallowed_host = "colab.google.com"\n'
                '[job]\nrun = ["true"]\n',
                encoding="utf-8",
            )
            with self.assertRaises(Refused) as ctx_sp:
                WindowSpec.from_file(spec_sealed_pub)
            self.assertEqual(ctx_sp.exception.code, "SEALED_DATA_REFUSED")

    def test_queue_budget_failure_isolation_board_and_four_way_counts(self):
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
                '[project]\nrepo = "org/q"\nstudy = "q-study"\njob = "j1"\nline = "l1"\ndata_class = "synthetic"\n'
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

            # Board & budget CLI
            b = build_board(state_dir=root / "q_state", out_root=root / "q_runs")
            txt = format_board(b)
            self.assertIn("LINES & BUDGETS", txt)
            self.assertIn("Recovery window after failure", txt)
            self.assertEqual(b["totals"]["counts"]["done"], 2)
            self.assertEqual(b["totals"]["counts"]["failed-ours"], 1)

            # Budget CLI set/show & budget deferral in queue
            state2 = root / "q_state2"
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                rc_set = main(["budget", "set", "--line", "l1", "--ccu", "0.5", "--state-dir", str(state2)])
                rc_show = main(["budget", "show", "--line", "l1", "--state-dir", str(state2)])
                rc_board = main(["board", "--state-dir", str(root / "q_state"), "--out-root", str(root / "q_runs")])
            self.assertEqual((rc_set, rc_show, rc_board), (0, 0, 0))
            self.assertIn("BUDGET line=l1 budget=0.5", out.getvalue())
            self.assertIn("TOTALS  windows=3", out.getvalue())

            q_def = run_queue(qp, local_cpu=True, state_dir=state2)
            self.assertEqual(q_def["jobs"][0]["status"], "deferred_budget")

            # Four-way classification checks
            self.assertEqual(classify_window_outcome({"status": "open_failed", "opened": False,
                             "refusal": {"code": "POOL_CEILING_REACHED", "message": "full"}}), "refused-capacity")
            self.assertEqual(classify_window_outcome({"status": "unreachable", "opened": True}), "failed-Google")
            LineLedger(state2 / "line_ledger.json").set_budget("l1", None)
            self.assertFalse(lg.evaluate_wall_guard(elapsed_s=1.0, wall_s=10.0, grace_s=0.0)["tripped"])

    def test_publish_queue_budget_and_cli_edge_paths(self):
        import urllib.error
        from colab_runner.manifest import WindowSpec
        from colab_runner.publish import _hf_home_token_path, default_hf_transport
        from colab_runner.queue import build_board

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # 1) Token lookup fallbacks + POSIX drive translation
            fake_home = root / "home"
            (fake_home / ".cache" / "huggingface").mkdir(parents=True)
            (fake_home / ".cache" / "huggingface" / "token").write_text("hf_cache_tok\n", encoding="utf-8")
            self.assertEqual(resolve_hf_token(environ={}, home=fake_home), "hf_cache_tok")
            self.assertIsNone(resolve_hf_token(environ={}, home=root / "empty_home"))
            with mock.patch("colab_runner.publish.os.name", "posix"), mock.patch.object(Path, "exists", return_value=True):
                self.assertEqual(_hf_home_token_path("E:\\AI\\hf", fake_home).as_posix(), "/mnt/e/AI/hf/token")

            # 2) default_hf_transport success + HTTPError
            resp_mock = mock.MagicMock()
            resp_mock.__enter__.return_value = resp_mock
            resp_mock.status = 200
            resp_mock.headers = {"Content-Type": "application/json"}
            resp_mock.read.return_value = b'{"ok":true}'
            with mock.patch("colab_runner.publish.urllib.request.urlopen", return_value=resp_mock):
                st, hdrs, body = default_hf_transport("GET", "https://huggingface.co/api/whoami", {}, None)
            self.assertEqual((st, hdrs["content-type"], body), (200, "application/json", b'{"ok":true}'))
            http_err = urllib.error.HTTPError("https://huggingface.co/x", 404, "not found", {}, io.BytesIO(b"nope"))
            try:
                with mock.patch("colab_runner.publish.urllib.request.urlopen", side_effect=http_err):
                    st_e, _, body_e = default_hf_transport("GET", "https://huggingface.co/x", {}, None)
                self.assertEqual((st_e, body_e), (404, b"nope"))
            finally:
                http_err.close()

            # 3) Publish LFS, model repo_type, deny_terms, token_missing, and error responses
            spec = _write_spec(root, card="T4", shape="standard", cap=2.5, max_rate=1.6, wall_s=180)
            spec.write_text(
                spec.read_text(encoding="utf-8").replace("[project]", '[project]\ndata_class = "public"')
                + '\n[publish]\nrepo_type = "model"\nrepo_name = "demo-model"\n',
                encoding="utf-8",
            )
            wspec = WindowSpec.from_file(spec)
            pub_dir = root / "pub_lfs"
            (pub_dir / "collected").mkdir(parents=True)
            (pub_dir / "collected" / "weights.bin").write_bytes(b"\x00BINARY_DATA")
            (pub_dir / "collected" / "README.md").write_text("ignored collected readme", encoding="utf-8")
            base_out = {"project": {"purpose": "LFS test", "line": "l"}, "card": "T4", "spend_ccu": 0.1, "elapsed_s": 10}
            tr_lfs = _MemoryHfTransport(conflict_409=False)
            r_lfs = publish_window_outputs(
                spec=wspec, out_dir=pub_dir, outcome=base_out, transport=tr_lfs, environ={"HF_TOKEN": "hf_x"},
            )
            self.assertEqual(r_lfs["status"], "published")
            self.assertEqual(r_lfs["repo_url"], "https://huggingface.co/mcp-tool-shop/demo-model")
            self.assertEqual(publish_window_outputs(spec=wspec, out_dir=pub_dir, outcome=base_out, environ={})["status"], "token_missing")

            self.assertEqual(scan_publish_files({"a.bin": b"\x00secret_tok", "b.txt": b"secret_tok"}, extra_deny=["secret_tok"])[0]["reason"], "deny_term")
            for fail_step in (
                lambda m, u, h, b, t=60: (500, {}, b'{"err":"create"}') if "create" in u else (200, {}, b"{}"),
                lambda m, u, h, b, t=60: (200, {}, b'{"private":false}') if m == "GET" else (200, {}, b"{}"),
                lambda m, u, h, b, t=60: (500, {}, b"{}") if "preupload" in u else ((200, {}, b'{"private":true}') if m == "GET" else (200, {}, b"{}")),
                lambda m, u, h, b, t=60: (500, {}, b"{}") if "commit" in u else ((200, {}, b'{"private":true}') if m == "GET" else (200, {}, b'{"files":[]}')),
                lambda m, u, h, b, t=60: (500, {}, b"{}") if "settings" in u else ((200, {}, b'{"private":true}') if m == "GET" else (200, {}, b'{"files":[]}')),
            ):
                r_err = publish_window_outputs(
                    spec=wspec, out_dir=pub_dir, outcome=base_out, transport=fail_step, environ={"HF_TOKEN": "hf_x"},
                )
                self.assertIn(r_err["status"], ("upload_failed", "flip_failed"))

            # 4) Queue validation errors, dry-run via CLI, pool ceiling skip, halt on unconfirmed stop, budget CLI all lines
            with self.assertRaises(Refused):
                QueueSpec.from_file(root / "missing_q.toml")
            spec_g4 = root / "g4.toml"
            spec_g4.write_text(spec.read_text(encoding="utf-8").replace('card = "T4"', 'card = "G4"').replace('study = "demo-study"', 'study = "same-study"'), encoding="utf-8")
            spec_t4 = root / "t4.toml"
            spec_t4.write_text(spec.read_text(encoding="utf-8").replace('study = "demo-study"', 'study = "same-study"'), encoding="utf-8")
            q_mix = _write_queue_spec(
                root,
                '[[jobs]]\nid = "a"\nspec = "g4.toml"\npurpose = "p1"\n'
                '[[jobs]]\nid = "b"\nspec = "t4.toml"\npurpose = "p2"\n',
            )
            with self.assertRaises(Refused) as ctx_mix:
                QueueSpec.from_file(q_mix)
            self.assertEqual(ctx_mix.exception.code, "CARD_MIX_REFUSED")

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

            # Pool ceiling -> refused-capacity without opening
            clk_p = FakeClock()
            full_t4 = ["[t%d] e | Hardware: T4 | Shape: Standard | Variant: GPU" % i for i in range(3)]
            sim_full = SimColab(clock=clk_p, sessions_rows=full_t4)
            q_full = run_queue(
                _write_queue_spec(root, 'out_root = "qr_full"\nstate_dir = "qs_full"\nline = "lf"\n[[jobs]]\nid = "f1"\nspec = "t4.toml"\npurpose = "Pool ceiling test"\n'),
                colab=sim_full, clock=clk_p.now, sleep=clk_p.sleep,
            )
            self.assertEqual(q_full["jobs"][0]["outcome_bucket"], "refused-capacity")

            # Ledger-only board + budget show summary all lines + budget error
            led = LineLedger(root / "qs_full" / "line_ledger.json")
            led.set_budget("lf", 5.0, note="standing")
            led.record_outcome("lf", {"window_id": "wid-1", "project": {"job": "j", "purpose": "p"}, "card": "T4", "status": "done", "spend_ccu": 1.25}, out_dir=root / "none")
            led.record_outcome("lf", {"window_id": "wid-1", "project": {"job": "j", "purpose": "p"}, "card": "T4", "status": "done", "spend_ccu": 1.5}, out_dir=root / "none")
            b_led = build_board(state_dir=root / "qs_full")
            self.assertEqual(b_led["totals"]["spend_ccu"], 1.5)
            out_b, err_b = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out_b), contextlib.redirect_stderr(err_b):
                self.assertEqual(main(["budget", "show", "--state-dir", str(root / "qs_full")]), 0)
                self.assertEqual(main(["board", "--state-dir", str(root / "qs_full"), "--json"]), 0)
                self.assertEqual(main(["budget", "set", "--line", "lf"]), 1)
            self.assertIn("LEDGER lines=1", out_b.getvalue())
            self.assertIn("REFUSED: CAP_REQUIRED", err_b.getvalue())

            # Pool-wait recovers on second check; unconfirmed stop halts queue cleanly
            clk_w = FakeClock()
            pool_polls = {"n": 0}

            class _WaitStopColab(SimColab):
                def sessions(self, timeout=30.0):
                    pool_polls["n"] += 1
                    if pool_polls["n"] == 1:
                        return CliResult(0, "\n".join(full_t4) + "\n")
                    return super().sessions(timeout=timeout)

            q_wait = _write_queue_spec(
                root,
                'out_root = "qr_wait"\nstate_dir = "qs_wait"\nline = "lw"\n'
                '[[jobs]]\nid = "w1"\nspec = "t4.toml"\npurpose = "Wait slot then unconfirmed stop"\n'
                '[[jobs]]\nid = "w2"\nspec = "t4.toml"\npurpose = "Should not run after halt"\n',
            )
            sim_wait = _WaitStopColab(clock=clk_w, stops=[1] * 20, usage=usage_text(rate=1.4, n=1), polls=lambda n, env: rec(rc=0, progress=1))
            q_halted = run_queue(q_wait, colab=sim_wait, pool_wait_s=5.0, pool_poll_s=1.0, clock=clk_w.now, sleep=clk_w.sleep)
            self.assertIn("STOP_NOT_CONFIRMED", q_halted["halted_reason"])
            self.assertEqual(q_halted["jobs"][1]["state"], "queued")


if __name__ == "__main__":
    unittest.main()

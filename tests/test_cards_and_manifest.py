"""Tests for platform card catalog, TPU shapes, pool keys, circuit breaker, study reservation, and sealed sandbox."""

import hashlib
import json
import os
import stat
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from colab_runner.cards import (
    CARDS,
    CARD_CHECK_FAIL_RC,
    CircuitBreaker,
    StudyLedger,
    _load_json_dict,
    pool_key_for_card,
    pool_key_for_session_row,
    resolve_card,
    shape_row_label,
    utc_day_str,
    validate_card_and_shape,
    vm_card_check_py,
)
from colab_runner.colab_cli import CliResult, Colab, parse_sessions_output
from colab_runner.errors import Refused
from colab_runner.manifest import WindowSpec, _parse_preregistration, render_vm_script, stage_inputs
from colab_runner.runner import Runner


def _minimal_spec_dict(files=None, *, root=".", deny_globs=None, sealed=None, min_vram_by_card=None) -> dict:
    inp = {"root": root, "files": files or []}
    if deny_globs is not None:
        inp["deny_globs"] = deny_globs
    if sealed is not None:
        inp["sealed"] = sealed
    comp = {
        "card": "G4",
        "shape": "standard",
        "max_rate": 10.5,
        "cap": 8.0,
        "wall_s": 1800,
        "cleanup_bound_s": 600,
    }
    if min_vram_by_card is not None:
        comp["min_vram_mib_by_card"] = min_vram_by_card
    return {
        "schema_version": 1,
        "project": {"repo": "org/repo", "study": "study-alpha", "job": "job1", "description": "demo"},
        "compute": comp,
        "inputs": inp,
        "job": {"run": ["python3 run.py"]},
        "outputs": {"collect": ["out.json"], "salvage": ["setup.log"]},
    }


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_readonly(path: Path) -> None:
    if os.name == "posix":
        path.chmod(0o444)
    else:
        path.chmod(stat.S_IREAD)


def _make_writable(path: Path) -> None:
    if path.exists():
        path.chmod(0o644 if os.name == "posix" else (stat.S_IWRITE | stat.S_IREAD))


class CardCatalogTest(unittest.TestCase):
    def test_known_cards_shapes_pools_tpu_and_unavailable_card(self):
        self.assertEqual(CARDS["G4"].min_vram_mib, 90000)
        self.assertEqual(CARDS["G4"].min_rate, 10.05)
        self.assertEqual(shape_row_label("standard"), "Standard")
        self.assertEqual(shape_row_label("high-mem"), "High-RAM")

        spec, shape = validate_card_and_shape("cpu", None)
        self.assertEqual((spec.name, spec.cli_flag, shape), ("CPU", "--cpu", None))

        spec, shape = validate_card_and_shape("a100-80gb", "high-mem")
        self.assertEqual((spec.name, spec.cli_gpu, shape), ("A100_80GB", "A100", "high-mem"))
        self.assertEqual(pool_key_for_card(resolve_card("A100"), "standard"), "A100_40GB")
        self.assertEqual(pool_key_for_card(resolve_card("A100"), "high-mem"), "A100_80GB")
        self.assertEqual(pool_key_for_session_row({"hardware": "A100", "shape": "High-RAM"}), "A100_80GB")
        self.assertEqual(pool_key_for_session_row({"hardware": "A100", "shape": "Standard"}), "A100_40GB")
        self.assertEqual(pool_key_for_session_row({"hardware": "CPU", "shape": "High-RAM"}), "CPU")

        # TPU V5E1/V6E1 match real colab sessions `Shape: Standard` + `--tpu`
        tpu_spec, tpu_shape = validate_card_and_shape("V5E1", "standard")
        self.assertEqual((tpu_spec.name, tpu_spec.cli_flag, tpu_shape), ("V5E1", "--tpu", "standard"))

        bad_cases = [
            ("UNKNOWN_GPU", "standard", "CARD_UNKNOWN"),
            ("H100", "standard", "CARD_UNAVAILABLE"),
            ("CPU", "standard", "SHAPE_NOT_FOR_CPU"),
            ("G4", None, "SHAPE_REQUIRED"),
            ("G4", "ultra", "SHAPE_INVALID"),
            ("L4", "high-mem", "SHAPE_HIGH_MEM_UNAVAILABLE"),
            ("A100_80GB", "standard", "SHAPE_STANDARD_UNAVAILABLE"),
            ("V5E1", "high-mem", "SHAPE_HIGH_MEM_UNAVAILABLE"),
        ]
        for card, sh, code in bad_cases:
            with self.subTest(card=card, shape=sh, code=code):
                with self.assertRaises(Refused) as ctx:
                    validate_card_and_shape(card, sh)
                self.assertEqual(ctx.exception.code, code)

    def test_circuit_breaker_trips_at_two_and_resets_next_utc_day(self):
        with tempfile.TemporaryDirectory() as tmp:
            cb = CircuitBreaker(Path(tmp) / "card_refusals.json")
            cb.check("G4", "2026-11-26")
            self.assertEqual(cb.record_refusal("G4", reason="503", session="s1", day="2026-11-26"), 1)
            cb.check("G4", "2026-11-26")
            self.assertEqual(cb.record_refusal("G4", reason="503", session="s2", day="2026-11-26"), 2)
            with self.assertRaises(Refused) as ctx:
                cb.check("G4", "2026-11-26")
            self.assertEqual(ctx.exception.code, "CARD_CIRCUIT_OPEN")
            cb.check("L4", "2026-11-26")
            cb.check("G4", "2026-11-27")
            self.assertRegex(utc_day_str(), r"^\d{4}-\d{2}-\d{2}$")

    def test_study_ledger_reserves_card_and_refuses_card_mixing(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = StudyLedger(Path(tmp) / "ledger.json")
            ledger.reserve_card("study-a", "G4", session="s1", window_id="w1")
            ledger.check_card("study-a", "G4")
            ledger.check_card("study-a", "CPU")  # CPU dry runs never lock GPU card
            with self.assertRaises(Refused) as ctx:
                ledger.reserve_card("study-a", "A100", session="s2", window_id="w2")
            self.assertEqual(ctx.exception.code, "CARD_MIX_REFUSED")
            # Item 9: releasing an un-opened reservation clears the lock so another card can reserve.
            ledger.release_reservation("study-a", window_id="w1")
            ledger.reserve_card("study-a", "A100", session="s2", window_id="w2")
            ledger.record_window("study-a", {
                "window_id": "w2", "session": "s2", "card": "A100", "card_class": "gpu",
                "shape": "standard", "status": "done", "opened": True,
            })
            # Releasing a third window when w2 already opened preserves the A100 lock.
            ledger.reserve_card("study-a", "A100", session="s3", window_id="w3")
            ledger.release_reservation("study-a", window_id="w3")
            with self.assertRaises(Refused) as ctx:
                ledger.reserve_card("study-a", "T4", session="s4", window_id="w4")
            self.assertEqual(ctx.exception.code, "CARD_MIX_REFUSED")

    def test_vm_card_check_script_verifies_name_vram_and_compute_cap(self):
        if os.name != "posix":
            self.skipTest("shell script helper test requires POSIX")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            smi = root / "nvidia-smi"
            out_dir = root / "out"
            py_src = vm_card_check_py(resolve_card("G4"))

            smi.write_text("#!/bin/sh\necho 'NVIDIA RTX PRO 6000 Blackwell, 97887, 12.0'\n", encoding="utf-8")
            smi.chmod(smi.stat().st_mode | stat.S_IEXEC)
            env = dict(os.environ, CR_OUT_DIR=str(out_dir), CR_NVIDIA_SMI=str(smi))
            ok_proc = subprocess.run([sys.executable, "-c", py_src], env=env, capture_output=True, text=True,
                                     stdin=subprocess.DEVNULL, timeout=30)
            self.assertEqual(ok_proc.returncode, 0, ok_proc.stderr)
            self.assertTrue(json.loads((out_dir / "vm_card.json").read_text())["ok"])

            smi.write_text("#!/bin/sh\necho 'Tesla T4, 15360, 7.5'\n", encoding="utf-8")
            bad_proc = subprocess.run([sys.executable, "-c", py_src], env=env, capture_output=True, text=True,
                                      stdin=subprocess.DEVNULL, timeout=30)
            self.assertEqual(bad_proc.returncode, CARD_CHECK_FAIL_RC)
            self.assertIn("CR_CARD_MISMATCH:", bad_proc.stdout)
            self.assertFalse(json.loads((out_dir / "vm_card.json").read_text())["ok"])


class ManifestSandboxTest(unittest.TestCase):
    def test_toml_json_job_name_per_card_vram_and_validation_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with self.assertRaises(Refused) as ctx:
                WindowSpec.from_file(root / "missing.toml")
            self.assertEqual(ctx.exception.code, "SPEC_NOT_FOUND")

            bad_json = root / "bad.json"
            bad_json.write_text("{not valid json", encoding="utf-8")
            with self.assertRaises(Refused) as ctx:
                WindowSpec.from_file(bad_json)
            self.assertEqual(ctx.exception.code, "SPEC_INVALID")

            bad_job = _minimal_spec_dict(min_vram_by_card={"T4": 14000, "G4": 90000})
            bad_job["project"]["job"] = "bad/job"
            with self.assertRaises(Refused) as ctx:
                WindowSpec.from_mapping(bad_job, base_dir=root)
            self.assertEqual(ctx.exception.code, "SPEC_INVALID")

            good_job = _minimal_spec_dict(min_vram_by_card={"T4": 14000, "G4": 90000})
            spec_v = WindowSpec.from_mapping(good_job, base_dir=root)
            self.assertEqual(spec_v.vram_floor_for(resolve_card("T4")), 14000)
            self.assertEqual(spec_v.vram_floor_for(resolve_card("G4")), 90000)

            good_toml = root / "good.toml"
            good_toml.write_text(
                'schema_version = 1\n'
                '[project]\nrepo = "o/r"\nstudy = "s"\njob = "j_1-a"\n'
                '[compute]\ncard = "CPU"\ncap = 2.0\nwall_s = 600\n'
                '[inputs]\nfiles = ["a.txt"]\n'
                '[job]\nrun = ["echo ok"]\n',
                encoding="utf-8",
            )
            spec = WindowSpec.from_file(good_toml)
            self.assertEqual((spec.card, spec.shape, spec.max_rate), ("CPU", None, 0.26))

            data = _minimal_spec_dict()
            data["outputs"]["collect"] = ["../escape.txt"]
            with self.assertRaises(Refused) as ctx:
                WindowSpec.from_mapping(data, base_dir=root)
            self.assertEqual(ctx.exception.code, "OUTPUT_PATH_INVALID")

    def test_sealed_gate_refusals_and_verified_preregistration(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            repo = root / "repo"
            (repo / ".git").mkdir(parents=True)
            (repo / "ok.txt").write_text("clean\n", encoding="utf-8")
            (repo / "staging-dir").mkdir()
            (repo / "staging-dir" / "hold.txt").write_text("holdout\n", encoding="utf-8")
            (repo / "sealed-eval").mkdir()
            (repo / "sealed-eval" / "item.json").write_text("{}\n", encoding="utf-8")
            (repo / "sealed-eval" / "extra.json").write_text("{}\n", encoding="utf-8")

            sealed_root = root / "sealed-data-root"
            sealed_root.mkdir()
            (sealed_root / "items.jsonl").write_text('{"id": 1}\n', encoding="utf-8")

            outside_repo = root / "external-data"
            outside_repo.mkdir()
            (outside_repo / "data.txt").write_text("external\n", encoding="utf-8")

            # 1a: whole-path sealed root refused without [inputs.sealed]
            spec_sealed_root = WindowSpec.from_mapping(
                _minimal_spec_dict(["items.jsonl"], root=str(sealed_root)), base_dir=repo
            )
            with self.assertRaises(Refused) as ctx:
                stage_inputs(spec_sealed_root, root / "b0.tar.gz")
            self.assertEqual(ctx.exception.code, "SEALED_DATA_REFUSED")

            # 1b: root outside spec repository refused without [inputs.sealed]
            spec_outside = WindowSpec.from_mapping(
                _minimal_spec_dict(["data.txt"], root=str(outside_repo)), base_dir=repo
            )
            with self.assertRaises(Refused) as ctx:
                stage_inputs(spec_outside, root / "b1.tar.gz")
            self.assertEqual(ctx.exception.code, "SEALED_DATA_REFUSED")

            # 1c: caller custom deny_globs (`staging*`) refused
            spec_deny = WindowSpec.from_mapping(
                _minimal_spec_dict(["staging-dir/hold.txt"], deny_globs=["staging*"]), base_dir=repo
            )
            with self.assertRaises(Refused) as ctx:
                stage_inputs(spec_deny, root / "b2.tar.gz")
            self.assertEqual(ctx.exception.code, "SEALED_DATA_REFUSED")

            # Build valid pre-registration file
            prereg = repo / "prereg.md"
            prereg_body = (
                "# Pre-registration\n"
                "Study: study-alpha\n"
                "Allowed cards: G4\n"
                "Allowed host: colab.google.com\n"
                "sealed_path: sealed-eval/item.json\n"
            ).encode("utf-8")
            prereg.write_bytes(prereg_body)
            _make_readonly(prereg)
            prereg_sha = _sha256_bytes(prereg_body)
            try:
                sealed_cfg = {
                    "preregistration_path": "prereg.md",
                    "preregistration_sha256": prereg_sha,
                    "allowed_cards": ["G4"],
                    "allowed_host": "colab.google.com",
                }

                # 2a: wrong sha256 -> SEALED_PREREG_INVALID
                bad_sha = dict(sealed_cfg, preregistration_sha256="0" * 64)
                spec_bad_sha = WindowSpec.from_mapping(
                    _minimal_spec_dict(["sealed-eval/item.json"], sealed=bad_sha), base_dir=repo
                )
                with self.assertRaises(Refused) as ctx:
                    stage_inputs(spec_bad_sha, root / "b_sha.tar.gz")
                self.assertEqual(ctx.exception.code, "SEALED_PREREG_INVALID")

                # 2b: writable pre-registration file -> SEALED_PREREG_INVALID
                _make_writable(prereg)
                spec_valid = WindowSpec.from_mapping(
                    _minimal_spec_dict(["sealed-eval/item.json"], sealed=sealed_cfg), base_dir=repo
                )
                with self.assertRaises(Refused) as ctx:
                    stage_inputs(spec_valid, root / "b_writable.tar.gz")
                self.assertEqual(ctx.exception.code, "SEALED_PREREG_INVALID")
                _make_readonly(prereg)

                # 2c: study id missing -> SEALED_PREREG_INVALID
                bad_study = _minimal_spec_dict(["sealed-eval/item.json"], sealed=sealed_cfg)
                bad_study["project"]["study"] = "other-study"
                spec_bad_study = WindowSpec.from_mapping(bad_study, base_dir=repo)
                with self.assertRaises(Refused) as ctx:
                    stage_inputs(spec_bad_study, root / "b_study.tar.gz")
                self.assertEqual(ctx.exception.code, "SEALED_PREREG_INVALID")

                # 2d: unlisted sealed path -> SEALED_PREREG_INVALID
                spec_unlisted = WindowSpec.from_mapping(
                    _minimal_spec_dict(["sealed-eval/item.json", "sealed-eval/extra.json"], sealed=sealed_cfg),
                    base_dir=repo,
                )
                with self.assertRaises(Refused) as ctx:
                    stage_inputs(spec_unlisted, root / "b_unlisted.tar.gz")
                self.assertEqual(ctx.exception.code, "SEALED_PREREG_INVALID")

                # 2e: glob in pre-registration paths -> SEALED_PREREG_INVALID
                glob_prereg = repo / "prereg_glob.md"
                glob_bytes = (
                    b"study: study-alpha\n"
                    b"allowed_cards: G4\n"
                    b"allowed_host: colab.google.com\n"
                    b"sealed_path: sealed-eval/*.json\n"
                )
                glob_prereg.write_bytes(glob_bytes)
                _make_readonly(glob_prereg)
                try:
                    spec_glob = WindowSpec.from_mapping(
                        _minimal_spec_dict(
                            ["sealed-eval/item.json"],
                            sealed=dict(sealed_cfg, preregistration_path="prereg_glob.md",
                                        preregistration_sha256=_sha256_bytes(glob_bytes)),
                        ),
                        base_dir=repo,
                    )
                    with self.assertRaises(Refused) as ctx:
                        stage_inputs(spec_glob, root / "b_glob.tar.gz")
                    self.assertEqual(ctx.exception.code, "SEALED_PREREG_INVALID")
                finally:
                    _make_writable(glob_prereg)

                # Item 8: prose-only mention without structured study/cards/host -> SEALED_PREREG_INVALID
                prose_prereg = repo / "prereg_prose.md"
                prose_bytes = (
                    b"# Note\n"
                    b"This disclaimer does not cover study-alpha on G4 at colab.google.com.\n"
                    b"sealed_path: sealed-eval/item.json\n"
                )
                prose_prereg.write_bytes(prose_bytes)
                _make_readonly(prose_prereg)
                try:
                    spec_prose = WindowSpec.from_mapping(
                        _minimal_spec_dict(
                            ["sealed-eval/item.json"],
                            sealed=dict(sealed_cfg, preregistration_path="prereg_prose.md",
                                        preregistration_sha256=_sha256_bytes(prose_bytes)),
                        ),
                        base_dir=repo,
                    )
                    with self.assertRaises(Refused) as ctx:
                        stage_inputs(spec_prose, root / "b_prose.tar.gz")
                    self.assertEqual(ctx.exception.code, "SEALED_PREREG_INVALID")
                finally:
                    _make_writable(prose_prereg)

                other_study_prereg = repo / "prereg_other_study.md"
                other_bytes = (
                    b"study: other-study\n"
                    b"allowed_cards: G4\n"
                    b"allowed_host: colab.google.com\n"
                    b"sealed_path: sealed-eval/item.json\n"
                    b"# Prose mention: not study-alpha\n"
                )
                other_study_prereg.write_bytes(other_bytes)
                _make_readonly(other_study_prereg)
                try:
                    spec_other = WindowSpec.from_mapping(
                        _minimal_spec_dict(
                            ["sealed-eval/item.json"],
                            sealed=dict(sealed_cfg, preregistration_path="prereg_other_study.md",
                                        preregistration_sha256=_sha256_bytes(other_bytes)),
                        ),
                        base_dir=repo,
                    )
                    with self.assertRaises(Refused) as ctx:
                        stage_inputs(spec_other, root / "b_other.tar.gz")
                    self.assertEqual(ctx.exception.code, "SEALED_PREREG_INVALID")
                finally:
                    _make_writable(other_study_prereg)

                # Item 6: pre-registration binds resolved path location, never just relative filename
                (repo / "sealed-a").mkdir()
                sealed_a_item = repo / "sealed-a" / "items.jsonl"
                sealed_a_item.write_text('{"id": 1}\n', encoding="utf-8")
                elsewhere = root / "elsewhere" / "sealed-b-holdout"
                elsewhere.mkdir(parents=True)
                (elsewhere / "items.jsonl").write_text('{"id": 99}\n', encoding="utf-8")
                loc_prereg = repo / "prereg_loc.md"
                loc_bytes = (
                    b"study: study-alpha\n"
                    b"allowed_cards: G4\n"
                    b"allowed_host: colab.google.com\n"
                    b"sealed_path: sealed-a/items.jsonl\n"
                )
                loc_prereg.write_bytes(loc_bytes)
                _make_readonly(loc_prereg)
                try:
                    loc_sealed_cfg = dict(
                        sealed_cfg,
                        preregistration_path="prereg_loc.md",
                        preregistration_sha256=_sha256_bytes(loc_bytes),
                    )
                    spec_ok_loc = WindowSpec.from_mapping(
                        _minimal_spec_dict(["items.jsonl"], root="sealed-a", sealed=loc_sealed_cfg),
                        base_dir=repo,
                    )
                    _, loc_rec = stage_inputs(
                        spec_ok_loc, root / "b_loc_ok.tar.gz", actual_card="G4", live=True, cli_allow_sealed=True
                    )
                    self.assertEqual(loc_rec["resolved_paths"], [str(sealed_a_item.resolve())])
                    self.assertEqual(len(loc_rec["files"]), 1)
                    self.assertEqual(loc_rec["files"][0]["resolved_path"], str(sealed_a_item.resolve()))
                    self.assertEqual(loc_rec["files"][0]["sha256"], _sha256_bytes(sealed_a_item.read_bytes()))

                    spec_bad_loc = WindowSpec.from_mapping(
                        _minimal_spec_dict(["items.jsonl"], root=str(elsewhere), sealed=loc_sealed_cfg),
                        base_dir=repo,
                    )
                    with self.assertRaises(Refused) as ctx:
                        stage_inputs(
                            spec_bad_loc, root / "b_loc_bad.tar.gz", actual_card="G4", live=True, cli_allow_sealed=True
                        )
                    self.assertEqual(ctx.exception.code, "SEALED_PREREG_INVALID")
                finally:
                    _make_writable(loc_prereg)

                # Item 7: directory without any `.git` above the spec treats inputs.root as outside-repo
                no_repo = root / "no_git_dir"
                holdout_dir = no_repo / "holdout-items"
                holdout_dir.mkdir(parents=True)
                (holdout_dir / "items.jsonl").write_text('{"id": 2}\n', encoding="utf-8")
                no_repo_spec = WindowSpec.from_mapping(
                    _minimal_spec_dict(["items.jsonl"], root="./holdout-items"),
                    base_dir=no_repo,
                )
                with self.assertRaises(Refused) as ctx:
                    stage_inputs(no_repo_spec, root / "b_norepo.tar.gz")
                self.assertEqual(ctx.exception.code, "SEALED_DATA_REFUSED")

                # 3a: wrong card destination -> SEALED_DESTINATION_REFUSED
                with self.assertRaises(Refused) as ctx:
                    stage_inputs(spec_valid, root / "b_card.tar.gz", actual_card="T4", live=True, cli_allow_sealed=True)
                self.assertEqual(ctx.exception.code, "SEALED_DESTINATION_REFUSED")

                # 3b: wrong host destination -> SEALED_DESTINATION_REFUSED
                with self.assertRaises(Refused) as ctx:
                    stage_inputs(spec_valid, root / "b_host.tar.gz", actual_card="G4",
                                 actual_host="other.example.com", live=True, cli_allow_sealed=True)
                self.assertEqual(ctx.exception.code, "SEALED_DESTINATION_REFUSED")

                # 4: live run without --allow-sealed -> ALLOW_SEALED_REQUIRED
                with self.assertRaises(Refused) as ctx:
                    stage_inputs(spec_valid, root / "b_flag.tar.gz", actual_card="G4", live=True, cli_allow_sealed=False)
                self.assertEqual(ctx.exception.code, "ALLOW_SEALED_REQUIRED")

                # Happy verified live sealed staging + recorded metadata
                bundle = root / "bundle.tar.gz"
                staged, sealed_rec = stage_inputs(
                    spec_valid, bundle, actual_card="G4", live=True, cli_allow_sealed=True
                )
                self.assertEqual([s.dst for s in staged], ["sealed-eval/item.json"])
                self.assertEqual(sealed_rec["preregistration_sha256"], prereg_sha)
                self.assertEqual(sealed_rec["sealed_files"], ["sealed-eval/item.json"])
                self.assertTrue(sealed_rec["destination"]["verified"])
                with tarfile.open(bundle, "r:gz") as tf:
                    self.assertEqual(tf.getnames(), ["sealed-eval/item.json"])
            finally:
                _make_writable(prereg)

            # Traversal, missing file, bad root, and symlink still refused
            with self.assertRaises(Refused) as ctx:
                WindowSpec.from_mapping(_minimal_spec_dict(["../secret.txt"]), base_dir=repo)
            self.assertEqual(ctx.exception.code, "INPUT_PATH_OUTSIDE_ROOT")

            spec_missing = WindowSpec.from_mapping(_minimal_spec_dict(["does-not-exist.txt"]), base_dir=repo)
            with self.assertRaises(Refused) as ctx:
                stage_inputs(spec_missing, root / "bm.tar.gz")
            self.assertEqual(ctx.exception.code, "INPUT_FILE_MISSING")

            bad_root_data = _minimal_spec_dict(["ok.txt"], root="no-such-dir")
            with self.assertRaises(Refused) as ctx:
                stage_inputs(WindowSpec.from_mapping(bad_root_data, base_dir=repo), root / "br.tar.gz")
            self.assertEqual(ctx.exception.code, "INPUT_ROOT_INVALID")

            sym_spec = WindowSpec.from_mapping(_minimal_spec_dict(["ok.txt"]), base_dir=repo)
            with mock.patch("colab_runner.manifest._is_link", side_effect=[False, True]):
                with self.assertRaises(Refused) as ctx:
                    stage_inputs(sym_spec, root / "bs.tar.gz")
                self.assertEqual(ctx.exception.code, "INPUT_SYMLINK_REFUSED")

            script = render_vm_script(spec_valid, resolve_card("G4"), bundle_filename="b.tar.gz")
            self.assertIn("export OMP_NUM_THREADS=4", script)
            self.assertIn("CR_CARD_MISMATCH:", script)


class AdditionalCardCliRunnerTest(unittest.TestCase):
    def test_pool_keys_ledger_cli_wrappers_and_runner_checks(self):
        self.assertIsNone(pool_key_for_session_row({}))
        self.assertEqual(pool_key_for_session_row({"hardware": "T4", "shape": "Standard"}), "T4")
        self.assertEqual(pool_key_for_session_row({"hardware": "A100", "shape": "Standard"}), "A100_40GB")
        self.assertEqual(pool_key_for_session_row({"hardware": "A100", "shape": "High-RAM"}), "A100_80GB")
        self.assertEqual(pool_key_for_session_row({"hardware": "A100 80GB"}), "A100_80GB")
        self.assertEqual(pool_key_for_session_row({"hardware": "A100 40GB"}), "A100_40GB")
        self.assertIsNone(pool_key_for_session_row({"hardware": "UNKNOWN"}))

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            corrupt = root / "bad.json"
            corrupt.write_text("{bad json", encoding="utf-8")
            self.assertEqual(_load_json_dict(corrupt), {})

            ledger_p = root / "ledger.json"
            ledger = StudyLedger(ledger_p)
            ledger.record_window("", {"card": "G4"})
            corrupt_ledger = root / "corrupt_ledger.json"
            corrupt_ledger.write_text("[1, 2]", encoding="utf-8")
            StudyLedger(corrupt_ledger).record_window(
                "st", {"card_class": "gpu", "opened": True, "card": "L4", "window_id": "w", "session": "s"}
            )
            self.assertEqual(json.loads(corrupt_ledger.read_text(encoding="utf-8"))["studies"]["st"]["card"], "L4")

            # Colab CLI wrappers: CPU, TPU (--tpu), A100_80GB (--high-mem), upload, sessions, restart, usage
            calls: list[list[str]] = []
            cli = Colab(["colab"])
            with mock.patch.object(cli, "run", side_effect=lambda args, timeout=60.0: calls.append(list(args)) or CliResult(0, "")):
                cli.new_session("s1", card="CPU", shape=None, cli_flag="--cpu")
                cli.new_session("s2", card="V5E1", shape="standard", cli_flag="--tpu")
                cli.new_session("s3", card="A100", shape="high-mem", cli_flag="--gpu")
                cli.upload("s1", "a", "/content/a")
                cli.sessions()
                cli.restart_kernel("s1")
                cli.usage()
            self.assertEqual(calls[0], ["new", "--high-mem", "-s", "s1"])
            self.assertEqual(calls[1], ["new", "--tpu", "v5e1", "-s", "s2"])
            self.assertEqual(calls[2], ["new", "--gpu", "A100", "--high-mem", "-s", "s3"])
            self.assertEqual(calls[3][:4], ["upload", "-s", "s1", "a"])
            self.assertEqual(calls[4], ["sessions"])
            self.assertEqual(calls[5], ["restart-kernel", "-s", "s1"])
            self.assertEqual(calls[6], ["usage"])
            self.assertEqual(parse_sessions_output("header without bracket\n"), [])

            # Runner opened_monotonic & pre_opened=True file out_dir refusals
            file_out = root / "existing_file"
            file_out.write_text("x", encoding="utf-8")
            with self.assertRaises(Refused) as ctx:
                Runner(Colab(), "s1", root / "o1").run(script="/content/x.sh", wall_s=60, opened_monotonic=float("nan"))
            self.assertEqual(ctx.exception.code, "SETTING_INVALID")
            with self.assertRaises(Refused) as ctx:
                Runner(Colab(), "s1", file_out, pre_opened=True).run(script="/content/x.sh", wall_s=60)
            self.assertEqual(ctx.exception.code, "OUT_DIR_INVALID")

            # JSON/TOML pre-registration parsers and WindowSpec edge validations
            json_prereg = json.dumps({
                "study": "s1", "allowed_cards": ["G4"], "allowed_host": "colab.google.com", "sealed_paths": ["a/b.txt"]
            }).encode("utf-8")
            self.assertEqual(_parse_preregistration(Path("p.json"), json_prereg)["sealed_paths"], ["a/b.txt"])
            with self.assertRaises(Refused) as ctx:
                _parse_preregistration(Path("p.json"), b"{bad")
            self.assertEqual(ctx.exception.code, "SEALED_PREREG_INVALID")
            toml_prereg = (
                b'study = "s1"\nallowed_cards = ["G4"]\nallowed_host = "colab.google.com"\n'
                b'sealed_paths = ["a/b.txt"]\n'
            )
            self.assertEqual(_parse_preregistration(Path("p.toml"), toml_prereg)["sealed_paths"], ["a/b.txt"])
            with self.assertRaises(Refused) as ctx:
                _parse_preregistration(Path("p.toml"), b"=[")
            self.assertEqual(ctx.exception.code, "SEALED_PREREG_INVALID")

            scalar_vram = _minimal_spec_dict()
            scalar_vram["compute"]["min_vram_mib"] = 16000
            self.assertEqual(WindowSpec.from_mapping(scalar_vram, base_dir=root).vram_floor_for(resolve_card("G4")), 16000)

            arr_json = root / "arr.json"
            arr_json.write_text("[1, 2]", encoding="utf-8")
            with self.assertRaises(Refused) as ctx:
                WindowSpec.from_file(arr_json)
            self.assertEqual(ctx.exception.code, "SPEC_INVALID")

            legacy = _minimal_spec_dict()
            legacy["inputs"]["allow_sealed"] = True
            with self.assertRaises(Refused) as ctx:
                WindowSpec.from_mapping(legacy, base_dir=root)
            self.assertEqual(ctx.exception.code, "SPEC_INVALID")

            no_run = _minimal_spec_dict()
            no_run["job"]["run"] = []
            with self.assertRaises(Refused) as ctx:
                WindowSpec.from_mapping(no_run, base_dir=root)
            self.assertEqual(ctx.exception.code, "SPEC_INVALID")


if __name__ == "__main__":
    unittest.main()

"""Tests for platform card catalog, shape validation, circuit breaker, study ledger, VM card checks, and manifest sandbox."""

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
    resolve_card,
    shape_row_label,
    utc_day_str,
    validate_card_and_shape,
    vm_card_check_py,
)
from colab_runner.errors import Refused
from colab_runner.manifest import WindowSpec, render_vm_script, stage_inputs


def _minimal_spec_dict(files=None) -> dict:
    return {
        "schema_version": 1,
        "project": {"repo": "org/repo", "study": "s1", "job": "job1", "description": "demo"},
        "compute": {
            "card": "G4",
            "shape": "standard",
            "max_rate": 10.5,
            "cap": 8.0,
            "wall_s": 1800,
            "cleanup_bound_s": 600,
        },
        "inputs": {"root": ".", "files": files or []},
        "job": {"run": ["python3 run.py"]},
        "outputs": {"collect": ["out.json"], "salvage": ["setup.log"]},
    }


class CardCatalogTest(unittest.TestCase):
    def test_known_cards_shapes_and_unavailable_card(self):
        self.assertEqual(CARDS["G4"].min_vram_mib, 90000)
        self.assertEqual(shape_row_label("standard"), "Standard")
        self.assertEqual(shape_row_label("high-mem"), "High-RAM")

        spec, shape = validate_card_and_shape("cpu", None)
        self.assertEqual((spec.name, shape), ("CPU", None))

        spec, shape = validate_card_and_shape("a100-80gb", "high-mem")
        self.assertEqual((spec.name, spec.cli_gpu, shape), ("A100_80GB", "A100", "high-mem"))

        bad_cases = [
            ("UNKNOWN_GPU", "standard", "CARD_UNKNOWN"),
            ("H100", "standard", "CARD_UNAVAILABLE"),
            ("CPU", "standard", "SHAPE_NOT_FOR_CPU"),
            ("G4", None, "SHAPE_REQUIRED"),
            ("G4", "ultra", "SHAPE_INVALID"),
            ("L4", "high-mem", "SHAPE_HIGH_MEM_UNAVAILABLE"),
            ("A100_80GB", "standard", "SHAPE_STANDARD_UNAVAILABLE"),
            ("V5E1", "standard", "SHAPE_STANDARD_UNAVAILABLE"),
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
            # Different card or next UTC day remains open for scheduling
            cb.check("L4", "2026-11-26")
            cb.check("G4", "2026-11-27")
            self.assertRegex(utc_day_str(), r"^\d{4}-\d{2}-\d{2}$")

    def test_study_ledger_refuses_card_mixing_within_study(self):
        with tempfile.TemporaryDirectory() as tmp:
            ledger = StudyLedger(Path(tmp) / "ledger.json")
            ledger.record_window("study-a", {
                "window_id": "w1", "session": "s1", "card": "G4", "card_class": "gpu",
                "shape": "standard", "status": "done", "opened": True, "opened_at": "t0",
                "closed_at": "t1", "spend_ccu": 1.0,
            })
            ledger.check_card("study-a", "G4")
            ledger.check_card("study-a", "CPU")  # CPU dry runs never lock GPU card
            with self.assertRaises(Refused) as ctx:
                ledger.check_card("study-a", "A100")
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

            # Wrong card / low VRAM -> exits CARD_CHECK_FAIL_RC (42)
            smi.write_text("#!/bin/sh\necho 'Tesla T4, 15360, 7.5'\n", encoding="utf-8")
            bad_proc = subprocess.run([sys.executable, "-c", py_src], env=env, capture_output=True, text=True,
                                      stdin=subprocess.DEVNULL, timeout=30)
            self.assertEqual(bad_proc.returncode, CARD_CHECK_FAIL_RC)
            self.assertIn("CR_CARD_MISMATCH:", bad_proc.stdout)
            card_rec = json.loads((out_dir / "vm_card.json").read_text())
            self.assertFalse(card_rec["ok"])


class ManifestSandboxTest(unittest.TestCase):
    def test_toml_and_json_loading_and_validation_errors(self):
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

            good_toml = root / "good.toml"
            good_toml.write_text(
                'schema_version = 1\n'
                '[project]\nrepo = "o/r"\nstudy = "s"\njob = "j"\n'
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

    def test_stage_inputs_enforces_sandbox_and_sealed_data_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            proj = root / "proj"
            proj.mkdir()
            outside = root / "secret.txt"
            outside.write_text("top secret\n", encoding="utf-8")
            (proj / "ok.txt").write_text("clean\n", encoding="utf-8")
            (proj / "sealed-eval").mkdir()
            (proj / "sealed-eval" / "item.json").write_text("{}\n", encoding="utf-8")

            # Traversal outside root refused at spec parse time
            with self.assertRaises(Refused) as ctx:
                WindowSpec.from_mapping(_minimal_spec_dict(["../secret.txt"]), base_dir=proj)
            self.assertEqual(ctx.exception.code, "INPUT_PATH_OUTSIDE_ROOT")

            # Missing file refused
            spec_missing = WindowSpec.from_mapping(_minimal_spec_dict(["does-not-exist.txt"]), base_dir=proj)
            with self.assertRaises(Refused) as ctx:
                stage_inputs(spec_missing, root / "b1.tar.gz")
            self.assertEqual(ctx.exception.code, "INPUT_FILE_MISSING")

            # Missing inputs.root refused
            bad_root_data = _minimal_spec_dict(["ok.txt"])
            bad_root_data["inputs"]["root"] = "no-such-dir"
            spec_bad_root = WindowSpec.from_mapping(bad_root_data, base_dir=proj)
            with self.assertRaises(Refused) as ctx:
                stage_inputs(spec_bad_root, root / "b2.tar.gz")
            self.assertEqual(ctx.exception.code, "INPUT_ROOT_INVALID")

            # Sealed path refused without allow_sealed + preregistration
            spec_sealed = WindowSpec.from_mapping(_minimal_spec_dict(["sealed-eval/item.json"]), base_dir=proj)
            with self.assertRaises(Refused) as ctx:
                stage_inputs(spec_sealed, root / "b3.tar.gz")
            self.assertEqual(ctx.exception.code, "SEALED_DATA_REFUSED")

            # Allowed when allow_sealed=True AND preregistration non-empty
            allowed_data = _minimal_spec_dict(["sealed-eval/item.json", "ok.txt"])
            allowed_data["inputs"]["allow_sealed"] = True
            allowed_data["inputs"]["preregistration"] = "prereg-2026-11-26-v1"
            spec_allowed = WindowSpec.from_mapping(allowed_data, base_dir=proj)
            bundle = root / "bundle.tar.gz"
            staged = stage_inputs(spec_allowed, bundle)
            self.assertEqual([s.dst for s in staged], ["sealed-eval/item.json", "ok.txt"])
            with tarfile.open(bundle, "r:gz") as tf:
                self.assertEqual(sorted(tf.getnames()), ["ok.txt", "sealed-eval/item.json"])

            # Symlink refused (mocked cleanly if OS restricts symlink creation)
            sym_spec = WindowSpec.from_mapping(_minimal_spec_dict(["ok.txt"]), base_dir=proj)
            with mock.patch("colab_runner.manifest._is_link", side_effect=[False, True]):
                with self.assertRaises(Refused) as ctx:
                    stage_inputs(sym_spec, root / "b4.tar.gz")
                self.assertEqual(ctx.exception.code, "INPUT_SYMLINK_REFUSED")

            script = render_vm_script(spec_allowed, resolve_card("G4"), bundle_filename="b.tar.gz")
            self.assertIn("export OMP_NUM_THREADS=4", script)
            self.assertIn("CR_CARD_MISMATCH:", script)


if __name__ == "__main__":
    unittest.main()

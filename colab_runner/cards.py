"""Platform card catalog, shape validation, per-card circuit breaker, study ledger, and VM GPU assertions.

Measured Colab Ultimate plan pool facts (dated 2026-11):
- Total active sessions across the account: 20.
- Per-accelerator pool ceilings: 3 each for A100 (40 GB / 80 GB tiers), G4 (RTX PRO 6000 96 GB), L4, and T4; 5 for CPU.
- H100 is unavailable on the plan (refused up front with CARD_UNAVAILABLE).
- Shape rules: CPU always opens with `--high-mem` and takes no `--shape`; L4 is Standard-only; V5E1/V6E1 are
  High-RAM-only; G4, T4, and A100 require an explicit `standard` or `high-mem` shape (there is no silent default).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping, Optional

from colab_runner.errors import Refused
from colab_runner.runner import write_json_atomic

PLATFORM_FACTS_UPDATED = "2026-11-26"
TOTAL_SESSION_CEILING = 20
DEFAULT_CIRCUIT_BREAKER_THRESHOLD = 2
CARD_CHECK_FAIL_RC = 42


@dataclass(frozen=True)
class CardSpec:
    """Platform specifications and guard thresholds for one Colab runtime card type."""

    name: str
    cli_gpu: str
    card_class: str  # "cpu" | "gpu"
    available: bool
    pool_ceiling: int
    shapes: tuple[str, ...]
    default_rate: Optional[float]
    min_vram_mib: int
    min_compute_cap: tuple[int, int]
    name_tokens: tuple[str, ...]


CARDS: dict[str, CardSpec] = {
    "CPU": CardSpec(
        name="CPU",
        cli_gpu="CPU",
        card_class="cpu",
        available=True,
        pool_ceiling=5,
        shapes=(),
        default_rate=0.26,
        min_vram_mib=0,
        min_compute_cap=(0, 0),
        name_tokens=(),
    ),
    "T4": CardSpec(
        name="T4",
        cli_gpu="T4",
        card_class="gpu",
        available=True,
        pool_ceiling=3,
        shapes=("standard", "high-mem"),
        default_rate=None,
        min_vram_mib=14000,
        min_compute_cap=(7, 5),
        name_tokens=("T4",),
    ),
    "L4": CardSpec(
        name="L4",
        cli_gpu="L4",
        card_class="gpu",
        available=True,
        pool_ceiling=3,
        shapes=("standard",),
        default_rate=None,
        min_vram_mib=21500,
        min_compute_cap=(8, 9),
        name_tokens=("L4",),
    ),
    "G4": CardSpec(
        name="G4",
        cli_gpu="G4",
        card_class="gpu",
        available=True,
        pool_ceiling=3,
        shapes=("standard", "high-mem"),
        default_rate=None,
        min_vram_mib=90000,
        min_compute_cap=(8, 9),
        name_tokens=("RTX PRO 6000", "G4"),
    ),
    "A100": CardSpec(
        name="A100",
        cli_gpu="A100",
        card_class="gpu",
        available=True,
        pool_ceiling=3,
        shapes=("standard", "high-mem"),
        default_rate=None,
        min_vram_mib=38000,
        min_compute_cap=(8, 0),
        name_tokens=("A100",),
    ),
    "A100_80GB": CardSpec(
        name="A100_80GB",
        cli_gpu="A100",
        card_class="gpu",
        available=True,
        pool_ceiling=3,
        shapes=("high-mem",),
        default_rate=None,
        min_vram_mib=76000,
        min_compute_cap=(8, 0),
        name_tokens=("A100",),
    ),
    "V5E1": CardSpec(
        name="V5E1",
        cli_gpu="V5E1",
        card_class="gpu",
        available=True,
        pool_ceiling=1,
        shapes=("high-mem",),
        default_rate=None,
        min_vram_mib=0,
        min_compute_cap=(0, 0),
        name_tokens=(),
    ),
    "V6E1": CardSpec(
        name="V6E1",
        cli_gpu="V6E1",
        card_class="gpu",
        available=True,
        pool_ceiling=1,
        shapes=("high-mem",),
        default_rate=None,
        min_vram_mib=0,
        min_compute_cap=(0, 0),
        name_tokens=(),
    ),
    "H100": CardSpec(
        name="H100",
        cli_gpu="H100",
        card_class="gpu",
        available=False,
        pool_ceiling=0,
        shapes=("standard", "high-mem"),
        default_rate=None,
        min_vram_mib=76000,
        min_compute_cap=(9, 0),
        name_tokens=("H100",),
    ),
}

_CARD_ALIASES = {
    "A100-80GB": "A100_80GB",
    "A100 80GB": "A100_80GB",
    "A100_40GB": "A100",
    "A100-40GB": "A100",
}


def resolve_card(card: str) -> CardSpec:
    """Resolve and validate a card name against the catalog, refusing unknown or unavailable cards."""
    key = (card or "").strip().upper()
    key = _CARD_ALIASES.get(key, key)
    spec = CARDS.get(key)
    if spec is None:
        raise Refused("CARD_UNKNOWN", "unknown Colab card %r" % (card,))
    if not spec.available:
        raise Refused("CARD_UNAVAILABLE", "card %s is currently unavailable on the Colab plan" % spec.name)
    return spec


def validate_card_and_shape(card: str, shape: Optional[str]) -> tuple[CardSpec, Optional[str]]:
    """Validate card availability and machine shape compatibility before any `colab` call."""
    spec = resolve_card(card)
    clean_shape = shape.strip().lower() if isinstance(shape, str) and shape.strip() else None
    if spec.card_class == "cpu":
        if clean_shape is not None:
            raise Refused(
                "SHAPE_NOT_FOR_CPU",
                "CPU card always opens with --high-mem and does not accept shape=%r" % clean_shape,
            )
        return spec, None
    if clean_shape is None:
        raise Refused(
            "SHAPE_REQUIRED",
            "GPU card %s requires an explicit shape ('standard' or 'high-mem')" % spec.name,
        )
    if clean_shape not in ("standard", "high-mem"):
        raise Refused("SHAPE_INVALID", "invalid shape %r: expected 'standard' or 'high-mem'" % clean_shape)
    if clean_shape == "high-mem" and spec.shapes == ("standard",):
        raise Refused(
            "SHAPE_HIGH_MEM_UNAVAILABLE",
            "%s only opens in Standard shape on Colab; pass shape='standard'" % spec.name,
        )
    if clean_shape == "standard" and spec.shapes == ("high-mem",):
        raise Refused(
            "SHAPE_STANDARD_UNAVAILABLE",
            "%s only opens in High-RAM shape on Colab; pass shape='high-mem'" % spec.name,
        )
    return spec, clean_shape


def shape_row_label(shape: str) -> str:
    """Return the `colab sessions` Shape column label expected for a requested shape."""
    return "High-RAM" if shape == "high-mem" else "Standard"


def utc_day_str(now: Optional[datetime] = None) -> str:
    """UTC calendar day string `YYYY-MM-DD` used to scope per-card circuit-breaker refusals."""
    dt = now or datetime.now(timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%d")


def _load_json_dict(path: Path) -> dict[str, Any]:
    if not path.is_file():
        return {}
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


class CircuitBreaker:
    """Per-card UTC-day refusal ledger that opens the circuit after `threshold` allocation/card failures."""

    def __init__(self, state_file: Path, *, threshold: int = DEFAULT_CIRCUIT_BREAKER_THRESHOLD):
        self.state_file = Path(state_file)
        self.threshold = int(threshold)

    def events_for(self, card: str, day: Optional[str] = None) -> list[dict[str, Any]]:
        target_day = day or utc_day_str()
        data = _load_json_dict(self.state_file)
        days = data.get("days", {})
        day_bucket = days.get(target_day, {}) if isinstance(days, dict) else {}
        events = day_bucket.get(card.upper(), []) if isinstance(day_bucket, dict) else []
        return list(events) if isinstance(events, list) else []

    def check(self, card: str, day: Optional[str] = None) -> None:
        """Raise CARD_CIRCUIT_OPEN if `card` already hit `threshold` allocation/card refusals today (UTC)."""
        target_day = day or utc_day_str()
        events = self.events_for(card, target_day)
        if len(events) >= self.threshold:
            raise Refused(
                "CARD_CIRCUIT_OPEN",
                "circuit breaker open for %s on %s UTC (%d refusals reached threshold %d)"
                % (card.upper(), target_day, len(events), self.threshold),
            )

    def record_refusal(
        self,
        card: str,
        *,
        reason: str,
        session: str,
        day: Optional[str] = None,
        at: Optional[str] = None,
    ) -> int:
        """Record an allocation or card mismatch failure for `card` on `day` (UTC) and return the new count."""
        target_day = day or utc_day_str()
        data = _load_json_dict(self.state_file)
        days = data.setdefault("days", {})
        if not isinstance(days, dict):
            days = {}
            data["days"] = days
        bucket = days.setdefault(target_day, {})
        if not isinstance(bucket, dict):
            bucket = {}
            days[target_day] = bucket
        events = bucket.setdefault(card.upper(), [])
        if not isinstance(events, list):
            events = []
            bucket[card.upper()] = events
        events.append({
            "at": at or datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "reason": reason,
            "session": session,
        })
        write_json_atomic(self.state_file, data)
        return len(events)


class StudyLedger:
    """Records per-study GPU card commitments and run outcomes so one study never mixes cards (e.g. G4 and A100)."""

    def __init__(self, ledger_file: Path):
        self.ledger_file = Path(ledger_file)

    def check_card(self, study: str, card: str) -> None:
        """Refuse switching or pooling GPU cards within one study (CPU runs never lock a GPU card)."""
        spec = resolve_card(card)
        if spec.card_class == "cpu" or not study:
            return
        data = _load_json_dict(self.ledger_file)
        studies = data.get("studies", {})
        entry = studies.get(study) if isinstance(studies, dict) else None
        if isinstance(entry, dict) and entry.get("card") and entry["card"] != spec.name:
            raise Refused(
                "CARD_MIX_REFUSED",
                "study %r is already locked to %s; cannot run on %s without starting a separate study"
                % (study, entry["card"], spec.name),
            )

    def record_window(self, study: str, outcome: Mapping[str, Any]) -> None:
        """Append a finished/closed window summary to the study ledger and lock its GPU card if applicable."""
        if not study:
            return
        data = _load_json_dict(self.ledger_file)
        studies = data.setdefault("studies", {})
        if not isinstance(studies, dict):
            studies = {}
            data["studies"] = studies
        entry = studies.setdefault(study, {"card": None, "windows": []})
        if outcome.get("card_class") == "gpu" and outcome.get("opened") and not entry.get("card"):
            entry["card"] = outcome.get("card")
        windows = entry.setdefault("windows", [])
        windows.append({
            "window_id": outcome.get("window_id"),
            "session": outcome.get("session"),
            "card": outcome.get("card"),
            "shape": outcome.get("shape"),
            "status": outcome.get("status"),
            "opened_at": outcome.get("opened_at"),
            "closed_at": outcome.get("closed_at"),
            "spend_ccu": outcome.get("spend_ccu"),
        })
        write_json_atomic(self.ledger_file, data)


def vm_card_check_py(spec: CardSpec, *, min_vram_mib: Optional[int] = None) -> str:
    """Return a self-contained stdlib Python snippet that verifies GPU name, VRAM, and compute capability on the VM."""
    vram_floor = int(spec.min_vram_mib if min_vram_mib is None else min_vram_mib)
    min_cc = list(spec.min_compute_cap)
    tokens = list(spec.name_tokens)
    return (
        "import json, os, shutil, subprocess, sys\n"
        "from pathlib import Path\n"
        f"want_card = {spec.name!r}\n"
        f"tokens = {tokens!r}\n"
        f"min_vram = {vram_floor!r}\n"
        f"min_cc = tuple({min_cc!r})\n"
        "out_dir = Path(os.environ.get('CR_OUT_DIR') or '.')\n"
        "out_dir.mkdir(parents=True, exist_ok=True)\n"
        "smi = os.environ.get('CR_NVIDIA_SMI') or shutil.which('nvidia-smi')\n"
        "rec = {'requested_card': want_card, 'ok': False, 'reason': '', 'gpu_name': None, "
        "'vram_mib': None, 'compute_cap': None}\n"
        "def _fail(msg):\n"
        "    rec['reason'] = msg\n"
        "    (out_dir / 'vm_card.json').write_text(json.dumps(rec, indent=2) + '\\n', encoding='utf-8')\n"
        "    print('CR_CARD_MISMATCH: ' + msg, flush=True)\n"
        f"    raise SystemExit({CARD_CHECK_FAIL_RC})\n"
        "if not smi:\n"
        "    _fail('nvidia-smi not found on VM for requested GPU ' + want_card)\n"
        "proc = subprocess.run([smi, '--query-gpu=name,memory.total,compute_cap', '--format=csv,noheader,nounits'], "
        "capture_output=True, text=True)\n"
        "if proc.returncode != 0 or not proc.stdout.strip():\n"
        "    _fail('nvidia-smi failed: ' + (proc.stderr or proc.stdout).strip())\n"
        "first = proc.stdout.strip().splitlines()[0]\n"
        "parts = [p.strip() for p in first.split(',')]\n"
        "if len(parts) < 3:\n"
        "    _fail('unexpected nvidia-smi row: ' + first)\n"
        "name = parts[0]\n"
        "try:\n"
        "    vram = int(float(parts[1]))\n"
        "    cc_parts = parts[2].split('.')\n"
        "    cc = (int(cc_parts[0]), int(cc_parts[1]) if len(cc_parts) > 1 else 0)\n"
        "except ValueError:\n"
        "    _fail('unparseable nvidia-smi metrics: ' + first)\n"
        "rec['gpu_name'] = name\n"
        "rec['vram_mib'] = vram\n"
        "rec['compute_cap'] = parts[2]\n"
        "if tokens and not any(t.upper() in name.upper() for t in tokens):\n"
        "    _fail(f'expected GPU matching {tokens} for {want_card}, got {name!r}')\n"
        "if vram < min_vram:\n"
        "    _fail(f'VRAM {vram} MiB below minimum {min_vram} MiB for {want_card} ({name})')\n"
        "if cc < min_cc:\n"
        "    _fail(f'compute capability {parts[2]} below minimum {min_cc[0]}.{min_cc[1]} for {want_card}')\n"
        "rec['ok'] = True\n"
        "(out_dir / 'vm_card.json').write_text(json.dumps(rec, indent=2) + '\\n', encoding='utf-8')\n"
        "print(f'CR_CARD_OK: {want_card} -> {name} ({vram} MiB, cc={parts[2]})', flush=True)\n"
    )


__all__ = [
    "CARDS",
    "CARD_CHECK_FAIL_RC",
    "CardSpec",
    "CircuitBreaker",
    "DEFAULT_CIRCUIT_BREAKER_THRESHOLD",
    "PLATFORM_FACTS_UPDATED",
    "StudyLedger",
    "TOTAL_SESSION_CEILING",
    "resolve_card",
    "shape_row_label",
    "utc_day_str",
    "validate_card_and_shape",
    "vm_card_check_py",
]

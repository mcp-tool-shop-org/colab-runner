"""Declarative window launcher: plan (`--dry`), local CPU dry-run (`--local-cpu`), live launch, booking `outcome.json`, and `status`."""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional, Sequence

from colab_runner.cards import (
    CARD_CHECK_FAIL_RC,
    TOTAL_SESSION_CEILING,
    CardSpec,
    CircuitBreaker,
    StudyLedger,
    shape_row_label,
    utc_day_str,
    validate_card_and_shape,
)
from colab_runner.colab_cli import (
    BackgroundCall,
    BinaryDownloadRefused,
    CliResult,
    Colab,
    QuarantineEscape,
    cli_from_env,
    default_quarantine_dir,
    parse_sessions_output,
    scrub,
    scrub_local,
    write_cell,
)
from colab_runner.errors import Refused, error_record
from colab_runner.manifest import StagedInputRecord, WindowSpec, render_vm_script, stage_inputs
from colab_runner.runner import Runner, require_number, write_json_atomic
from colab_runner.spend import SpendGuard, neighbour_warning
from colab_runner.window_lib import launch_guards as lg

ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
DEFAULT_SALVAGE_ALLOWANCE_S = 120.0
DEFAULT_CLEANUP_FLOOR_S = 600.0


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9-]+", "-", (value or "win").strip().lower()).strip("-")
    return cleaned[:24] or "win"


def default_session_name(spec: WindowSpec, card: str) -> str:
    """Build a unique Colab session name from the study, card, and a short random suffix."""
    return "cr-%s-%s-%s" % (_slug(spec.job), card.lower().replace("_", "-"), uuid.uuid4().hex[:6])


def resolve_kit_env(items: Sequence[str], environ: Mapping[str, str] | None = None) -> dict[str, str]:
    """Resolve `NAME=VALUE` or `NAME` environment declarations; refuse malformed or unset names."""
    source = os.environ if environ is None else environ
    out: dict[str, str] = {}
    for item in items:
        name, eq, value = item.partition("=")
        if not ENV_NAME_RE.fullmatch(name):
            raise Refused("ENV_INVALID", "--env needs NAME=VALUE or NAME, with NAME a variable name")
        if not eq:
            if name not in source:
                raise Refused("ENV_UNSET", "--env %s names a variable that is not set in this environment" % name)
            value = source[name]
        out[name] = value
    return out


def default_cleanup_bound_s(resident: bool, runner_cls: type[Runner] = Runner) -> float:
    """Compute a safe default `--cleanup-bound-s` clearing `Runner.own_cleanup_bound_s` plus salvage allowance."""
    probe = runner_cls(Colab(), "probe", Path.cwd() / "probe_out")
    return max(DEFAULT_CLEANUP_FLOOR_S, probe.own_cleanup_bound_s(resident) + DEFAULT_SALVAGE_ALLOWANCE_S)


def _check_empty_out_dir(out_dir: Path) -> Path:
    default_quarantine_dir(out_dir)  # validates non-root resolved path -> OUT_DIR_INVALID
    try:
        if out_dir.exists() and not out_dir.is_dir():
            raise Refused("OUT_DIR_INVALID", "%s exists and is not a directory" % out_dir)
        if out_dir.exists() and any(out_dir.iterdir()):
            raise Refused("OUT_DIR_NOT_EMPTY", "%s already exists and is not an empty directory" % out_dir)
    except OSError as exc:
        raise Refused("OUT_DIR_INVALID", "%s cannot be inspected (%s: %s)" % (out_dir, type(exc).__name__, exc)) from None
    return out_dir


def _state_paths(state_dir: Optional[Path | str], study_ledger: Optional[Path | str]) -> tuple[Optional[Path], Optional[Path]]:
    raw_state = state_dir or os.environ.get("COLAB_RUNNER_STATE_DIR")
    s_dir = Path(raw_state) if raw_state else None
    l_file = Path(study_ledger) if study_ledger else (s_dir / "study_ledger.json" if s_dir is not None else None)
    return s_dir, l_file


@contextlib.contextmanager
def _open_lock(
    lock_file: Path,
    *,
    timeout_s: float = 1200.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
):
    """Cross-process lock serializing pre-open usage attribution, `colab new`, shape check, and initial rate poll."""
    lock_file.parent.mkdir(parents=True, exist_ok=True)
    if os.name == "posix":
        import fcntl

        with lock_file.open("a+", encoding="utf-8") as fh:
            deadline = clock() + max(0.0, float(timeout_s))
            while True:
                try:
                    fcntl.flock(fh.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if clock() >= deadline:
                        raise Refused(
                            "OPEN_LOCK_TIMEOUT",
                            "could not acquire open lock %s within %.0fs" % (lock_file, timeout_s),
                        ) from None
                    sleep(min(0.1, max(0.0, deadline - clock())))
            try:
                yield
            finally:
                fcntl.flock(fh.fileno(), fcntl.LOCK_UN)
    else:  # pragma: no cover - Windows atomic mkdir fallback; CI runs Linux
        lock_dir = lock_file.with_suffix(".lockdir")
        deadline = clock() + max(0.0, float(timeout_s))
        while True:
            try:
                lock_dir.mkdir(parents=False)
                break
            except FileExistsError:
                if clock() >= deadline:
                    raise Refused(
                        "OPEN_LOCK_TIMEOUT",
                        "could not acquire open lock %s within %.0fs" % (lock_dir, timeout_s),
                    ) from None
                sleep(min(0.1, max(0.0, deadline - clock())))
        try:
            yield
        finally:
            try:
                lock_dir.rmdir()
            except OSError:
                pass


class LocalColab(Colab):
    """Offline directory-backed `Colab` stand-in for `--local-cpu` dry runs (never touches Google Colab)."""

    def __init__(self, content_dir: Path, **kw):
        super().__init__(("local-cpu",), **kw)
        self.content_dir = Path(content_dir)
        self.content_dir.mkdir(parents=True, exist_ok=True)
        self.active_sessions: dict[str, dict[str, str]] = {}
        self.confirmed_stops: list[str] = []

    def _translate_remote(self, remote: str) -> Path:
        norm = remote.replace("\\", "/").strip()
        if norm == "/content":
            return self.content_dir
        if norm.startswith("/content/"):
            rel = norm[len("/content/"):]
            dest = (self.content_dir / rel).resolve()
            if not dest.is_relative_to(self.content_dir.resolve()):
                raise ValueError("remote path escapes /content: %s" % remote)
            return dest
        dest = (self.content_dir / norm.lstrip("/")).resolve()
        if not dest.is_relative_to(self.content_dir.resolve()):
            raise ValueError("remote path escapes content root: %s" % remote)
        return dest

    def _cell_env(self, env: Mapping[str, str] | None) -> dict[str, str]:
        full = dict(env or {})
        full["CR_CONTENT"] = str(self.content_dir)
        job = full.get("CR_JOB", "job")
        if not full.get("CR_OUT_DIR"):
            full["CR_OUT_DIR"] = str(self.content_dir / (job + "_out"))
        script = full.get("CR_SCRIPT", "")
        if script.startswith("/content/"):
            full["CR_SCRIPT"] = str(self._translate_remote(script))
        full.setdefault("CR_DRY_RUN", "1")
        full.setdefault("CR_CARD_CLASS", "cpu")
        return full

    def sessions(self, timeout: float = 30.0) -> CliResult:
        lines = [
            "[%s] Card: %s | Shape: %s" % (name, info["card"], info["shape"])
            for name, info in sorted(self.active_sessions.items())
        ]
        return CliResult(0, "\n".join(lines) + ("\n" if lines else ""))

    def new_session(self, session: str, *, card: str, shape: Optional[str] = None,
                    timeout: float = 180.0) -> CliResult:
        label = "High-RAM" if (card.upper() == "CPU" or shape == "high-mem") else "Standard"
        self.active_sessions[session] = {"card": card.upper(), "shape": label}
        return CliResult(0, "Started session %s (%s)\n" % (session, label))

    def upload(self, session: str, local: str, remote: str, timeout: float = 120.0) -> CliResult:
        if session not in self.active_sessions:
            return CliResult(1, "", "session %s not open" % session)
        dest = self._translate_remote(remote)
        if dest.parent != self.content_dir.resolve():
            return CliResult(1, "", "500 Server Error: parent directory does not exist for %s" % remote)
        shutil.copyfile(local, dest)
        return CliResult(0, "Uploaded %s\n" % remote)

    def exec_cell(self, session: str, source: str, env: Mapping[str, str] | None = None,
                  timeout: float = 60.0) -> CliResult:
        if session not in self.active_sessions:
            return CliResult(1, "", "session %s not open" % session)
        merged = self._cell_env(env)
        with tempfile.TemporaryDirectory(prefix="colab-local-cell-") as tmp:
            cell = write_cell(Path(tmp), source, merged)
            saved_argv = self.argv
            self.argv = [sys.executable]
            try:
                return self.run([str(cell)], timeout)
            finally:
                self.argv = saved_argv

    def start_cell(self, session: str, source: str, env: Mapping[str, str] | None = None,
                   timeout: float = 60.0) -> BackgroundCall:
        merged = self._cell_env(env)
        saved_argv = self.argv
        self.argv = [sys.executable]
        try:
            tmp = tempfile.TemporaryDirectory(prefix="colab-local-bg-")
            cell = write_cell(Path(tmp.name), source, merged)
            out_fh = (Path(tmp.name) / "out.txt").open("w", encoding="utf-8")
            err_fh = (Path(tmp.name) / "err.txt").open("w", encoding="utf-8")
            try:
                kwargs = {"start_new_session": True} if os.name == "posix" else {}
                proc = subprocess.Popen(
                    [sys.executable, str(cell)],
                    stdin=subprocess.DEVNULL, stdout=out_fh, stderr=err_fh, env=self.env, **kwargs,
                )
            finally:
                out_fh.close()
                err_fh.close()
            return BackgroundCall(proc, tmp, time.monotonic() + max(0.0, float(timeout)))
        finally:
            self.argv = saved_argv

    def download(self, session: str, remote: str, local: str, timeout: float = 120.0) -> CliResult:
        try:
            src = self._translate_remote(remote)
            if not src.exists():
                return CliResult(1, "", "no such file: %s" % remote)
            dst = Path(local)
            if src.is_dir():
                shutil.copytree(src, dst, dirs_exist_ok=True)
            else:
                dst.parent.mkdir(parents=True, exist_ok=True)
                shutil.copyfile(src, dst)
            return CliResult(0, "")
        finally:
            scrub_local(
                local, quarantine_dir=self.quarantine_dir, base_dir=self._base_dir,
                log=self.log, on_quarantine=self.on_quarantine,
            )

    def stop(self, session: str, timeout: float = 120.0) -> CliResult:
        self.active_sessions.pop(session, None)
        self.confirmed_stops.append(session)
        return CliResult(0, "Stopped %s\n" % session)

    def restart_kernel(self, session: str, timeout: float = 180.0) -> CliResult:
        return CliResult(0, "restarted\n")

    def usage(self, timeout: float = 60.0) -> CliResult:
        return CliResult(
            0,
            "Current balance: 100.00 compute units\nUsage rate: 0.26 / hr\nActive assignments: %d\n"
            % len(self.active_sessions),
        )


def _apply_overrides(
    spec: WindowSpec,
    *,
    card: Optional[str] = None,
    shape: Optional[str] = None,
    cap: Optional[float] = None,
    max_rate: Optional[float] = None,
    wall_s: Optional[float] = None,
    cleanup_bound_s: Optional[float] = None,
    margin: Optional[float] = None,
    resident: Optional[bool] = None,
    local_cpu: bool = False,
) -> tuple[CardSpec, Optional[str], float, float, float, float, float, bool]:
    eff_card_name = "CPU" if local_cpu else (card or spec.card)
    if local_cpu:
        eff_shape_in = None
    elif card is not None and card.strip().upper() == "CPU":
        eff_shape_in = None
    else:
        eff_shape_in = shape if shape is not None else spec.shape

    card_spec, clean_shape = validate_card_and_shape(eff_card_name, eff_shape_in)

    eff_max_rate = (card_spec.default_rate if (local_cpu and max_rate is None and card_spec.default_rate is not None)
                    else (max_rate if max_rate is not None else spec.max_rate))
    if eff_max_rate is None:
        eff_max_rate = card_spec.default_rate
    require_number("MAX_RATE_INVALID", "max_rate", eff_max_rate)

    eff_cap = cap if cap is not None else spec.cap
    require_number("CAP_INVALID", "cap", eff_cap)

    eff_wall_s = wall_s if wall_s is not None else spec.wall_s
    require_number("WALL_INVALID", "wall_s", eff_wall_s)

    eff_margin = margin if margin is not None else spec.margin
    require_number("MARGIN_INVALID", "margin", eff_margin, inclusive=True)

    eff_resident = spec.resident if resident is None else bool(resident)
    eff_cleanup = (cleanup_bound_s if cleanup_bound_s is not None
                   else (spec.cleanup_bound_s if spec.cleanup_bound_s is not None
                         else default_cleanup_bound_s(eff_resident)))
    require_number("CLEANUP_BOUND_INVALID", "cleanup_bound_s", eff_cleanup, inclusive=True)

    return (
        card_spec, clean_shape, float(eff_max_rate), float(eff_cap), float(eff_wall_s),
        float(eff_margin), float(eff_cleanup), eff_resident,
    )


def _base_outcome(
    *,
    window_id: str,
    session: str,
    spec: WindowSpec,
    card_spec: CardSpec,
    shape: Optional[str],
    mode: str,
    cap_ccu: float,
    margin_ccu: float,
    cleanup_bound_s: float,
    max_rate: float,
    staged: Sequence[StagedInputRecord],
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "window_id": window_id,
        "session": session,
        "project": {
            "repo": spec.repo,
            "study": spec.study,
            "job": spec.job,
            "description": spec.description,
        },
        "card": card_spec.name,
        "card_class": card_spec.card_class,
        "shape": shape,
        "mode": mode,
        "status": "unknown",
        "exit_code": 2,
        "run_rc": None,
        "opened": False,
        "opened_at": None,
        "closed_at": None,
        "elapsed_s": 0.0,
        "cap_ccu": cap_ccu,
        "margin_ccu": margin_ccu,
        "cleanup_bound_s": cleanup_bound_s,
        "max_rate": max_rate,
        "priced_rate": max_rate,
        "window_rate_stable": None,
        "spend_basis": "not_opened",
        "spend_ccu": 0.0,
        "account_balance": {"start": None, "end": None, "drop": None, "used_for_pricing": False},
        "session_stopped_confirmed": False,
        "session_gone_confirmed": None,
        "refusal": None,
        "cap_trip": None,
        "input_files": [asdict(item) for item in staged],
        "collected_outputs": [],
        "salvaged_outputs": [],
        "quarantined": [],
    }


def _download_declared_outputs(
    runner: Runner,
    paths: Sequence[str],
    dest_dir: Path,
    *,
    content: str,
    job: str,
) -> list[str]:
    transferred: list[str] = []
    work_prefix = "%s/%s_work" % (content.rstrip("/"), job)
    out_prefix = "%s/%s_out" % (content.rstrip("/"), job)
    for rel in paths:
        local = dest_dir / rel
        local.parent.mkdir(parents=True, exist_ok=True)
        res = runner.download("%s/%s" % (work_prefix, rel), local)
        if not res.ok:
            res = runner.download("%s/%s" % (out_prefix, rel), local)
        if res.ok and local.exists():
            transferred.append(rel)
    return transferred


def _card_mismatch_seen(out_dir: Path, job: str) -> bool:
    for p in (out_dir / ("%s.run.out" % job), out_dir / "collected" / "setup.log", out_dir / "salvage" / "setup.log"):
        if p.is_file() and "CR_CARD_MISMATCH:" in p.read_text(encoding="utf-8", errors="replace"):
            return True
    for p in (out_dir / "salvage" / "vm_card.json", out_dir / "collected" / "vm_card.json"):
        if p.is_file() and '"ok": false' in p.read_text(encoding="utf-8", errors="replace").lower():
            return True
    return False


def _book_spend(
    outcome: dict[str, Any],
    *,
    card_spec: CardSpec,
    guard: SpendGuard,
    elapsed_s: float,
    bal_end: Optional[float],
) -> None:
    priced = round(float(guard.priced_rate()), 6)
    stable = guard.state.get("best_stable")
    outcome["priced_rate"] = priced
    outcome["window_rate_stable"] = stable
    outcome["elapsed_s"] = round(max(0.0, float(elapsed_s)), 3)
    if card_spec.card_class == "cpu":
        outcome["spend_basis"] = "cpu_configured_rate_x_elapsed"
    elif stable is not None:
        outcome["spend_basis"] = "window_rate_x_elapsed"
    else:
        outcome["spend_basis"] = "max_rate_x_elapsed_no_stable_rate"
    outcome["spend_ccu"] = round((priced * max(0.0, float(elapsed_s))) / 3600.0, 6)
    bal_start = outcome["account_balance"].get("start")
    drop = round(bal_start - bal_end, 6) if (bal_start is not None and bal_end is not None) else None
    outcome["account_balance"] = {"start": bal_start, "end": bal_end, "drop": drop, "used_for_pricing": False}


def _pool_check(rows: Sequence[Mapping[str, Optional[str]]], *, session: str, card_spec: CardSpec) -> None:
    if any(r.get("session") == session for r in rows):
        raise Refused("SESSION_ALREADY_OPEN", "Colab session %r is already active" % session)
    if len(rows) >= TOTAL_SESSION_CEILING:
        raise Refused(
            "POOL_CEILING_REACHED",
            "account has %d active sessions (ceiling %d)" % (len(rows), TOTAL_SESSION_CEILING),
        )
    same_card = 0
    for r in rows:
        accel = (r.get("accelerator") or "").upper()
        raw = (r.get("raw") or "").upper()
        if card_spec.name == "CPU":
            if "CPU" in accel or "| CARD: CPU" in raw or "| BACKEND: CPU" in raw:
                same_card += 1
        elif any(tok.upper() in accel or tok.upper() in raw for tok in card_spec.name_tokens + (card_spec.cli_gpu,)):
            same_card += 1
    if card_spec.pool_ceiling and same_card >= card_spec.pool_ceiling:
        raise Refused(
            "POOL_CEILING_REACHED",
            "account already has %d active %s sessions (pool ceiling %d)"
            % (same_card, card_spec.name, card_spec.pool_ceiling),
        )


def _read_pre_open_usage(colab: Colab, *, card_spec: CardSpec) -> tuple[Optional[float], Optional[int], Optional[float]]:
    res = colab.usage()
    if not res.ok:
        if card_spec.card_class == "gpu":
            raise Refused("PRE_OPEN_READ_FAILED", "pre-open `colab usage` failed (rc=%s)" % res.rc)
        return None, None, None
    try:
        parsed = lg.parse_usage_text(res.out, "cpu" if card_spec.card_class == "cpu" else "gpu",
                                     usage_rc=res.rc, session_opened=False)
    except lg.RateUnread as exc:
        parsed = dict(exc.partial, ok=True, error=None)
    if not parsed.get("ok"):
        if card_spec.card_class == "gpu":
            raise Refused("PRE_OPEN_READ_FAILED", parsed.get("error") or "could not parse pre-open `colab usage`")
        return None, None, None
    pre_rate = parsed.get("rate_measured")
    pre_assign = parsed.get("active_assignments")
    bal = parsed.get("balance")
    if card_spec.card_class == "gpu" and pre_assign is None:
        raise Refused("PRE_OPEN_READ_FAILED", "pre-open `colab usage` reported no Active assignments count")
    if pre_rate is not None and pre_assign is None:
        return None, None, bal
    return pre_rate, pre_assign, bal


def _finalize_close(
    colab: Colab,
    session: str,
    out_dir: Path,
    outcome: dict[str, Any],
    runner: Runner,
    *,
    already_stopped: Optional[bool],
) -> bool:
    if already_stopped is None:
        stopped = runner.stop_session()
    else:
        stopped = bool(already_stopped)
    outcome["session_stopped_confirmed"] = bool(stopped and not (out_dir / "session_open.json").exists())
    if outcome["session_stopped_confirmed"]:
        outcome["closed_at"] = _utc_now()
        sess = colab.sessions()
        if sess.ok:
            rows = parse_sessions_output(sess.out)
            outcome["session_gone_confirmed"] = not any(r.get("session") == session for r in rows)
    return bool(outcome["session_stopped_confirmed"])


def _read_end_balance(colab: Colab) -> Optional[float]:
    res = colab.usage()
    if not res.ok:
        return None
    try:
        parsed = lg.parse_usage_text(res.out, "cpu", usage_rc=res.rc, session_opened=False)
    except lg.RateUnread as exc:  # pragma: no cover - cpu class does not raise RateUnread
        return exc.partial.get("balance")
    return parsed.get("balance") if parsed.get("ok") else None


def launch_window(
    spec_path: Path | str,
    out_dir: Path | str,
    *,
    card: Optional[str] = None,
    shape: Optional[str] = None,
    session: Optional[str] = None,
    cap: Optional[float] = None,
    max_rate: Optional[float] = None,
    wall_s: Optional[float] = None,
    cleanup_bound_s: Optional[float] = None,
    margin: Optional[float] = None,
    resident: Optional[bool] = None,
    extra_env: Sequence[str] = (),
    dry: bool = False,
    local_cpu: bool = False,
    state_dir: Optional[Path | str] = None,
    study_ledger: Optional[Path | str] = None,
    open_lock: Optional[Path | str] = None,
    post_open_polls: int = 6,
    rate_poll_interval_s: float = 5.0,
    colab: Optional[Colab] = None,
    runner_cls: type[Runner] = Runner,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = lambda s: None,
    trace: Callable[[str], None] = lambda s: None,
) -> dict[str, Any]:
    """Validate, stage, plan (`--dry`), or execute (`--local-cpu` / live) one Colab window end-to-end."""
    out_path = _check_empty_out_dir(Path(out_dir))
    spec = WindowSpec.from_file(spec_path)
    (
        card_spec, clean_shape, eff_max_rate, eff_cap, eff_wall_s, eff_margin, eff_cleanup, eff_resident,
    ) = _apply_overrides(
        spec, card=card, shape=shape, cap=cap, max_rate=max_rate, wall_s=wall_s,
        cleanup_bound_s=cleanup_bound_s, margin=margin, resident=resident, local_cpu=local_cpu,
    )
    merged_env = resolve_kit_env(list(spec.env) + list(extra_env))
    if "CR_RUN_ID" in merged_env:
        raise Refused("RUN_ID_RESERVED", "CR_RUN_ID is reserved: omit it from job.env and --env")

    s_dir, l_file = _state_paths(state_dir, study_ledger)
    breaker = CircuitBreaker(s_dir / "card_refusals.json") if s_dir is not None else None
    ledger = StudyLedger(l_file) if l_file is not None else None
    utc_day = utc_day_str()
    if ledger is not None and card_spec.card_class == "gpu" and not dry and not local_cpu:
        ledger.check_card(spec.study, card_spec.name)
    if breaker is not None and not dry and not local_cpu:
        breaker.check(card_spec.name, utc_day)

    guard = SpendGuard(max_rate=eff_max_rate, cap=eff_cap, margin=eff_margin, cleanup_bound_s=eff_cleanup)
    probe = runner_cls(Colab(), "probe", out_path)
    min_bound = probe.own_cleanup_bound_s(eff_resident)
    if eff_cleanup < min_bound:
        raise Refused(
            "CLEANUP_BOUND_TOO_SMALL",
            "cleanup_bound_s %g is below the runner's own cleanup bound %g s (kill, %sstop attempts)"
            % (eff_cleanup, min_bound, "kernel restart, run log, " if eff_resident else ""),
        )
    eff_session = session or default_session_name(spec, card_spec.name)
    window_id = "%s-%s" % (_slug(spec.job), uuid.uuid4().hex[:8])

    out_path.mkdir(parents=True, exist_ok=True)
    staged_dir = out_path / "staged"
    bundle_name = "%s_inputs.tar.gz" % spec.job
    script_name = "%s_job.sh" % spec.job
    bundle_path = staged_dir / bundle_name
    script_path = staged_dir / script_name

    staged_inputs = stage_inputs(spec, bundle_path)
    script_text = render_vm_script(spec, card_spec, bundle_filename=bundle_name)
    script_path.parent.mkdir(parents=True, exist_ok=True)
    script_path.write_text(script_text, encoding="utf-8", newline="\n")

    budget = guard.budget()
    if dry:
        plan = {
            "schema_version": 1,
            "mode": "dry_run",
            "window_id": window_id,
            "session": eff_session,
            "project": {"repo": spec.repo, "study": spec.study, "job": spec.job, "description": spec.description},
            "card": card_spec.name,
            "card_class": card_spec.card_class,
            "shape": clean_shape,
            "wall_s": eff_wall_s,
            "resident": eff_resident,
            "budget": budget,
            "budget_summary": lg.format_dry_cap_budget(budget),
            "staged_files": [asdict(s) for s in staged_inputs],
            "script": str(script_path),
            "bundle": str(bundle_path),
            "outputs": {"collect": list(spec.collect_outputs), "salvage": list(spec.salvage_outputs)},
        }
        write_json_atomic(out_path / "dry_run_plan.json", plan)
        return plan

    mode_name = "local_cpu" if local_cpu else "live"
    if colab is not None:
        client = colab
    elif local_cpu:
        client = LocalColab(out_path / "_local_vm_content")
    else:
        client = cli_from_env()

    outcome = _base_outcome(
        window_id=window_id, session=eff_session, spec=spec, card_spec=card_spec,
        shape=clean_shape, mode=mode_name, cap_ccu=eff_cap, margin_ccu=eff_margin,
        cleanup_bound_s=eff_cleanup, max_rate=eff_max_rate, staged=staged_inputs,
    )
    meta = {
        "schema_version": 1,
        "window_id": window_id,
        "session": eff_session,
        "project": outcome["project"],
        "card": card_spec.name,
        "card_class": card_spec.card_class,
        "shape": clean_shape,
        "mode": mode_name,
        "started_at": _utc_now(),
        "wall_s": eff_wall_s,
        "cap_ccu": eff_cap,
        "max_rate": eff_max_rate,
    }
    write_json_atomic(out_path / "window_meta.json", meta)

    runner = runner_cls(
        client, eff_session, out_path, job=spec.job, content="/content", poll_s=spec.poll_s,
        pre_opened=True, clock=clock, sleep=sleep, log=log, trace=trace,
    )
    lock_path = Path(open_lock) if open_lock else (
        (s_dir / "open.lock") if s_dir is not None else Path(tempfile.gettempdir()) / "colab-runner-open.lock"
    )
    t_open_monotonic: Optional[float] = None
    marker_written = False
    try:
        with _open_lock(lock_path, clock=clock, sleep=sleep):
            sess_res = client.sessions()
            if sess_res.ok:
                _pool_check(parse_sessions_output(sess_res.out), session=eff_session, card_spec=card_spec)
            pre_rate, pre_assign, bal_start = _read_pre_open_usage(client, card_spec=card_spec)
            outcome["account_balance"]["start"] = bal_start
            guard = SpendGuard(
                max_rate=eff_max_rate, cap=eff_cap, margin=eff_margin, cleanup_bound_s=eff_cleanup,
                pre_open_rate=pre_rate, pre_open_assignments=pre_assign,
            )
            warn = neighbour_warning(pre_assign or 0)
            if warn:
                log(scrub(warn))

            write_json_atomic(out_path / "session_open.json", {"session": eff_session, "job": spec.job, "content": "/content"})
            marker_written = True
            new_res = client.new_session(eff_session, card=card_spec.cli_gpu, shape=clean_shape)
            if not new_res.ok:
                msg = scrub((new_res.err or new_res.out or "rc=%s" % new_res.rc).strip())
                if breaker is not None:
                    breaker.record_refusal(card_spec.name, reason="SESSION_OPEN_FAILED: %s" % msg,
                                           session=eff_session, day=utc_day)
                check_rows = client.sessions()
                listed = any(r.get("session") == eff_session for r in parse_sessions_output(check_rows.out)) if check_rows.ok else True
                if not listed:
                    (out_path / "session_open.json").unlink(missing_ok=True)
                    outcome["session_stopped_confirmed"] = True
                    outcome["session_gone_confirmed"] = True
                else:
                    _finalize_close(client, eff_session, out_path, outcome, runner, already_stopped=None)
                outcome["status"] = "open_failed"
                outcome["exit_code"] = 2
                outcome["refusal"] = error_record("SESSION_OPEN_FAILED", "colab new failed: %s" % msg)
                write_json_atomic(out_path / "outcome.json", outcome)
                return outcome

            t_open_monotonic = clock()
            outcome["opened"] = True
            outcome["opened_at"] = _utc_now()

            if card_spec.card_class == "gpu":
                after_open = client.sessions()
                rows = parse_sessions_output(after_open.out) if after_open.ok else []
                row = next((r for r in rows if r.get("session") == eff_session), None)
                expected_shape = shape_row_label(clean_shape or "standard")
                observed_shape = row.get("shape") if row else None
                if observed_shape != expected_shape:
                    if breaker is not None:
                        breaker.record_refusal(
                            card_spec.name, reason="SHAPE_MISMATCH: expected %s got %s" % (expected_shape, observed_shape),
                            session=eff_session, day=utc_day,
                        )
                    _finalize_close(client, eff_session, out_path, outcome, runner, already_stopped=None)
                    elapsed_now = clock() - t_open_monotonic
                    _book_spend(outcome, card_spec=card_spec, guard=guard, elapsed_s=elapsed_now,
                                bal_end=_read_end_balance(client))
                    outcome["status"] = "shape_mismatch"
                    outcome["exit_code"] = 2
                    outcome["refusal"] = error_record(
                        "SHAPE_MISMATCH",
                        "expected Shape: %s for %s, got %r" % (expected_shape, card_spec.name, observed_shape),
                    )
                    write_json_atomic(out_path / "outcome.json", outcome)
                    return outcome

                for idx in range(max(1, int(post_open_polls))):
                    u = client.usage()
                    guard.fold_usage(u.out if u.ok else u.err, rc=u.rc, epoch=clock(), at=_utc_now())
                    decision = guard.decision()
                    if decision == "above_max":
                        _finalize_close(client, eff_session, out_path, outcome, runner, already_stopped=None)
                        _book_spend(outcome, card_spec=card_spec, guard=guard, elapsed_s=clock() - t_open_monotonic,
                                    bal_end=_read_end_balance(client))
                        outcome["status"] = "cap"
                        outcome["exit_code"] = 2
                        outcome["cap_trip"] = error_record(
                            "RATE_ABOVE_MAX",
                            "window stable rate %s/h exceeds max_rate %s/h" % (guard.state.get("best_stable"), eff_max_rate),
                        )
                        write_json_atomic(out_path / "outcome.json", outcome)
                        return outcome
                    if decision in ("ok", "unattributable"):
                        break
                    if idx + 1 < int(post_open_polls):
                        sleep(max(0.0, float(rate_poll_interval_s)))
                if guard.decision() == "unread":
                    _finalize_close(client, eff_session, out_path, outcome, runner, already_stopped=None)
                    _book_spend(outcome, card_spec=card_spec, guard=guard, elapsed_s=clock() - t_open_monotonic,
                                bal_end=_read_end_balance(client))
                    outcome["status"] = "open_failed"
                    outcome["exit_code"] = 2
                    outcome["refusal"] = error_record(
                        "POST_OPEN_RATE_UNREAD",
                        "post-open `colab usage` did not settle on a positive stable GPU rate",
                    )
                    write_json_atomic(out_path / "outcome.json", outcome)
                    return outcome

        # Open lock released: upload bundle and driver script flat into /content.
        for local_f, remote_f in (
            (bundle_path, "/content/%s" % bundle_name),
            (script_path, "/content/%s" % script_name),
        ):
            up = client.upload(eff_session, str(local_f), remote_f)
            if not up.ok:
                msg = scrub((up.err or up.out or "rc=%s" % up.rc).strip())
                _finalize_close(client, eff_session, out_path, outcome, runner, already_stopped=None)
                _book_spend(outcome, card_spec=card_spec, guard=guard, elapsed_s=clock() - t_open_monotonic,
                            bal_end=_read_end_balance(client))
                outcome["status"] = "upload_failed"
                outcome["exit_code"] = 2
                outcome["refusal"] = error_record("UPLOAD_FAILED", "upload of %s failed: %s" % (local_f.name, msg))
                write_json_atomic(out_path / "outcome.json", outcome)
                return outcome
    except BaseException:
        if marker_written:
            _finalize_close(client, eff_session, out_path, outcome, runner, already_stopped=None)
        raise

    collected: list[str] = []
    salvaged: list[str] = []

    def _do_collect() -> bool:
        if not spec.collect_outputs:
            return True
        collected.extend(
            _download_declared_outputs(runner, spec.collect_outputs, out_path / "collected", content="/content", job=spec.job)
        )
        return True

    def _do_salvage(reason: str) -> bool:
        if not spec.salvage_outputs:
            return True
        salvaged.extend(
            _download_declared_outputs(runner, spec.salvage_outputs, out_path / "salvage", content="/content", job=spec.job)
        )
        return True

    run_env = dict(merged_env)
    run_env["CR_CARD"] = card_spec.name
    run_env["CR_CARD_CLASS"] = card_spec.card_class
    run_env["CR_DRY_RUN"] = "1" if (local_cpu or card_spec.card_class == "cpu") else "0"
    if spec.progress_glob:
        run_env["CR_PROGRESS_GLOB"] = spec.progress_glob
    if spec.units_glob:
        run_env["CR_UNITS_GLOB"] = spec.units_glob

    run_error: Optional[BaseException] = None
    run_result = None
    try:
        run_result = runner.run(
            script="/content/%s" % script_name,
            env=run_env,
            wall_s=eff_wall_s,
            guard=guard,
            progress_stall_s=spec.progress_stall_s,
            setup_stall_s=spec.setup_stall_s,
            resident=eff_resident,
            salvage=_do_salvage if spec.salvage_outputs else None,
            collect=_do_collect if spec.collect_outputs else None,
            unreachable_polls=spec.unreachable_polls,
            unreachable_s=spec.unreachable_s,
        )
    except BaseException as exc:
        run_error = exc
        run_result = runner._res

    stopped_ok = _finalize_close(
        client, eff_session, out_path, outcome, runner,
        already_stopped=(run_result.stopped if run_result is not None else None),
    )
    elapsed_total = clock() - (t_open_monotonic if t_open_monotonic is not None else clock())
    _book_spend(outcome, card_spec=card_spec, guard=guard, elapsed_s=elapsed_total, bal_end=_read_end_balance(client))

    outcome["run_rc"] = run_result.rc
    outcome["collected_outputs"] = list(collected)
    outcome["salvaged_outputs"] = list(salvaged)
    outcome["quarantined"] = list(run_result.quarantined)
    outcome["cap_trip"] = run_result.cap_trip
    outcome["refusal"] = run_result.refusal

    if run_result.reason == "failed" and run_result.rc == CARD_CHECK_FAIL_RC and _card_mismatch_seen(out_path, spec.job):
        outcome["status"] = "card_mismatch"
        outcome["refusal"] = error_record(
            "CARD_MISMATCH",
            "VM GPU verification failed for %s (%s/vm_card.json)" % (card_spec.name, spec.job),
        )
        if breaker is not None:
            breaker.record_refusal(card_spec.name, reason="CARD_MISMATCH", session=eff_session, day=utc_day)
    else:
        outcome["status"] = run_result.reason

    if run_error is not None:
        outcome["status"] = "error"
        if outcome["refusal"] is None:
            if isinstance(run_error, BinaryDownloadRefused):
                outcome["refusal"] = error_record("BINARY_DOWNLOAD_TOKEN", scrub(run_error.message))
            elif isinstance(run_error, QuarantineEscape):
                outcome["refusal"] = error_record("QUARANTINE_ESCAPE", scrub(run_error.message))
            else:
                outcome["refusal"] = error_record("UNEXPECTED_ERROR", scrub("%s: %s" % (type(run_error).__name__, run_error)))
        outcome["exit_code"] = 2
    elif outcome["status"] == "done":
        outcome["exit_code"] = 0 if stopped_ok else 3
    else:
        outcome["exit_code"] = 2

    write_json_atomic(out_path / "outcome.json", outcome)
    if ledger is not None and card_spec.card_class == "gpu" and not local_cpu:
        ledger.record_window(spec.study, outcome)
    return outcome


def _load_opt_json(path: Path) -> Optional[dict[str, Any]]:
    if not path.is_file():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else None
    except (OSError, ValueError):
        return None


def status_window(out_dir: Path | str, *, tail_lines: int = 10) -> dict[str, Any]:
    """Inspect a live, pending-stop, dry-run, or completed window out-dir without touching the VM."""
    root = Path(out_dir)
    if not root.is_dir():
        raise Refused("STATUS_DIR_MISSING", "window directory does not exist: %s" % root)

    dry_plan = _load_opt_json(root / "dry_run_plan.json")
    outcome = _load_opt_json(root / "outcome.json")
    meta = _load_opt_json(root / "window_meta.json")
    sess_open = _load_opt_json(root / "session_open.json")
    run_res = _load_opt_json(root / "runner_result.json")
    stop_conf = _load_opt_json(root / "stop_confirmed.json")
    stop_pend = _load_opt_json(root / "stop_pending.json")

    job_name = (
        (meta or {}).get("project", {}).get("job")
        or (dry_plan or {}).get("project", {}).get("job")
        or (sess_open or {}).get("job")
        or "job"
    )
    run_log = root / ("%s.run.out" % job_name)
    tail: list[str] = []
    if run_log.is_file():
        raw_lines = run_log.read_text(encoding="utf-8", errors="replace").splitlines()
        tail = [scrub(ln) for ln in raw_lines[-max(0, int(tail_lines)):]]

    if outcome is not None:
        state = "stop_pending" if (sess_open is not None and not outcome.get("session_stopped_confirmed")) else "finished"
    elif dry_plan is not None:
        state = "dry_run"
    elif stop_pend is not None or (sess_open is not None and run_res is not None):
        state = "stop_pending"
    elif sess_open is not None:
        state = "running"
    elif stop_conf is not None or run_res is not None:
        state = "finished"
    else:
        state = "unknown"

    return {
        "out_dir": str(root),
        "state": state,
        "session": (outcome or meta or sess_open or dry_plan or {}).get("session"),
        "card": (outcome or meta or dry_plan or {}).get("card"),
        "shape": (outcome or meta or dry_plan or {}).get("shape"),
        "status": (outcome or run_res or {}).get("status") or (run_res or {}).get("reason"),
        "elapsed_s": (outcome or run_res or {}).get("elapsed_s"),
        "spend_ccu": (outcome or {}).get("spend_ccu"),
        "session_stopped_confirmed": (
            outcome.get("session_stopped_confirmed") if outcome is not None else (stop_conf is not None and sess_open is None)
        ),
        "stop_pending": stop_pend is not None or (sess_open is not None and run_res is not None),
        "outcome": outcome,
        "last_log_lines": tail,
    }


__all__ = [
    "LocalColab",
    "default_cleanup_bound_s",
    "default_session_name",
    "launch_window",
    "resolve_kit_env",
    "status_window",
]

"""Declarative window launcher: plan (`--dry`), local CPU dry-run (`--local-cpu`), live launch, booking `outcome.json`, and `status`."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import tarfile
import tempfile
import threading
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
    pool_key_for_card,
    pool_key_for_session_row,
    shape_row_label,
    utc_day_str,
    validate_card_and_shape,
)
from colab_runner.colab_cli import (
    KILL_WAIT_S,
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
from colab_runner.errors import Refused, cap_trip_record, error_record
from colab_runner.manifest import DEFAULT_SEALED_HOST, StagedInputRecord, WindowSpec, render_vm_script, stage_inputs
from colab_runner.queue import classify_window_outcome
from colab_runner.runner import (
    DEFAULT_DOWNLOAD_TIMEOUT_S,
    MIN_CALL_S,
    _CELL_TRANSIENT_ENV,
    RunInterrupted,
    Runner,
    require_number,
    vm_lib_source,
    write_json_atomic,
)
from colab_runner.spend import SpendGuard, neighbour_warning
from colab_runner.window_lib import launch_guards as lg

ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
DEFAULT_CLEANUP_FLOOR_S = 600.0
OPEN_LOCK_STALE_MTIME_S = 10.0
DEFAULT_TAR_TIMEOUT_S = 60.0
DEFAULT_DOWNLOAD_CHUNK_BYTES = 256 * 1024 * 1024
_STREAM_BUF_BYTES = 1024 * 1024

_TAR_OUTPUT_CELL_SRC = """
import glob as _cr_glob
import hashlib as _cr_hash
import io as _cr_io
import json as _cr_json
import posixpath as _cr_posix
import tarfile as _cr_tar

_CR_SMALL_TEXT_SCAN_BYTES = 8 * 1024 * 1024


class _CrPartWriter:
    def __init__(self, base_path, chunk_bytes):
        self.base_path = base_path
        self.chunk_bytes = max(1, int(chunk_bytes))
        self.parts = []
        self.total_size = 0
        self.total_sha = _cr_hash.sha256()
        self._fh = None
        self._part_path = None
        self._part_size = 0
        self._part_sha = None
        self._idx = 0
        parent = os.path.dirname(base_path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        for stale in _cr_glob.glob(base_path + ".part*") + [base_path + ".tmp"]:
            if not os.path.islink(stale) and os.path.isfile(stale):
                try:
                    os.unlink(stale)
                except OSError:
                    pass

    def _open_next(self):
        self._close_part()
        self._part_path = "%s.part%04d" % (self.base_path, self._idx)
        self._idx += 1
        self._fh = open(self._part_path, "wb")
        self._part_size = 0
        self._part_sha = _cr_hash.sha256()

    def _close_part(self):
        if self._fh is not None:
            self._fh.close()
            self.parts.append({
                "path": self._part_path,
                "size": self._part_size,
                "sha256": self._part_sha.hexdigest(),
            })
            self._fh = None

    def writable(self):
        return True

    def tell(self):
        return self.total_size

    def write(self, data):
        mv = memoryview(data)
        off = 0
        n = len(mv)
        while off < n:
            if self._fh is None or self._part_size >= self.chunk_bytes:
                self._open_next()
            space = self.chunk_bytes - self._part_size
            chunk = mv[off:off + space]
            self._fh.write(chunk)
            clen = len(chunk)
            self._part_sha.update(chunk)
            self._part_size += clen
            self.total_sha.update(chunk)
            self.total_size += clen
            off += clen
        return n

    def close(self):
        self._close_part()


def _cr_tar_output():
    rel = os.environ.get("CR_TAR_REL", "").strip().replace("\\\\", "/")
    if not rel:
        print("CR_TAR_MISSING", flush=True)
        return
    try:
        chunk_bytes = max(1, int(os.environ.get("CR_TAR_CHUNK_BYTES", "268435456")))
    except ValueError:
        chunk_bytes = 268435456
    work_dir = os.path.join(CR_CONTENT, CR_JOB + "_work")
    src = None
    kind = None
    for base in (work_dir, CR_OUT):
        cand = os.path.join(base, rel)
        if os.path.islink(cand):
            continue
        if os.path.isdir(cand):
            src, kind = cand, "dir"
            break
        if os.path.isfile(cand):
            src, kind = cand, "file"
            break
    if src is None:
        print("CR_TAR_MISSING", flush=True)
        return

    if kind == "file":
        fsize = os.lstat(src).st_size
        if fsize <= chunk_bytes:
            h = _cr_hash.sha256()
            with open(src, "rb") as fh:
                while True:
                    blk = fh.read(1024 * 1024)
                    if not blk:
                        break
                    h.update(blk)
            manifest = {"kind": "file", "remote": src, "size": fsize, "sha256": h.hexdigest(), "parts": []}
        else:
            stage_base = os.path.join(CR_OUT, rel)
            writer = _CrPartWriter(stage_base, chunk_bytes)
            with open(src, "rb") as fh:
                while True:
                    blk = fh.read(1024 * 1024)
                    if not blk:
                        break
                    writer.write(blk)
            writer.close()
            manifest = {
                "kind": "file",
                "remote": None,
                "size": writer.total_size,
                "sha256": writer.total_sha.hexdigest(),
                "parts": writer.parts,
            }
        print("CR_TAR_READY", flush=True)
        print("CR_TAR_MANIFEST " + _cr_json.dumps(manifest), flush=True)
        return

    tar_path = os.path.join(CR_OUT, rel + ".tar")
    writer = _CrPartWriter(tar_path, chunk_bytes)
    with _cr_tar.open(fileobj=writer, mode="w:") as tf:
        for dirpath, dirnames, filenames in os.walk(src, followlinks=False):
            rel_dir = os.path.relpath(dirpath, src)
            dir_arc = "." if rel_dir == "." else rel_dir.replace(os.sep, "/")
            keep = []
            for d in sorted(dirnames):
                dp = os.path.join(dirpath, d)
                if not os.path.islink(dp) and os.path.isdir(dp):
                    keep.append(d)
                    sub_arc = d if dir_arc == "." else _cr_posix.join(dir_arc, d)
                    ti = _cr_tar.TarInfo(sub_arc)
                    ti.type = _cr_tar.DIRTYPE
                    ti.mode = 0o755
                    tf.addfile(ti)
            dirnames[:] = keep
            for fn in sorted(filenames):
                fp = os.path.join(dirpath, fn)
                if os.path.islink(fp) or not os.path.isfile(fp):
                    continue
                arc = fn if dir_arc == "." else _cr_posix.join(dir_arc, fn)
                fsize = os.lstat(fp).st_size
                ti = _cr_tar.TarInfo(arc)
                ti.mode = 0o644
                if fsize <= _CR_SMALL_TEXT_SCAN_BYTES:
                    with open(fp, "rb") as fh:
                        data = fh.read()
                    if b"\\x00" not in data:
                        try:
                            txt = data.decode("utf-8")
                            scrubbed = scrub_colab_tokens(txt)
                            if scrubbed != txt:
                                data = scrubbed.encode("utf-8")
                        except UnicodeDecodeError:
                            pass
                    ti.size = len(data)
                    tf.addfile(ti, _cr_io.BytesIO(data))
                else:
                    ti.size = fsize
                    with open(fp, "rb") as fh:
                        tf.addfile(ti, fh)
    writer.close()
    if len(writer.parts) == 1:
        os.replace(writer.parts[0]["path"], tar_path)
        manifest = {
            "kind": "dir",
            "remote": tar_path,
            "size": writer.total_size,
            "sha256": writer.total_sha.hexdigest(),
            "parts": [],
        }
    else:
        manifest = {
            "kind": "dir",
            "remote": None,
            "size": writer.total_size,
            "sha256": writer.total_sha.hexdigest(),
            "parts": writer.parts,
        }
    print("CR_TAR_READY", flush=True)
    print("CR_TAR_MANIFEST " + _cr_json.dumps(manifest), flush=True)

_cr_tar_output()
"""


def _utc_now(dt: Optional[datetime] = None) -> str:
    return (dt or datetime.now(timezone.utc)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _refuse_native_windows() -> None:
    """Refuse launch on native Windows so callers run on Linux or WSL."""
    if sys.platform == "win32":
        raise Refused("PLATFORM_UNSUPPORTED", "colab-runner runs on Linux or WSL")


def _slug(value: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9-]+", "-", (value or "win").strip().lower()).strip("-")
    return cleaned[:24] or "win"


def default_session_name(spec: WindowSpec, card: str) -> str:
    """Build a unique Colab session name from the job, card, and a short random suffix."""
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


def _refuse_reserved_env(env: Mapping[str, str]) -> None:
    """Refuse CR_RUN_ID and any CR_* variable supplied by user job.env or --env."""
    if "CR_RUN_ID" in env:
        raise Refused("RUN_ID_RESERVED", "CR_RUN_ID is reserved: omit it from job.env and --env")
    for name in env:
        if str(name).startswith("CR_"):
            raise Refused("ENV_RESERVED", "%s is reserved: omit CR_* variables from job.env and --env" % name)


def build_job_env(
    spec: WindowSpec,
    card_spec: CardSpec,
    *,
    content: str,
    local_cpu: bool = False,
    extra_env: Mapping[str, str] | None = None,
) -> dict[str, str]:
    """Build the complete job environment shared by live Colab windows and `--local-cpu` runs."""
    if extra_env:
        _refuse_reserved_env(extra_env)
    content_base = content.rstrip("/\\") or "/"
    out_dir = f"{content_base}/{spec.job}_out" if content_base != "/" else f"/{spec.job}_out"
    env: dict[str, str] = {}
    if extra_env:
        env.update({str(k): str(v) for k, v in extra_env.items()})
    env.update({
        "CR_CONTENT": content,
        "CR_JOB": spec.job,
        "CR_OUT_DIR": out_dir,
        "CR_CARD": card_spec.name,
        "CR_CARD_CLASS": card_spec.card_class,
        "CR_DRY_RUN": "1" if (local_cpu or card_spec.card_class == "cpu") else "0",
        "CR_PROGRESS_GLOB": spec.progress_glob or "",
        "CR_UNITS_GLOB": spec.units_glob or "",
        "CR_POLL_EVERY_S": str(spec.poll_s),
        "CR_NVIDIA_SMI": "",
    })
    return env


def _download_chunk_bytes() -> int:
    raw = os.environ.get("CR_DOWNLOAD_CHUNK_BYTES", "").strip()
    if not raw:
        return DEFAULT_DOWNLOAD_CHUNK_BYTES
    try:
        val = int(raw)
    except ValueError:
        raise Refused("SETTING_INVALID", "CR_DOWNLOAD_CHUNK_BYTES must be a positive integer") from None
    if val < 1:
        raise Refused("SETTING_INVALID", "CR_DOWNLOAD_CHUNK_BYTES must be a positive integer")
    return val


def cleanup_outputs_bound_s(
    spec: WindowSpec,
    card_spec: CardSpec,
    *,
    download_timeout_s: float = DEFAULT_DOWNLOAD_TIMEOUT_S,
) -> float:
    """Worst-case seconds for `vm_card.json` plus salvage (or collect) downloads including directory tars, chunks, and kills."""
    one_dl_s = float(download_timeout_s) + 2.0 * KILL_WAIT_S
    tar_exec_s = DEFAULT_TAR_TIMEOUT_S + 2.0 * KILL_WAIT_S
    per_output_s = float(max(3, int(spec.download_parts))) * one_dl_s + tar_exec_s
    vm_card_s = one_dl_s if (card_spec.card_class == "gpu" and card_spec.cli_flag == "--gpu") else 0.0
    salvage_s = vm_card_s + len(spec.salvage_outputs) * per_output_s
    collect_s = vm_card_s + len(spec.collect_outputs) * per_output_s
    return max(salvage_s, collect_s)


def default_cleanup_bound_s(
    resident: bool,
    runner_cls: type[Runner] = Runner,
    *,
    outputs_bound_s: float = 0.0,
) -> float:
    """Compute a safe default `--cleanup-bound-s` clearing `Runner.own_cleanup_bound_s` plus declared outputs."""
    probe = runner_cls(Colab(), "probe", Path.cwd() / "probe_out")
    return max(DEFAULT_CLEANUP_FLOOR_S, probe.own_cleanup_bound_s(resident) + max(0.0, float(outputs_bound_s)))


def open_phase_bound_s(
    card_spec: CardSpec,
    *,
    local_cpu: bool = False,
    shape_attempts: int = 3,
    sessions_timeout_s: float = 30.0,
    post_open_polls: int = 6,
    usage_timeout_s: float = 60.0,
    rate_poll_interval_s: float = 5.0,
    upload_timeout_s: float = 120.0,
) -> float:
    """Worst-case seconds after `colab new` succeeds until `Runner.run` starts polling."""
    uploads_bound = 2 * (float(upload_timeout_s) + 2.0 * KILL_WAIT_S)
    if local_cpu or card_spec.card_class == "cpu":
        return uploads_bound
    shape_pauses = sum(2.0 * i for i in range(1, max(1, int(shape_attempts))))
    shape_bound = int(shape_attempts) * (float(sessions_timeout_s) + 2.0 * KILL_WAIT_S) + shape_pauses
    rate_pauses = max(0, int(post_open_polls) - 1) * float(rate_poll_interval_s)
    rate_bound = int(post_open_polls) * (float(usage_timeout_s) + 2.0 * KILL_WAIT_S) + rate_pauses
    return shape_bound + rate_bound + uploads_bound


def compute_launch_budget(
    guard: SpendGuard,
    open_bound_s: float,
    *,
    outputs_bound_s: float = 0.0,
) -> dict[str, Any]:
    """Combine `SpendGuard.budget()` with open-phase and cleanup-outputs bounds (`cap` counts from before `colab new`)."""
    base = dict(guard.budget())
    priced_rate = float(base["priced_rate"])
    open_units = round((max(0.0, float(open_bound_s)) / 3600.0) * priced_rate, 6)
    wc_cap = round(float(base["worst_case_cap_path"]), 6)
    base["cleanup_outputs_bound_s"] = round(max(0.0, float(outputs_bound_s)), 2)
    base["open_phase_bound_s"] = round(float(open_bound_s), 2)
    base["open_phase_bound_units"] = open_units
    base["death_time_max_s"] = None
    base["worst_case_cap_path"] = wc_cap
    base["worst_case_launcher_death_path"] = "unbounded until stop --out-dir"
    base["worst_case_total_spend"] = wc_cap
    base["worst_case_total_spend_units"] = wc_cap
    base["assumptions"] = {
        "cap_path": (
            "live launcher stays alive, counts elapsed from before colab new, clamps open and run calls to "
            "remaining cap, and runs bounded colab stop"
        ),
        "launcher_death_path": "unbounded until stop --out-dir (launch starts no background watcher)",
    }
    return base


def _cap_eq_tag(val: float, cap_v: float, label: str = "cap") -> str:
    return f" (= {label} {cap_v})" if abs(float(val) - float(cap_v)) < 1e-6 else ""


def format_launch_budget(rec: dict[str, Any]) -> str:
    """Format honest dry-run budget lines (`worst_case_total_spend <= cap`, unbounded death path)."""
    assumptions = rec.get("assumptions") or {}
    cap_v = float(rec["cap"])
    lines = [
        f"formula: {rec['formula']}",
        f"cap: {rec['cap']}",
        f"margin: {rec['margin']}",
        f"cleanup_bound_s: {rec['cleanup_bound_s']}",
        f"cleanup_outputs_bound_s: {rec.get('cleanup_outputs_bound_s', 0.0)}",
        f"open_phase_bound_s: {rec['open_phase_bound_s']}",
        f"priced_rate: {rec['priced_rate']}/h",
        f"max_rate: {rec['max_rate']}/h",
        f"reserve (cleanup_reserve): {rec['cleanup_reserve_units']} ({rec['cleanup_bound_s']} / 3600 * {rec['priced_rate']})",
        f"open_phase_bound_units: {rec['open_phase_bound_units']} ({rec['open_phase_bound_s']} / 3600 * {rec['priced_rate']})",
        f"effective_trip: {rec['effective_trip_units']} ({rec['cap']} - {rec['margin']} - {rec['cleanup_reserve_units']})",
        f"runtime_to_trip (priced_rate {rec['priced_rate']}/h): {rec['runtime_at_priced_rate_s']} s",
        f"runtime_to_trip (--max-rate {rec['max_rate']}/h): {rec['runtime_at_max_rate_s']} s",
        (
            f"worst_case_cap_path: {rec['worst_case_cap_path']}"
            f" (effective_trip {rec['effective_trip_units']} + margin {rec['margin']} + "
            f"cleanup_reserve {rec['cleanup_reserve_units']}"
            f"{_cap_eq_tag(rec['worst_case_cap_path'], cap_v)})"
        ),
        f"worst_case_launcher_death_path: {rec['worst_case_launcher_death_path']}",
        (
            f"worst_case_total_spend: {rec['worst_case_total_spend']}"
            f" (live cap_path {rec['worst_case_cap_path']}{_cap_eq_tag(rec['worst_case_total_spend'], cap_v)}; "
            f"launcher death is unbounded until stop --out-dir)"
        ),
        (
            f"assumptions: cap_path assumes {assumptions.get('cap_path', '')}; "
            f"launcher_death_path: {assumptions.get('launcher_death_path', '')}"
        ),
        json.dumps(rec),
    ]
    return "\n".join(lines)


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


def _default_user_state_dir() -> Path:
    """Return a persistent per-user state directory outside the system temp directory."""
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "colab-runner"
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "colab-runner"
    base = os.environ.get("XDG_STATE_HOME") or str(Path.home() / ".local" / "state")
    return Path(base) / "colab-runner"


def _state_paths(
    state_dir: Optional[Path | str],
    study_ledger: Optional[Path | str],
    *,
    live: bool,
) -> tuple[Optional[Path], Optional[Path]]:
    raw_state = state_dir or os.environ.get("COLAB_RUNNER_STATE_DIR")
    if not raw_state and live:
        s_dir: Optional[Path] = _default_user_state_dir()
    else:
        s_dir = Path(raw_state) if raw_state else None
    l_file = Path(study_ledger) if study_ledger else (s_dir / "study_ledger.json" if s_dir is not None else None)
    return s_dir, l_file


def _default_open_lock_path(open_lock: Optional[Path | str]) -> Path:
    if open_lock:
        return Path(open_lock)
    env_lock = os.environ.get("COLAB_RUNNER_OPEN_LOCK", "").strip()
    if env_lock:
        return Path(env_lock)
    return _default_user_state_dir() / "open.lock"


def _pid_alive(pid: int) -> bool:
    """Return True if process `pid` appears alive on this OS (never treats zombie on POSIX as alive)."""
    if pid <= 0:
        return False
    if os.name == "posix":
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            return False
        except PermissionError:
            return True
        try:
            stat_txt = Path("/proc/%d/stat" % pid).read_text(encoding="utf-8", errors="replace")
            return stat_txt.rsplit(")", 1)[1].split()[0] != "Z"
        except OSError:
            return True
    else:  # pragma: no cover - Windows Win32 API; CI runs Linux
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        k32 = ctypes.windll.kernel32
        handle = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
        if not handle:
            return k32.GetLastError() == 5  # ACCESS_DENIED -> exists
        try:
            code = ctypes.c_ulong()
            if k32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return code.value == STILL_ACTIVE
            return True
        finally:
            k32.CloseHandle(handle)


def _read_owner_bytes(dir_path: Path) -> Optional[bytes]:
    """Read `dir_path/owner.json` raw bytes, `b\"\"` if absent, or `None` on I/O error."""
    try:
        return (dir_path / "owner.json").read_bytes()
    except FileNotFoundError:
        return b""
    except OSError:
        return None


def _check_lock_dir_stale(dir_path: Path, *, pid_alive: Callable[[int], bool]) -> tuple[bool, Optional[bytes]]:
    """Check if `dir_path` belongs to a dead PID (or is an old ownerless dir) and return its owner bytes."""
    raw = _read_owner_bytes(dir_path)
    if raw:
        try:
            info = json.loads(raw.decode("utf-8"))
            pid = info.get("pid") if isinstance(info, dict) else None
            if isinstance(pid, int) and not pid_alive(pid):
                return True, raw
        except (UnicodeDecodeError, ValueError):
            pass
        return False, None
    if raw == b"" and dir_path.exists():
        try:
            if (time.time() - dir_path.stat().st_mtime) > OPEN_LOCK_STALE_MTIME_S:
                return True, b""
        except OSError:
            return False, None
    return False, None


def _is_lock_dir_stale(dir_path: Path, *, pid_alive: Callable[[int], bool]) -> bool:
    """Return True when a lock or reclaim directory belongs to a dead PID (or is an old ownerless dir)."""
    return _check_lock_dir_stale(dir_path, pid_alive=pid_alive)[0]


def _claim_reclaim_guard(reclaim_guard: Path) -> bool:
    """Build `reclaim_guard` via a prep directory holding `owner.json`, then rename into place."""
    if reclaim_guard.exists():
        return False
    prep_dir = reclaim_guard.with_name(
        "%s.prep-%d-%s" % (reclaim_guard.name, os.getpid(), uuid.uuid4().hex[:8])
    )
    try:
        prep_dir.mkdir(parents=False)
        write_json_atomic(
            prep_dir / "owner.json",
            {"pid": os.getpid(), "at": _utc_now(), "nonce": uuid.uuid4().hex[:12]},
        )
        if reclaim_guard.exists():
            shutil.rmtree(prep_dir, ignore_errors=True)
            return False
        os.rename(prep_dir, reclaim_guard)
        return True
    except OSError:
        shutil.rmtree(prep_dir, ignore_errors=True)
        return False


def _try_acquire_reclaim_guard(reclaim_guard: Path, *, pid_alive: Callable[[int], bool]) -> bool:
    """Atomically claim `reclaim_guard` with `owner.json` (PID + time + nonce), reclaiming only matching stale dirs."""
    if _claim_reclaim_guard(reclaim_guard):
        return True
    stale, expected_bytes = _check_lock_dir_stale(reclaim_guard, pid_alive=pid_alive)
    if not stale:
        return False
    stale_guard = reclaim_guard.with_name(
        "%s.stale-%d-%s" % (reclaim_guard.name, os.getpid(), uuid.uuid4().hex[:8])
    )
    try:
        os.rename(reclaim_guard, stale_guard)
    except OSError:
        return False
    moved_bytes = _read_owner_bytes(stale_guard)
    still_stale = moved_bytes == expected_bytes
    if still_stale and expected_bytes == b"":
        try:
            still_stale = (time.time() - stale_guard.stat().st_mtime) > OPEN_LOCK_STALE_MTIME_S
        except OSError:
            still_stale = False
    if not still_stale:
        try:
            if not reclaim_guard.exists():
                os.rename(stale_guard, reclaim_guard)
                return False
        except OSError:
            pass
        shutil.rmtree(stale_guard, ignore_errors=True)
        return False
    shutil.rmtree(stale_guard, ignore_errors=True)
    return _claim_reclaim_guard(reclaim_guard)


@contextlib.contextmanager
def _acquire_dir_lock(
    lock_dir: Path,
    *,
    timeout_s: float = 1200.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    pid_alive: Callable[[int], bool] = _pid_alive,
):
    """Cross-process directory lock with dead-PID stale-lock and crashed-reclaimer recovery."""
    owner_file = lock_dir / "owner.json"
    deadline = clock() + max(0.0, float(timeout_s))
    while True:
        try:
            lock_dir.mkdir(parents=False)
            write_json_atomic(owner_file, {"pid": os.getpid(), "at": _utc_now()})
            break
        except FileExistsError:
            if _is_lock_dir_stale(lock_dir, pid_alive=pid_alive):
                reclaim_guard = lock_dir.with_suffix(".reclaim")
                if _try_acquire_reclaim_guard(reclaim_guard, pid_alive=pid_alive):
                    reclaim_dir = lock_dir.with_name(
                        "%s.stale-%d-%s" % (lock_dir.name, os.getpid(), uuid.uuid4().hex[:8])
                    )
                    try:
                        if _is_lock_dir_stale(lock_dir, pid_alive=pid_alive):
                            try:
                                os.rename(lock_dir, reclaim_dir)
                                lock_dir.mkdir(parents=False)
                                write_json_atomic(owner_file, {"pid": os.getpid(), "at": _utc_now()})
                                break
                            except OSError:
                                pass
                    finally:
                        shutil.rmtree(reclaim_dir, ignore_errors=True)
                        shutil.rmtree(reclaim_guard, ignore_errors=True)
            if clock() >= deadline:
                raise Refused(
                    "OPEN_LOCK_TIMEOUT",
                    "could not acquire open lock %s within %.0fs" % (lock_dir, timeout_s),
                ) from None
            sleep(min(0.1, max(0.0, deadline - clock())))
    try:
        yield
    finally:
        shutil.rmtree(lock_dir, ignore_errors=True)


@contextlib.contextmanager
def _open_lock(
    lock_file: Path,
    *,
    timeout_s: float = 1200.0,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
):
    """Cross-process lock serializing pool check, usage attribution, study lock, `colab new`, shape check, and rate."""
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
    else:  # pragma: no cover - Windows directory lock with PID stale recovery; tested via _acquire_dir_lock
        with _acquire_dir_lock(lock_file.with_suffix(".lockdir"), timeout_s=timeout_s, clock=clock, sleep=sleep):
            yield


class _OpenInterrupted(BaseException):
    """Signal received between `colab new` and `Runner.run` handoff so cleanup stops and books spend first."""

    def __init__(self, signum: int):
        super().__init__("signal %d" % signum)
        self.signum = signum


@contextlib.contextmanager
def _open_phase_signals():
    """Keep signal handlers armed until Runner._arm_signals takes over, deferring hits during cleanup."""
    sig_state: dict[str, Any] = {"cleaning": False, "deferred": []}
    if threading.current_thread() is not threading.main_thread():
        yield sig_state
        return
    sigs = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGHUP"):
        sigs.append(signal.SIGHUP)
    old: dict[Any, Any] = {}

    def _handler(signum, frame):
        if sig_state["cleaning"]:
            sig_state["deferred"].append(signum)
            return
        raise _OpenInterrupted(signum)

    for s in sigs:
        old[s] = signal.signal(s, _handler)
    exc_in_flight = False
    try:
        yield sig_state
    except BaseException:
        exc_in_flight = True
        raise
    finally:
        for s, prev in old.items():
            signal.signal(s, prev)
        if not exc_in_flight and sig_state["deferred"]:
            raise RunInterrupted(sig_state["deferred"][0])


class LocalColab(Colab):
    """Offline directory-backed `Colab` stand-in for `--local-cpu` dry runs (never touches Google Colab)."""

    def __init__(self, content_dir: Path, **kw):
        super().__init__(("local-cpu",), **kw)
        self.content_dir = Path(content_dir)
        self.content_dir.mkdir(parents=True, exist_ok=True)
        self.active_sessions: dict[str, dict[str, str]] = {}
        self.confirmed_stops: list[str] = []

    def _translate_remote(self, remote: str) -> Path:
        root = self.content_dir.resolve()
        norm = remote.replace("\\", "/").strip()
        if norm == "/content":
            return self.content_dir
        if norm.startswith("/content/"):
            rel = norm[len("/content/"):]
            dest = (self.content_dir / rel).resolve()
            if not dest.is_relative_to(root):
                raise ValueError("remote path escapes /content: %s" % remote)
            return dest
        if Path(remote).is_absolute():
            dest = Path(remote).resolve()
            if not dest.is_relative_to(root):
                raise ValueError("remote path escapes content root: %s" % remote)
            return dest
        dest = (self.content_dir / norm.lstrip("/")).resolve()
        if not dest.is_relative_to(root):
            raise ValueError("remote path escapes content root: %s" % remote)
        return dest

    def sessions(self, timeout: float = 30.0) -> CliResult:
        lines = [
            "[%s] local | Hardware: %s | Shape: %s | Variant: %s"
            % (name, info["card"], info["shape"], "DEFAULT" if info["card"] == "CPU" else "GPU")
            for name, info in sorted(self.active_sessions.items())
        ]
        return CliResult(0, "\n".join(lines) + ("\n" if lines else ""))

    def new_session(self, session: str, *, card: str, shape: Optional[str] = None,
                    cli_flag: str = "--gpu", timeout: float = 180.0) -> CliResult:
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
        merged = dict(env or {})
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
        merged = dict(env or {})
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
            if src.is_dir():
                return CliResult(1, "", "is a directory: %s" % remote)
            dst = Path(local)
            dst.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(src, dst)
            return CliResult(0, "")
        finally:
            scrub_local(
                local, quarantine_dir=self.quarantine_dir, base_dir=self._base_dir,
                log=self.log, on_quarantine=self.on_quarantine,
            )

    def download_unscrubbed(self, session: str, remote: str, local: str, timeout: float = 120.0) -> CliResult:
        src = self._translate_remote(remote)
        if not src.exists():
            return CliResult(1, "", "no such file: %s" % remote)
        if src.is_dir():
            return CliResult(1, "", "is a directory: %s" % remote)
        dst = Path(local)
        dst.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(src, dst)
        return CliResult(0, "")

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
    runner_cls: type[Runner] = Runner,
    log: Callable[[str], None] = lambda s: None,
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
    if float(eff_max_rate) < card_spec.min_rate:
        log("rate note (not a refusal): max_rate %g/h is below %s catalog floor %g/h"
            % (float(eff_max_rate), card_spec.name, card_spec.min_rate))

    eff_cap = cap if cap is not None else spec.cap
    require_number("CAP_INVALID", "cap", eff_cap)

    eff_wall_s = wall_s if wall_s is not None else spec.wall_s
    require_number("WALL_INVALID", "wall_s", eff_wall_s)

    eff_margin = margin if margin is not None else spec.margin
    require_number("MARGIN_INVALID", "margin", eff_margin, inclusive=True)

    eff_resident = spec.resident if resident is None else bool(resident)
    if cleanup_bound_s is not None:
        eff_cleanup = cleanup_bound_s
    elif spec.cleanup_bound_s is not None:
        eff_cleanup = spec.cleanup_bound_s
    else:
        probe = runner_cls(Colab(), "probe", Path.cwd() / "probe_out")
        outputs_bound_s = cleanup_outputs_bound_s(spec, card_spec, download_timeout_s=probe.download_timeout_s)
        eff_cleanup = default_cleanup_bound_s(eff_resident, runner_cls, outputs_bound_s=outputs_bound_s)
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
    sealed: Optional[dict[str, Any]],
    purpose: str = "",
    line: str = "",
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
            "purpose": purpose,
            "line": line,
        },
        "purpose": purpose,
        "line": line,
        "card": card_spec.name,
        "card_class": card_spec.card_class,
        "shape": shape,
        "mode": mode,
        "status": "unknown",
        "outcome_bucket": None,
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
        "sealed": sealed,
        "collected_outputs": [],
        "salvaged_outputs": [],
        "missing_outputs": [],
        "output_errors": {},
        "quarantined": [],
    }


def _scrub_and_place_staged(
    stage_rel: Path,
    stage_root: Path,
    rel: str,
    local: Path,
    runner: Runner,
) -> Optional[str]:
    """Scrub a staged output file or directory under quarantine binding and move it into `local`."""
    with runner._bind_quarantine():
        try:
            scrub_local(
                stage_rel, quarantine_dir=runner.quarantine_dir, base_dir=stage_root,
                log=runner.log, on_quarantine=runner._record_quarantine,
            )
        except (BinaryDownloadRefused, QuarantineEscape) as exc:
            if runner._download_refusal is None:
                runner._download_refusal = exc
            return exc.code
        except OSError as exc:
            runner.log("output %r scrub failed: %s: %s" % (rel, type(exc).__name__, exc))
            code = "OUTPUT_SCRUB_FAILED"
            return code
    if not stage_rel.exists():
        code = "OUTPUT_SCRUB_FAILED"
        return code
    if local.is_dir():
        shutil.rmtree(local)
    elif local.exists():
        local.unlink()
    local.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(stage_rel), str(local))
    return None


def _finalize_staged_file(
    assembled: Path,
    stage_root: Path,
    rel: str,
    local: Path,
    runner: Runner,
    *,
    bucket: str = "collected",
) -> Optional[str]:
    """Place a single assembled output file into `stage_root/bucket/rel`, scrub/quarantine, then move to `local`."""
    stage_rel = stage_root / bucket / rel
    stage_rel.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(assembled), str(stage_rel))
    return _scrub_and_place_staged(stage_rel, stage_root, rel, local, runner)


def _extract_dir_tar(
    tmp_tar: Path,
    stage_root: Path,
    rel: str,
    local: Path,
    runner: Runner,
    *,
    bucket: str = "collected",
) -> Optional[str]:
    """Safely extract `tmp_tar` into `stage_root/bucket/rel` (`filter='data'`), scrub files individually, then move."""
    if not tmp_tar.is_file():
        code = "OUTPUT_TAR_CORRUPT"
        return code
    stage_rel = stage_root / bucket / rel
    stage_rel.mkdir(parents=True, exist_ok=True)
    try:
        try:
            with tarfile.open(tmp_tar, "r:") as tf:
                tf.extractall(path=stage_rel, filter="data")
        except tarfile.FilterError as exc:
            runner.log("directory output %s refused by tar filter: %s" % (rel, exc))
            code = "OUTPUT_TAR_UNSAFE"
            return code
        except (tarfile.TarError, OSError, EOFError) as exc:
            runner.log("directory output %s tar unreadable: %s" % (rel, exc))
            code = "OUTPUT_TAR_CORRUPT"
            return code
    finally:
        tmp_tar.unlink(missing_ok=True)
    return _scrub_and_place_staged(stage_rel, stage_root, rel, local, runner)


def _hash_file(path: Path) -> tuple[int, str]:
    size = 0
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        while True:
            blk = fh.read(_STREAM_BUF_BYTES)
            if not blk:
                break
            size += len(blk)
            digest.update(blk)
    return size, digest.hexdigest()


def _parse_tar_manifest(out_text: str) -> tuple[bool, Optional[dict[str, Any]]]:
    line = next((ln for ln in (out_text or "").splitlines() if ln.startswith("CR_TAR_MANIFEST ")), None)
    if line is None:
        return False, None
    try:
        obj = json.loads(line[len("CR_TAR_MANIFEST "):])
    except ValueError:
        return True, None
    if not isinstance(obj, dict):
        return True, None
    kind = obj.get("kind")
    size = obj.get("size")
    sha = obj.get("sha256")
    parts = obj.get("parts")
    if (
        kind not in ("dir", "file")
        or not isinstance(size, int) or isinstance(size, bool) or size < 0
        or not isinstance(sha, str) or not sha
        or not isinstance(parts, list)
    ):
        return True, None
    if not parts:
        remote = obj.get("remote")
        if not isinstance(remote, str) or not remote:
            return True, None
        return True, obj
    for part in parts:
        if not isinstance(part, dict):
            return True, None
        p_path, p_size, p_sha = part.get("path"), part.get("size"), part.get("sha256")
        if (
            not isinstance(p_path, str) or not p_path
            or not isinstance(p_size, int) or isinstance(p_size, bool) or p_size < 0
            or not isinstance(p_sha, str) or not p_sha
        ):
            return True, None
    return True, obj


def _download_manifest_payload(
    runner: Runner,
    manifest: dict[str, Any],
    stage_root: Path,
    rel: str,
    assembled: Path,
    next_timeout: Optional[Callable[[], Optional[float]]],
) -> tuple[Optional[str], bool]:
    """Download single or multi-part payload from VM, verify per-part + whole SHA-256, and reassemble."""
    parts = manifest["parts"]
    if not parts:
        dl_t = runner.download_timeout_s if next_timeout is None else next_timeout()
        if dl_t is None:
            return None, False
        dl_res = runner._clean(runner.colab.download_unscrubbed(runner.session, manifest["remote"], str(assembled), dl_t))
        if not dl_res.ok:
            return None, True
        got_size, got_sha = _hash_file(assembled)
        if (got_size, got_sha) != (manifest["size"], manifest["sha256"]):
            runner.log("output %s payload mismatch (expected %d/%s, got %d/%s)"
                       % (rel, manifest["size"], manifest["sha256"][:12], got_size, got_sha[:12]))
            code = "OUTPUT_TAR_CORRUPT"
            return code, True
        return "", True

    part_local = stage_root / "part.bin"
    whole_size = 0
    whole_sha = hashlib.sha256()
    with assembled.open("wb") as out_fh:
        for part in parts:
            dl_t = runner.download_timeout_s if next_timeout is None else next_timeout()
            if dl_t is None:
                return None, False
            dl_part = runner._clean(runner.colab.download_unscrubbed(runner.session, part["path"], str(part_local), dl_t))
            if isinstance(runner.colab, LocalColab):
                try:
                    runner.colab._translate_remote(part["path"]).unlink(missing_ok=True)
                except Exception:
                    pass
            if not dl_part.ok or not part_local.is_file():
                part_local.unlink(missing_ok=True)
                return None, True
            p_size = 0
            p_sha = hashlib.sha256()
            with part_local.open("rb") as in_fh:
                while True:
                    blk = in_fh.read(_STREAM_BUF_BYTES)
                    if not blk:
                        break
                    out_fh.write(blk)
                    p_size += len(blk)
                    p_sha.update(blk)
                    whole_size += len(blk)
                    whole_sha.update(blk)
            part_local.unlink(missing_ok=True)
            if (p_size, p_sha.hexdigest()) != (part["size"], part["sha256"]):
                runner.log("output %s part %s checksum mismatch" % (rel, part["path"]))
                code = "OUTPUT_TAR_CORRUPT"
                return code, True
    if (whole_size, whole_sha.hexdigest()) != (manifest["size"], manifest["sha256"]):
        runner.log("output %s assembled size/sha256 mismatch" % rel)
        code = "OUTPUT_TAR_CORRUPT"
        return code, True
    return "", True


def _download_declared_outputs(
    runner: Runner,
    paths: Sequence[str],
    dest_dir: Path,
    *,
    content: str,
    job: str,
    next_timeout: Optional[Callable[[], Optional[float]]] = None,
    next_exec_timeout: Optional[Callable[[], Optional[float]]] = None,
    output_errors: Optional[dict[str, str]] = None,
    download_parts: int = 1,
    chunk_bytes: int = DEFAULT_DOWNLOAD_CHUNK_BYTES,
) -> tuple[list[str], bool]:
    transferred: list[str] = []
    completed = True
    base = content.rstrip("/\\") or "/"
    work_prefix = "%s/%s_work" % (base, job)
    out_prefix = "%s/%s_out" % (base, job)
    try_direct = int(download_parts) == 1 and int(chunk_bytes) == DEFAULT_DOWNLOAD_CHUNK_BYTES
    for rel in paths:
        local = dest_dir / rel
        local.parent.mkdir(parents=True, exist_ok=True)
        if try_direct:
            dl_t = runner.download_timeout_s if next_timeout is None else next_timeout()
            if dl_t is None:
                completed = False
                break
            res = runner.download("%s/%s" % (work_prefix, rel), local, timeout=dl_t)
            if not res.ok:
                dl_t2 = runner.download_timeout_s if next_timeout is None else next_timeout()
                if dl_t2 is None:
                    completed = False
                    break
                res = runner.download("%s/%s" % (out_prefix, rel), local, timeout=dl_t2)
            if res.ok and local.is_file():
                transferred.append(rel)
                continue

        exec_t = DEFAULT_TAR_TIMEOUT_S if next_exec_timeout is None else next_exec_timeout()
        if exec_t is None:
            completed = False
            break
        tar_res = runner._clean(runner.colab.exec_cell(
            runner.session, vm_lib_source() + "\n" + _TAR_OUTPUT_CELL_SRC,
            runner._env({"CR_TAR_REL": rel, "CR_TAR_CHUNK_BYTES": str(int(chunk_bytes))}), exec_t,
        ))
        if not (tar_res.ok and "CR_TAR_READY" in tar_res.out):
            continue
        has_manifest, manifest = _parse_tar_manifest(tar_res.out)
        if has_manifest and manifest is None:
            if output_errors is not None:
                code = "OUTPUT_TAR_CORRUPT"
                output_errors[rel] = code
            continue
        with tempfile.TemporaryDirectory(prefix="colab-dir-tar-") as tmp_stage:
            stage_root = Path(tmp_stage)
            assembled = stage_root / "archive.tar"
            if manifest is not None:
                status, ok_budget = _download_manifest_payload(
                    runner, manifest, stage_root, rel, assembled, next_timeout,
                )
                if not ok_budget:
                    completed = False
                    break
                if status != "":
                    if status and output_errors is not None:
                        output_errors[rel] = status
                    continue
                if manifest["kind"] == "file":
                    err_code = _finalize_staged_file(assembled, stage_root, rel, local, runner, bucket=dest_dir.name)
                else:
                    err_code = _extract_dir_tar(assembled, stage_root, rel, local, runner, bucket=dest_dir.name)
            else:
                dl_t3 = runner.download_timeout_s if next_timeout is None else next_timeout()
                if dl_t3 is None:
                    completed = False
                    break
                dl_tar = runner._clean(runner.colab.download_unscrubbed(
                    runner.session, "%s/%s.tar" % (out_prefix, rel), str(assembled), dl_t3,
                ))
                if not dl_tar.ok:
                    continue
                err_code = _extract_dir_tar(assembled, stage_root, rel, local, runner, bucket=dest_dir.name)
            if err_code is None:
                transferred.append(rel)
            elif output_errors is not None:
                output_errors[rel] = err_code
    return transferred, completed


def _card_mismatch_seen(out_dir: Path, job: str) -> bool:
    for p in (out_dir / ("%s.run.out" % job), out_dir / "collected" / "setup.log", out_dir / "salvage" / "setup.log"):
        if p.is_file() and "CR_CARD_MISMATCH:" in p.read_text(encoding="utf-8", errors="replace"):
            return True
    for p in (out_dir / "vm_card.json", out_dir / "salvage" / "vm_card.json", out_dir / "collected" / "vm_card.json"):
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


def _pool_check(
    rows: Sequence[Mapping[str, Optional[str]]],
    *,
    session: str,
    card_spec: CardSpec,
    shape: Optional[str],
) -> None:
    if any(r.get("session") == session for r in rows):
        raise Refused("SESSION_ALREADY_OPEN", "Colab session %r is already active" % session)
    if len(rows) >= TOTAL_SESSION_CEILING:
        raise Refused(
            "POOL_CEILING_REACHED",
            "account has %d active sessions (ceiling %d)" % (len(rows), TOTAL_SESSION_CEILING),
        )
    want_key = pool_key_for_card(card_spec, shape)
    same_card = sum(1 for r in rows if pool_key_for_session_row(r) == want_key)
    if card_spec.pool_ceiling and same_card >= card_spec.pool_ceiling:
        raise Refused(
            "POOL_CEILING_REACHED",
            "account already has %d active %s sessions (pool ceiling %d)"
            % (same_card, want_key, card_spec.pool_ceiling),
        )


def _read_sessions_strict(client: Colab) -> list[dict[str, Optional[str]]]:
    sess_res = client.sessions()
    if not sess_res.ok:
        msg = scrub((sess_res.err or sess_res.out or "rc=%s" % sess_res.rc).strip())
        raise Refused("SESSIONS_READ_FAILED", "could not read active `colab sessions` before open: %s" % msg)
    text = (sess_res.out or "").strip()
    if not text or "no active sessions" in text.lower():
        return []
    rows = parse_sessions_output(text)
    if not rows or any(not r.get("hardware") for r in rows):
        raise Refused("SESSIONS_READ_FAILED", "could not parse `colab sessions` rows reliably")
    return rows


def _read_pre_open_usage(
    colab: Colab,
    *,
    card_spec: CardSpec,
    log: Callable[[str], None] = lambda s: None,
) -> tuple[Optional[float], Optional[int], Optional[float]]:
    res = colab.usage()
    if not res.ok:
        log("pre-open `colab usage` failed (rc=%s); proceeding without pre-open baseline" % res.rc)
        return None, None, None
    try:
        parsed = lg.parse_usage_text(res.out, "cpu" if card_spec.card_class == "cpu" else "gpu",
                                     usage_rc=res.rc, session_opened=False)
    except lg.RateUnread as exc:
        parsed = dict(exc.partial, ok=True, error=None)
    if not parsed.get("ok"):
        log("pre-open `colab usage` unreadable (%s); proceeding without pre-open baseline"
            % (parsed.get("error") or "parse failed"))
        return None, None, None
    pre_rate = parsed.get("rate_measured")
    pre_assign = parsed.get("active_assignments")
    bal = parsed.get("balance")
    if pre_assign is None and (card_spec.card_class == "gpu" or pre_rate is not None):
        log("pre-open `colab usage` reported no Active assignments count; proceeding without pre-open baseline")
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
    clock: Callable[[], float] = time.monotonic,
) -> tuple[bool, float]:
    if already_stopped is None:
        stopped = runner.stop_session()
        stopped_at = clock()
    else:
        stopped_at = clock()
        stopped = bool(already_stopped)
    outcome["session_stopped_confirmed"] = bool(stopped and not (out_dir / "session_open.json").exists())
    if outcome["session_stopped_confirmed"]:
        outcome["closed_at"] = _utc_now()
        try:
            sess = colab.sessions()
            if sess.ok:
                rows = parse_sessions_output(sess.out)
                outcome["session_gone_confirmed"] = not any(r.get("session") == session for r in rows)
        except Exception:
            pass
    return bool(outcome["session_stopped_confirmed"]), stopped_at


def _read_end_balance(colab: Colab) -> Optional[float]:
    try:
        res = colab.usage()
    except Exception:
        return None
    if not res.ok:
        return None
    try:
        parsed = lg.parse_usage_text(res.out, "cpu", usage_rc=res.rc, session_opened=False)
    except lg.RateUnread as exc:  # pragma: no cover - cpu class does not raise RateUnread
        return exc.partial.get("balance")
    return parsed.get("balance") if parsed.get("ok") else None


def reconcile_stopped_outcome(
    out_dir: Path,
    colab: Colab,
    session: str,
    *,
    closed_dt: Optional[datetime] = None,
    marker_mtime: Optional[float] = None,
) -> Optional[dict[str, Any]]:
    """Re-book `outcome.json` (or synthesize one for a killed orphan from `window_meta.json`) up to confirmed close."""
    out_root = Path(out_dir)
    outcome_path = out_root / "outcome.json"
    meta = _load_opt_json(out_root / "window_meta.json")
    now_dt = (closed_dt or datetime.now(timezone.utc)).astimezone(timezone.utc)
    now_iso = _utc_now(now_dt)

    if outcome_path.is_file():
        try:
            outcome = json.loads(outcome_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        if not isinstance(outcome, dict):
            return None

        outcome["session_stopped_confirmed"] = True
        outcome["closed_at"] = now_iso

        start_iso = outcome.get("opened_at") or (meta or {}).get("started_at")
        if start_iso:
            try:
                opened_dt = datetime.strptime(str(start_iso), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
                reconciled_elapsed = max(float(outcome.get("elapsed_s") or 0.0), (now_dt - opened_dt).total_seconds())
                priced_rate = float(outcome.get("priced_rate") or outcome.get("max_rate") or (meta or {}).get("max_rate") or 0.0)
                outcome["elapsed_s"] = round(max(0.0, reconciled_elapsed), 3)
                outcome["spend_ccu"] = round((priced_rate * max(0.0, reconciled_elapsed)) / 3600.0, 6)
                basis = str(outcome.get("spend_basis") or "max_rate_x_elapsed")
                if basis == "not_opened":
                    outcome["spend_basis"] = "max_rate_x_elapsed_reconciled"
                elif not basis.endswith("_reconciled"):
                    outcome["spend_basis"] = basis + "_reconciled"
            except (TypeError, ValueError):
                pass
    else:
        if meta is None:
            return None
        start_dt: Optional[datetime] = None
        if meta.get("started_at"):
            try:
                start_dt = datetime.strptime(str(meta["started_at"]), "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
            except (TypeError, ValueError):
                start_dt = None
        if start_dt is None:
            fb_mtime = marker_mtime
            if fb_mtime is None:
                for cand_name in ("session_open.json", "window_meta.json"):
                    cand_p = out_root / cand_name
                    if cand_p.is_file():
                        fb_mtime = cand_p.stat().st_mtime
                        break
            start_dt = (
                datetime.fromtimestamp(float(fb_mtime), tz=timezone.utc) if fb_mtime is not None else now_dt
            )
        elapsed_raw = max(0.0, (now_dt - start_dt).total_seconds())
        max_rate = float(meta.get("max_rate") or 0.0)
        card_name = str(meta.get("card") or "CPU")
        card_class = str(meta.get("card_class") or ("cpu" if card_name == "CPU" else "gpu"))
        outcome = {
            "schema_version": 1,
            "window_id": meta.get("window_id") or ("%s-orphan" % _slug(session)),
            "session": session,
            "project": meta.get("project") or {"repo": "", "study": "", "job": "job", "description": ""},
            "card": card_name,
            "card_class": card_class,
            "shape": meta.get("shape"),
            "mode": meta.get("mode") or "live",
            "status": "orphaned",
            "exit_code": 2,
            "run_rc": None,
            "opened": True,
            "opened_at": _utc_now(start_dt),
            "closed_at": now_iso,
            "elapsed_s": round(elapsed_raw, 3),
            "cap_ccu": meta.get("cap_ccu"),
            "margin_ccu": meta.get("margin_ccu"),
            "cleanup_bound_s": meta.get("cleanup_bound_s"),
            "max_rate": max_rate,
            "priced_rate": round(max_rate, 6),
            "window_rate_stable": None,
            "spend_basis": "max_rate_x_elapsed_orphan_reconciled",
            "spend_ccu": round((max_rate * elapsed_raw) / 3600.0, 6),
            "account_balance": {
                "start": None,
                "end": _read_end_balance(colab),
                "drop": None,
                "used_for_pricing": False,
            },
            "session_stopped_confirmed": True,
            "session_gone_confirmed": None,
            "refusal": error_record("UNEXPECTED_ERROR", "launcher exited before writing outcome.json; booked by stop"),
            "cap_trip": None,
            "input_files": [],
            "sealed": None,
            "collected_outputs": [],
            "salvaged_outputs": [],
            "missing_outputs": [],
            "output_errors": {},
            "quarantined": [],
        }

    try:
        sess = colab.sessions()
        if sess.ok:
            rows = parse_sessions_output(sess.out)
            outcome["session_gone_confirmed"] = not any(r.get("session") == session for r in rows)
    except Exception:
        pass
    outcome["outcome_bucket"] = classify_window_outcome(outcome)
    write_json_atomic(outcome_path, outcome)

    proj = outcome.get("project") or {}
    study = str(proj.get("study") or "")
    mode_val = str(outcome.get("mode") or (meta or {}).get("mode") or "live")
    s_dir, l_file = _state_paths(
        (meta or {}).get("state_dir"),
        (meta or {}).get("study_ledger"),
        live=(mode_val == "live"),
    )
    if s_dir is not None and study and outcome.get("card_class") == "gpu" and mode_val == "live" and l_file is not None:
        lock_p = _default_open_lock_path((meta or {}).get("open_lock"))
        with _open_lock(lock_p):
            ledger = StudyLedger(l_file)
            if not outcome.get("opened"):
                ledger.release_reservation(study, window_id=str(outcome.get("window_id") or ""))
            ledger.record_window(study, outcome)
    return outcome


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
    allow_sealed: bool = False,
    actual_host: str = DEFAULT_SEALED_HOST,
    state_dir: Optional[Path | str] = None,
    study_ledger: Optional[Path | str] = None,
    open_lock: Optional[Path | str] = None,
    purpose: Optional[str] = None,
    line: Optional[str] = None,
    post_open_polls: int = 6,
    rate_poll_interval_s: float = 5.0,
    shape_check_attempts: int = 3,
    colab: Optional[Colab] = None,
    runner_cls: type[Runner] = Runner,
    clock: Callable[[], float] = time.monotonic,
    sleep: Callable[[float], None] = time.sleep,
    log: Callable[[str], None] = lambda s: None,
    trace: Callable[[str], None] = lambda s: None,
) -> dict[str, Any]:
    """Validate, stage, plan (`--dry`), or execute (`--local-cpu` / live) one Colab window end-to-end."""
    _refuse_native_windows()
    out_path = _check_empty_out_dir(Path(out_dir))
    spec = WindowSpec.from_file(spec_path)
    chunk_bytes = _download_chunk_bytes()
    (
        card_spec, clean_shape, eff_max_rate, eff_cap, eff_wall_s, eff_margin, eff_cleanup, eff_resident,
    ) = _apply_overrides(
        spec, card=card, shape=shape, cap=cap, max_rate=max_rate, wall_s=wall_s,
        cleanup_bound_s=cleanup_bound_s, margin=margin, resident=resident, local_cpu=local_cpu,
        runner_cls=runner_cls, log=log,
    )
    eff_purpose = (purpose.strip() if purpose and purpose.strip() else spec.purpose) or "%s/%s" % (spec.study, spec.job)
    eff_line = (line.strip() if line and line.strip() else spec.line) or spec.study
    merged_env = resolve_kit_env(list(spec.env) + list(extra_env))
    _refuse_reserved_env(merged_env)

    live_mode = not dry and not local_cpu
    s_dir, l_file = _state_paths(state_dir, study_ledger, live=live_mode)
    breaker = CircuitBreaker(s_dir / "card_refusals.json") if s_dir is not None else None
    ledger = StudyLedger(l_file) if l_file is not None else None
    utc_day = utc_day_str()
    if ledger is not None and card_spec.card_class == "gpu" and live_mode:
        ledger.check_card(spec.study, card_spec.name)
    if breaker is not None and live_mode:
        breaker.check(card_spec.name, utc_day)

    probe = runner_cls(Colab(), "probe", out_path)
    own_bound_s = probe.own_cleanup_bound_s(eff_resident)
    outputs_bound_s = cleanup_outputs_bound_s(spec, card_spec, download_timeout_s=probe.download_timeout_s)
    min_bound = own_bound_s + outputs_bound_s
    if eff_cleanup < min_bound:
        raise Refused(
            "CLEANUP_BOUND_TOO_SMALL",
            "cleanup_bound_s %g is below required cleanup bound %g s (runner own %g s + declared outputs %g s)"
            % (eff_cleanup, min_bound, own_bound_s, outputs_bound_s),
        )
    guard_log = lambda msg: log(scrub(msg))
    guard = SpendGuard(max_rate=eff_max_rate, cap=eff_cap, margin=eff_margin, cleanup_bound_s=eff_cleanup, log=guard_log)
    eff_session = session or default_session_name(spec, card_spec.name)
    window_id = "%s-%s" % (_slug(spec.job), uuid.uuid4().hex[:8])

    open_bound_s = open_phase_bound_s(
        card_spec,
        local_cpu=local_cpu,
        shape_attempts=shape_check_attempts,
        post_open_polls=post_open_polls,
        rate_poll_interval_s=rate_poll_interval_s,
    )
    budget = compute_launch_budget(guard, open_bound_s, outputs_bound_s=outputs_bound_s)

    # Validate & stage inputs BEFORE creating persistent out_dir files on refusal
    with tempfile.TemporaryDirectory(prefix="cr-stage-") as stage_tmp:
        tmp_bundle = Path(stage_tmp) / ("%s_inputs.tar.gz" % spec.job)
        staged_inputs, sealed_record = stage_inputs(
            spec,
            tmp_bundle,
            actual_card=card_spec.name,
            actual_host=actual_host,
            live=live_mode,
            local_cpu=local_cpu,
            cli_allow_sealed=allow_sealed,
        )
        out_path.mkdir(parents=True, exist_ok=True)
        staged_dir = out_path / "staged"
        bundle_name = "%s_inputs.tar.gz" % spec.job
        script_name = "%s_job.sh" % spec.job
        bundle_path = staged_dir / bundle_name
        script_path = staged_dir / script_name
        staged_dir.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(tmp_bundle, bundle_path)

    script_text = render_vm_script(spec, card_spec, bundle_filename=bundle_name)
    script_path.write_text(script_text, encoding="utf-8", newline="\n")

    if dry:
        plan = {
            "schema_version": 1,
            "mode": "dry_run",
            "window_id": window_id,
            "session": eff_session,
            "project": {
                "repo": spec.repo, "study": spec.study, "job": spec.job, "description": spec.description,
                "purpose": eff_purpose, "line": eff_line,
            },
            "purpose": eff_purpose,
            "line": eff_line,
            "card": card_spec.name,
            "card_class": card_spec.card_class,
            "shape": clean_shape,
            "min_vram_mib": spec.vram_floor_for(card_spec),
            "wall_s": eff_wall_s,
            "resident": eff_resident,
            "budget": budget,
            "budget_summary": format_launch_budget(budget),
            "staged_files": [asdict(s) for s in staged_inputs],
            "sealed": sealed_record,
            "script": str(script_path),
            "bundle": str(bundle_path),
            "outputs": {
                "collect": list(spec.collect_outputs),
                "salvage": list(spec.salvage_outputs),
                "download_parts": spec.download_parts,
            },
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
    vm_content = str(client.content_dir.resolve()) if isinstance(client, LocalColab) else "/content"
    vm_content_base = vm_content.rstrip("/\\") or "/"
    remote_bundle = "%s/%s" % (vm_content_base, bundle_name)
    remote_script = "%s/%s" % (vm_content_base, script_name)

    outcome = _base_outcome(
        window_id=window_id, session=eff_session, spec=spec, card_spec=card_spec,
        shape=clean_shape, mode=mode_name, cap_ccu=eff_cap, margin_ccu=eff_margin,
        cleanup_bound_s=eff_cleanup, max_rate=eff_max_rate, staged=staged_inputs,
        sealed=sealed_record, purpose=eff_purpose, line=eff_line,
    )
    meta = {
        "schema_version": 1,
        "window_id": window_id,
        "launcher_pid": os.getpid(),
        "session": eff_session,
        "project": outcome["project"],
        "purpose": eff_purpose,
        "line": eff_line,
        "card": card_spec.name,
        "card_class": card_spec.card_class,
        "shape": clean_shape,
        "mode": mode_name,
        "started_at": _utc_now(),
        "wall_s": eff_wall_s,
        "cap_ccu": eff_cap,
        "margin_ccu": eff_margin,
        "cleanup_bound_s": eff_cleanup,
        "max_rate": eff_max_rate,
        "state_dir": str(s_dir) if s_dir is not None else None,
        "study_ledger": str(l_file) if l_file is not None else None,
        "open_lock": str(_default_open_lock_path(open_lock)),
    }
    write_json_atomic(out_path / "window_meta.json", meta)

    runner = runner_cls(
        client, eff_session, out_path, job=spec.job, content=vm_content, poll_s=spec.poll_s,
        pre_opened=True, clock=clock, sleep=sleep, log=log, trace=trace,
    )
    lock_path = _default_open_lock_path(open_lock)
    t_open_monotonic: Optional[float] = None
    marker_written = False
    lock_held = False

    @contextlib.contextmanager
    def _with_state_lock():
        if lock_held:
            yield
        else:
            with _open_lock(lock_path, clock=clock, sleep=sleep):
                yield

    def _open_time_left() -> tuple[float, str]:
        elapsed = max(0.0, clock() - t_open_monotonic) if t_open_monotonic is not None else 0.0
        wall_left = lg.evaluate_wall_guard(elapsed_s=elapsed, wall_s=eff_wall_s, grace_s=0.0)["remaining_s"]
        cap_left = guard.seconds_left(elapsed)
        return (cap_left, "cap") if cap_left < wall_left else (wall_left, "wall")

    def _record_window_outcome(stopped_ok: bool) -> None:
        outcome["outcome_bucket"] = classify_window_outcome(outcome)
        if not live_mode or ledger is None or card_spec.card_class != "gpu":
            return
        with _with_state_lock():
            if not outcome["opened"] and stopped_ok:
                ledger.release_reservation(spec.study, window_id=window_id)
            ledger.record_window(spec.study, outcome)

    def _trip_open_bound(sig_state: dict[str, Any]) -> Optional[dict[str, Any]]:
        if t_open_monotonic is None:
            return None
        elapsed = max(0.0, clock() - t_open_monotonic)
        if lg.evaluate_wall_guard(elapsed_s=elapsed, wall_s=eff_wall_s, grace_s=0.0)["tripped"]:
            sig_state["cleaning"] = True
            stopped_ok, stopped_at = _finalize_close(
                client, eff_session, out_path, outcome, runner, already_stopped=None, clock=clock
            )
            _book_spend(outcome, card_spec=card_spec, guard=guard, elapsed_s=stopped_at - t_open_monotonic,
                        bal_end=_read_end_balance(client))
            outcome["status"] = "wall"
            outcome["exit_code"] = 2
            outcome["refusal"] = error_record("CAP_CEILING_REACHED", "wall %.0fs reached during open phase" % eff_wall_s)
            write_json_atomic(out_path / "outcome.json", outcome)
            _record_window_outcome(stopped_ok)
            return outcome
        trip = guard.check(elapsed)
        if trip or guard.seconds_left(elapsed) <= 1e-3:
            sig_state["cleaning"] = True
            stopped_ok, stopped_at = _finalize_close(
                client, eff_session, out_path, outcome, runner, already_stopped=None, clock=clock
            )
            _book_spend(outcome, card_spec=card_spec, guard=guard, elapsed_s=stopped_at - t_open_monotonic,
                        bal_end=_read_end_balance(client))
            outcome["status"] = "cap"
            outcome["exit_code"] = 2
            outcome["cap_trip"] = (
                cap_trip_record(scrub(trip))
                if trip
                else error_record("CAP_CEILING_REACHED", "the cap was reached during open phase")
            )
            write_json_atomic(out_path / "outcome.json", outcome)
            _record_window_outcome(stopped_ok)
            return outcome
        return None

    def _next_call_timeout(default_s: float, sig_state: dict[str, Any]) -> tuple[Optional[float], Optional[dict[str, Any]]]:
        tripped = _trip_open_bound(sig_state)
        if tripped is not None:
            return None, tripped
        left, _ = _open_time_left()
        if left < MIN_CALL_S:
            if left > 0:
                sleep(left)
            tripped = _trip_open_bound(sig_state)
            if tripped is not None:
                return None, tripped
            sig_state["cleaning"] = True
            stopped_ok, stopped_at = _finalize_close(
                client, eff_session, out_path, outcome, runner, already_stopped=None, clock=clock
            )
            _book_spend(outcome, card_spec=card_spec, guard=guard, elapsed_s=stopped_at - t_open_monotonic,
                        bal_end=_read_end_balance(client))
            outcome["status"] = "cap"
            outcome["exit_code"] = 2
            outcome["cap_trip"] = error_record("CAP_CEILING_REACHED", "the cap was reached during open phase")
            write_json_atomic(out_path / "outcome.json", outcome)
            _record_window_outcome(stopped_ok)
            return None, outcome
        return min(float(default_s), left), None

    with _open_phase_signals() as sig_state:
        try:
            with _open_lock(lock_path, clock=clock, sleep=sleep):
                lock_held = True
                try:
                    rows = _read_sessions_strict(client)
                    _pool_check(rows, session=eff_session, card_spec=card_spec, shape=clean_shape)
                    pre_rate, pre_assign, bal_start = _read_pre_open_usage(client, card_spec=card_spec, log=log)
                    outcome["account_balance"]["start"] = bal_start
                    guard = SpendGuard(
                        max_rate=eff_max_rate, cap=eff_cap, margin=eff_margin, cleanup_bound_s=eff_cleanup,
                        pre_open_rate=pre_rate, pre_open_assignments=pre_assign, balance=bal_start, log=guard_log,
                    )
                    warn = neighbour_warning(pre_assign or 0)
                    if warn:
                        log(scrub(warn))

                    if breaker is not None and live_mode:
                        breaker.check(card_spec.name, utc_day)
                    if ledger is not None and card_spec.card_class == "gpu" and live_mode:
                        ledger.reserve_card(spec.study, card_spec.name, session=eff_session, window_id=window_id)

                    write_json_atomic(
                        out_path / "session_open.json", {"session": eff_session, "job": spec.job, "content": vm_content}
                    )
                    marker_written = True
                    # Count wall and cap spend from immediately BEFORE `colab new` (Colab bills from assignment).
                    t_open_monotonic = clock()
                    outcome["opened_at"] = _utc_now()
                    new_t_lim, tripped_out = _next_call_timeout(180.0, sig_state)
                    if tripped_out is not None:
                        return tripped_out
                    new_res = client.new_session(
                        eff_session, card=card_spec.cli_gpu, shape=clean_shape,
                        cli_flag=card_spec.cli_flag, timeout=float(new_t_lim),
                    )
                    if not new_res.ok:
                        if float(new_t_lim) < 180.0:
                            tripped_out = _trip_open_bound(sig_state)
                            if tripped_out is not None:
                                return tripped_out
                        sig_state["cleaning"] = True
                        msg = scrub((new_res.err or new_res.out or "rc=%s" % new_res.rc).strip())
                        if breaker is not None:
                            breaker.record_refusal(card_spec.name, reason="SESSION_OPEN_FAILED: %s" % msg,
                                                   session=eff_session, day=utc_day)
                        stopped_ok, stopped_at = _finalize_close(
                            client, eff_session, out_path, outcome, runner, already_stopped=None, clock=clock
                        )
                        _book_spend(outcome, card_spec=card_spec, guard=guard,
                                    elapsed_s=stopped_at - t_open_monotonic, bal_end=_read_end_balance(client))
                        outcome["status"] = "open_failed"
                        outcome["exit_code"] = 2
                        outcome["refusal"] = error_record("SESSION_OPEN_FAILED", "colab new failed: %s" % msg)
                        write_json_atomic(out_path / "outcome.json", outcome)
                        _record_window_outcome(stopped_ok)
                        return outcome

                    outcome["opened"] = True

                    if card_spec.card_class == "gpu":
                        expected_shape = shape_row_label(clean_shape or "standard")
                        observed_shape: Optional[str] = None
                        confirmed_shape_mismatch = False
                        for attempt in range(1, max(1, int(shape_check_attempts)) + 1):
                            sess_t_lim, tripped_out = _next_call_timeout(30.0, sig_state)
                            if tripped_out is not None:
                                return tripped_out
                            after_open = client.sessions(timeout=float(sess_t_lim))
                            rows = parse_sessions_output(after_open.out) if after_open.ok else []
                            row = next((r for r in rows if r.get("session") == eff_session), None)
                            observed_shape = row.get("shape") if row else None
                            if after_open.ok and observed_shape == expected_shape:
                                break
                            if after_open.ok and observed_shape is not None and observed_shape != expected_shape:
                                confirmed_shape_mismatch = True
                                break
                            if attempt < int(shape_check_attempts):
                                left_s, _ = _open_time_left()
                                wait_s = min(2.0 * attempt, max(0.0, left_s))
                                if wait_s > 0:
                                    sleep(wait_s)

                        if confirmed_shape_mismatch:
                            sig_state["cleaning"] = True
                            if breaker is not None:
                                breaker.record_refusal(
                                    card_spec.name,
                                    reason="SHAPE_MISMATCH: expected %s got %s" % (expected_shape, observed_shape),
                                    session=eff_session, day=utc_day,
                                )
                            stopped_ok, stopped_at = _finalize_close(
                                client, eff_session, out_path, outcome, runner, already_stopped=None, clock=clock
                            )
                            _book_spend(outcome, card_spec=card_spec, guard=guard,
                                        elapsed_s=stopped_at - t_open_monotonic, bal_end=_read_end_balance(client))
                            outcome["status"] = "shape_mismatch"
                            outcome["exit_code"] = 2
                            outcome["refusal"] = error_record(
                                "SHAPE_MISMATCH",
                                "expected Shape: %s for %s, got %r" % (expected_shape, card_spec.name, observed_shape),
                            )
                            write_json_atomic(out_path / "outcome.json", outcome)
                            _record_window_outcome(stopped_ok)
                            return outcome

                        if observed_shape != expected_shape:
                            log("post-open `colab sessions` shape check inconclusive (got %r); continuing"
                                % (observed_shape,))

                        tripped_out = _trip_open_bound(sig_state)
                        if tripped_out is not None:
                            return tripped_out

                        for idx in range(max(1, int(post_open_polls))):
                            u_t_lim, tripped_out = _next_call_timeout(60.0, sig_state)
                            if tripped_out is not None:
                                return tripped_out
                            u = client.usage(timeout=float(u_t_lim))
                            guard.fold_usage(u.out if u.ok else u.err, rc=u.rc, epoch=clock(), at=_utc_now())
                            decision = guard.decision()
                            if decision == "above_max":
                                sig_state["cleaning"] = True
                                stopped_ok, stopped_at = _finalize_close(
                                    client, eff_session, out_path, outcome, runner, already_stopped=None, clock=clock
                                )
                                _book_spend(outcome, card_spec=card_spec, guard=guard,
                                            elapsed_s=stopped_at - t_open_monotonic, bal_end=_read_end_balance(client))
                                outcome["status"] = "cap"
                                outcome["exit_code"] = 2
                                outcome["cap_trip"] = error_record(
                                    "RATE_ABOVE_MAX",
                                    "window stable rate %s/h exceeds max_rate %s/h"
                                    % (guard.state.get("best_stable"), eff_max_rate),
                                )
                                write_json_atomic(out_path / "outcome.json", outcome)
                                _record_window_outcome(stopped_ok)
                                return outcome
                            tripped_out = _trip_open_bound(sig_state)
                            if tripped_out is not None:
                                return tripped_out
                            if decision in ("ok", "unattributable"):
                                break
                            if idx + 1 < int(post_open_polls):
                                left_s, _ = _open_time_left()
                                pause_s = min(max(0.0, float(rate_poll_interval_s)), max(0.0, left_s))
                                if pause_s > 0:
                                    sleep(pause_s)
                        if guard.decision() == "unread":
                            log("post-open usage rate not settled; continuing priced locally at max_rate %g/h"
                                % eff_max_rate)
                finally:
                    lock_held = False

            # Open lock released: upload bundle and driver script flat into vm_content, bounded by remaining cap/wall.
            for local_f, remote_f in (
                (bundle_path, remote_bundle),
                (script_path, remote_script),
            ):
                up_t_lim, tripped_out = _next_call_timeout(120.0, sig_state)
                if tripped_out is not None:
                    return tripped_out
                up = client.upload(eff_session, str(local_f), remote_f, timeout=float(up_t_lim))
                if not up.ok:
                    tripped_out = _trip_open_bound(sig_state)
                    if tripped_out is not None:
                        return tripped_out
                    sig_state["cleaning"] = True
                    msg = scrub((up.err or up.out or "rc=%s" % up.rc).strip())
                    stopped_ok, stopped_at = _finalize_close(
                        client, eff_session, out_path, outcome, runner, already_stopped=None, clock=clock
                    )
                    _book_spend(outcome, card_spec=card_spec, guard=guard, elapsed_s=stopped_at - t_open_monotonic,
                                bal_end=_read_end_balance(client))
                    outcome["status"] = "upload_failed"
                    outcome["exit_code"] = 2
                    outcome["refusal"] = error_record("UPLOAD_FAILED", "upload of %s failed: %s" % (local_f.name, msg))
                    write_json_atomic(out_path / "outcome.json", outcome)
                    _record_window_outcome(stopped_ok)
                    return outcome
            tripped_out = _trip_open_bound(sig_state)
            if tripped_out is not None:
                return tripped_out
        except BaseException as exc:
            if marker_written:
                sig_state["cleaning"] = True
                try:
                    stopped_ok, stopped_at = _finalize_close(
                        client, eff_session, out_path, outcome, runner, already_stopped=None, clock=clock
                    )
                except BaseException:
                    stopped_ok, stopped_at = False, clock()
                elapsed_exc = (stopped_at - t_open_monotonic) if t_open_monotonic is not None else 0.0
                if t_open_monotonic is not None:
                    _book_spend(outcome, card_spec=card_spec, guard=guard, elapsed_s=elapsed_exc,
                                bal_end=_read_end_balance(client))
                is_int = isinstance(exc, (_OpenInterrupted, KeyboardInterrupt))
                outcome["status"] = "interrupted" if is_int else "error"
                outcome["exit_code"] = 2
                if not is_int and outcome["refusal"] is None:
                    outcome["refusal"] = error_record("UNEXPECTED_ERROR", scrub("%s: %s" % (type(exc).__name__, exc)))
                write_json_atomic(out_path / "outcome.json", outcome)
                _record_window_outcome(stopped_ok)
            if isinstance(exc, _OpenInterrupted):
                raise RunInterrupted(exc.signum) from None
            raise

        collected: list[str] = []
        salvaged: list[str] = []
        output_errors: dict[str, str] = {}
        collect_attempted = False

        def _next_cleanup_budget(max_call_s: float) -> Optional[float]:
            now = clock()
            stop_res_s = runner.stop_bound_s()
            end_t0 = runner._end_t0 if runner._end_t0 is not None else now
            cleanup_rem_s = eff_cleanup - max(0.0, now - end_t0) - stop_res_s
            if t_open_monotonic is not None:
                cap_total_s = (eff_cap / guard.priced_rate()) * 3600.0
                cap_rem_s = cap_total_s - max(0.0, now - t_open_monotonic) - stop_res_s
                rem_s = min(cleanup_rem_s, cap_rem_s) - 1e-3
            else:
                rem_s = cleanup_rem_s - 1e-3
            call_budget_s = rem_s - 2.0 * KILL_WAIT_S
            if call_budget_s < MIN_CALL_S:
                log("cleanup download skipped: %.2fs left after reserving %.0fs for stop" % (rem_s, stop_res_s))
                return None
            return min(float(max_call_s), call_budget_s)

        def _next_cleanup_download_timeout() -> Optional[float]:
            return _next_cleanup_budget(runner.download_timeout_s)

        def _next_cleanup_exec_timeout() -> Optional[float]:
            return _next_cleanup_budget(DEFAULT_TAR_TIMEOUT_S)

        def _download_vm_card(dest_list: list[str]) -> bool:
            if not (card_spec.card_class == "gpu" and card_spec.cli_flag == "--gpu"):
                return True
            dl_t = _next_cleanup_download_timeout()
            if dl_t is None:
                return False
            vm_card_local = out_path / "vm_card.json"
            remote_vm_card = "%s/%s_out/vm_card.json" % (vm_content_base, spec.job)
            vm_res = runner.download(remote_vm_card, vm_card_local, timeout=dl_t)
            if vm_res.ok and vm_card_local.is_file() and "vm_card.json" not in dest_list:
                dest_list.append("vm_card.json")
            return True

        def _do_collect() -> bool:
            nonlocal collect_attempted
            collect_attempted = True
            ok = _download_vm_card(collected)
            if ok and spec.collect_outputs:
                got, dl_ok = _download_declared_outputs(
                    runner, spec.collect_outputs, out_path / "collected",
                    content=vm_content, job=spec.job,
                    next_timeout=_next_cleanup_download_timeout,
                    next_exec_timeout=_next_cleanup_exec_timeout,
                    output_errors=output_errors,
                    download_parts=spec.download_parts,
                    chunk_bytes=chunk_bytes,
                )
                collected.extend(got)
                ok = dl_ok and all(rel in collected for rel in spec.collect_outputs)
            return ok

        def _do_salvage(reason: str) -> bool:
            ok = _download_vm_card(salvaged)
            if ok and spec.salvage_outputs:
                got, dl_ok = _download_declared_outputs(
                    runner, spec.salvage_outputs, out_path / "salvage",
                    content=vm_content, job=spec.job,
                    next_timeout=_next_cleanup_download_timeout,
                    next_exec_timeout=_next_cleanup_exec_timeout,
                    output_errors=output_errors,
                    download_parts=spec.download_parts,
                    chunk_bytes=chunk_bytes,
                )
                salvaged.extend(got)
                ok = dl_ok
            return ok

        run_env = build_job_env(
            spec, card_spec, content=vm_content, local_cpu=local_cpu, extra_env=merged_env,
        )
        runner._job_env = {
            str(k): str(v) for k, v in run_env.items() if k not in _CELL_TRANSIENT_ENV
        }

        run_error: Optional[BaseException] = None
        run_result = None
        need_gpu_card = card_spec.card_class == "gpu" and card_spec.cli_flag == "--gpu"
        need_collect = bool(spec.collect_outputs) or need_gpu_card
        need_salvage = bool(spec.salvage_outputs) or need_gpu_card
        try:
            try:
                run_result = runner.run(
                    script=remote_script,
                    env=run_env,
                    wall_s=eff_wall_s,
                    guard=guard,
                    progress_stall_s=spec.progress_stall_s,
                    setup_stall_s=spec.setup_stall_s,
                    resident=eff_resident,
                    salvage=_do_salvage if need_salvage else None,
                    collect=_do_collect if need_collect else None,
                    opened_monotonic=t_open_monotonic,
                    unreachable_polls=spec.unreachable_polls,
                    unreachable_s=spec.unreachable_s,
                )
            except BaseException as exc:
                if isinstance(exc, _OpenInterrupted):
                    run_error = RunInterrupted(exc.signum)
                else:
                    run_error = exc
                run_result = runner._res
        finally:
            sig_state["cleaning"] = True
            stopped_ok, stopped_at = _finalize_close(
                client, eff_session, out_path, outcome, runner,
                already_stopped=(run_result.stopped if run_result is not None else None),
                clock=clock,
            )
            elapsed_total = stopped_at - (t_open_monotonic if t_open_monotonic is not None else stopped_at)
            _book_spend(outcome, card_spec=card_spec, guard=guard, elapsed_s=elapsed_total, bal_end=_read_end_balance(client))

            if run_result is not None:
                outcome["run_rc"] = run_result.rc
                outcome["collected_outputs"] = list(collected)
                outcome["salvaged_outputs"] = list(salvaged)
                outcome["missing_outputs"] = (
                    [rel for rel in spec.collect_outputs if rel not in collected]
                    if (run_result.reason == "done" or (collect_attempted and run_result.prior_reason == "done"))
                    else []
                )
                outcome["output_errors"] = dict(output_errors)
                outcome["quarantined"] = list(run_result.quarantined)
                outcome["cap_trip"] = run_result.cap_trip
                outcome["refusal"] = run_result.refusal

            if (
                run_result is not None
                and run_result.reason == "failed"
                and run_result.rc == CARD_CHECK_FAIL_RC
                and _card_mismatch_seen(out_path, spec.job)
            ):
                outcome["status"] = "card_mismatch"
                outcome["refusal"] = error_record(
                    "CARD_MISMATCH",
                    "VM GPU verification failed for %s (%s/vm_card.json)" % (card_spec.name, spec.job),
                )
                if breaker is not None:
                    with _with_state_lock():
                        breaker.record_refusal(card_spec.name, reason="CARD_MISMATCH", session=eff_session, day=utc_day)
            elif run_result is not None:
                outcome["status"] = run_result.reason

            if outcome["status"] == "done" and outcome["missing_outputs"]:
                outcome["status"] = "partial"

            if run_error is not None:
                is_int = isinstance(run_error, (RunInterrupted, KeyboardInterrupt))
                outcome["status"] = "interrupted" if is_int else "error"
                if outcome["refusal"] is None and not is_int:
                    if isinstance(run_error, BinaryDownloadRefused):
                        outcome["refusal"] = error_record("BINARY_DOWNLOAD_TOKEN", scrub(run_error.message))
                    elif isinstance(run_error, QuarantineEscape):
                        outcome["refusal"] = error_record("QUARANTINE_ESCAPE", scrub(run_error.message))
                    else:
                        outcome["refusal"] = error_record(
                            "UNEXPECTED_ERROR", scrub("%s: %s" % (type(run_error).__name__, run_error))
                        )
                outcome["exit_code"] = 2
            elif outcome["status"] == "done":
                outcome["exit_code"] = 0 if stopped_ok else 3
            else:
                outcome["exit_code"] = 2

            outcome["outcome_bucket"] = classify_window_outcome(outcome)
            write_json_atomic(out_path / "outcome.json", outcome)
            _record_window_outcome(stopped_ok)

        if run_error is not None:
            raise run_error
        if sig_state["deferred"]:
            raise RunInterrupted(sig_state["deferred"][0])
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
    """Inspect a live, orphaned, pending-stop, dry-run, or completed window out-dir without touching the VM."""
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
        pid = (meta or {}).get("launcher_pid")
        state = "orphaned" if (isinstance(pid, int) and pid > 0 and not _pid_alive(pid)) else "running"
    elif stop_conf is not None or run_res is not None:
        state = "finished"
    else:
        state = "unknown"

    return {
        "out_dir": str(root),
        "state": state,
        "launcher_pid": (meta or {}).get("launcher_pid"),
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
    "build_job_env",
    "compute_launch_budget",
    "default_cleanup_bound_s",
    "default_session_name",
    "format_launch_budget",
    "launch_window",
    "open_phase_bound_s",
    "reconcile_stopped_outcome",
    "resolve_kit_env",
    "status_window",
]

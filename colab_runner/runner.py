"""The rig-side detached runner.

Starts a job on the VM as its own session (idempotent), then polls it with short, bounded exec calls and backoff.
A failed poll never touches the job. The run ends on: the job's status file (`done`, or `failed` / `oom`), dead
without a status file, a progress or setup stall, a poll blackout (`unreachable`), the wall, the cap, a start that
was never confirmed, a signal (`interrupted`) or an exception (`error`).

Whatever the end, the run stops its own session, in a `finally`: on a trip it first terminates the VM job's process
group, then runs the optional `salvage` callable (on every end but `done`), then stops. Markers are written only
AFTER the action is confirmed: `session_open.json` is written before the start cell and removed only after a
confirmed stop, and a stop that failed leaves `stop_pending.json`, never `stop_confirmed.json`, so
`python -m colab_runner stop --out-dir D` can retry it after a rig crash.

Every step is bounded by the wall and by the cap's ceiling: each sleep and each call timeout is clamped to the time
left to the nearer of the two, and start retries stop when the next attempt could pass it.

Every run has its own id (CR_RUN_ID). The VM stamps it on the job's files, its status file and every poll record;
the runner accepts only records stamped with its own id, so a record another run left under the same `--job` never
ends this run, and the VM never touches another run's files or job (a `--job` held by another run is refused,
JOB_ALREADY_EXISTS).

Kits supply: the job script (or hold seconds), a progress counter (CR_PROGRESS_GLOB / CR_UNITS_GLOB, see vm/lib.py),
and a phase file (`<out>/phase.txt`: "setup" while installing, anything else once running)."""

from __future__ import annotations

import contextlib
import inspect
import json
import math
import os
import signal
import threading
import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from colab_runner.colab_cli import (
    EXEC_REPORT_S,
    KILL_WAIT_S,
    BinaryDownloadRefused,
    QuarantineEscape,
    CliResult,
    Colab,
    default_quarantine_dir,
    scrub_local,
)
from colab_runner.errors import Refused, cap_trip_record, error_record
from colab_runner.window_lib import launch_guards as lg

VM_DIR = Path(__file__).parent / "vm"
SETUP_PHASES = (None, "", "setup")
MIN_CALL_S = 1.0  # no call is started with less than this left before the wall or the ceiling
USAGE_RETRY_PAUSE_S = 2.0  # between the tries of a failed `colab usage` read
REACHED_EPS_S = 1e-6  # less than this left: the wall or the ceiling has been reached
DEFAULT_DOWNLOAD_TIMEOUT_S = 120.0
_CELL_TRANSIENT_ENV = frozenset({
    "CR_RUN_ID",
    "CR_MODE",
    "CR_SCRIPT",
    "CR_HOLD_S",
    "CR_HEARTBEAT_S",
    "CR_POLL_LOG_OFFSET",
    "CR_KILL_GRACE_S",
    "CR_RESIDENT_MAX_S",
    "CR_TAR_REL",
})
# Ends on which the VM job may still be running: its process group is terminated before salvage and stop.
TRIPS = frozenset(
    {"wall", "cap", "setup_stall", "progress_stall", "dead", "unreachable", "start_failed", "interrupted", "error"}
)

# The kill cell's answers for a job that is this run's own (vm/lib.py cr_kill_job checks the owner first).
OWN_KILL_ANSWERS = ("CR_KILLED", "CR_NOT_RUNNING", "CR_NO_JOB")

# A fresh `colab exec` kernel handshake can be slower than the CLI's default request timeout, so a start or poll that
# fails is retried with backoff rather than read as a dead job.


def scrub(text: Optional[str]) -> str:
    return lg.scrub_colab_tokens(text or "")


def scrub_obj(obj: Any) -> Any:
    """Every string in `obj` (dicts and lists included) through the library's token scrub."""
    if isinstance(obj, str):
        return lg.scrub_colab_tokens(obj)
    if isinstance(obj, dict):
        return {scrub_obj(k): scrub_obj(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [scrub_obj(v) for v in obj]
    return obj


def _scrub_source() -> str:
    """The library's token scrub for the VM cells: its patterns, and its own function source. The VM has no copy of
    the library, so the cell carries the one implementation instead of a copy of one of its regexes."""
    pats = [(p.pattern, p.flags, repl) for p, repl in lg.TOKEN_SCRUB_PATTERNS]
    return (
        "import re as _cr_re\n"
        "TOKEN_SCRUB_PATTERNS = [(_cr_re.compile(_p, _f), _r) for _p, _f, _r in %r]\n" % (pats,)
        + inspect.getsource(lg.scrub_colab_tokens)
    )


def vm_lib_source() -> str:
    """The library's scrub + vm/lib.py: what every cell starts with."""
    return _scrub_source() + "\n" + (VM_DIR / "lib.py").read_text(encoding="utf-8")


def cell_source(name: str) -> str:
    """vm_lib_source() + <name>.py: one self-contained kernel cell."""
    return vm_lib_source() + "\n" + (VM_DIR / (name + ".py")).read_text(encoding="utf-8")


def parse_poll(stdout: str) -> Optional[tuple[dict, list[str]]]:
    rec, lines = None, []
    for ln in stdout.splitlines():
        if ln.startswith("CR_POLL "):
            try:
                rec = json.loads(ln[len("CR_POLL "):])
            except ValueError:
                rec = None
        elif ln.startswith("CR_LOG "):
            lines.append(ln[len("CR_LOG "):])
    return (rec, lines) if isinstance(rec, dict) else None


def write_json_atomic(path: Path, obj) -> None:
    """Write `obj` as JSON, every string in it scrubbed first, via a temp file and a rename."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(scrub_obj(obj), indent=2) + "\n", encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def _utc_now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def require_number(code: str, name: str, value, *, low: float = 0.0, inclusive: bool = False,
                   integer: bool = False) -> None:
    """Refuse (`code`) a value that is not a finite number above `low` (or at it, when `inclusive`)."""
    ok = (
        isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value)
        and (value >= low if inclusive else value > low) and (not integer or float(value).is_integer())
    )
    if not ok:
        raise Refused(code, "%s must be a finite %s %s %g (got %r)" % (
            name, "integer" if integer else "number", ">=" if inclusive else ">", low, value))


def _stop_pause(attempt: int) -> float:
    return min(2.0 * attempt, 10.0)


def _parse_resident_max_s(raw: Any) -> float:
    try:
        val = float(raw)
    except (TypeError, ValueError):
        raise Refused("RESIDENT_MAX_INVALID", "CR_RESIDENT_MAX_S must be a finite number > 0 (got %r)" % (raw,)) from None
    require_number("RESIDENT_MAX_INVALID", "CR_RESIDENT_MAX_S", val)
    return val


@dataclass
class RunResult:
    # done | failed | oom | dead | progress_stall | setup_stall | unreachable | wall | cap | start_failed |
    # interrupted | error
    reason: str
    rc: Optional[int] = None
    polls_ok: int = 0
    polls_failed: int = 0
    max_consecutive_failed: int = 0
    last: dict = field(default_factory=dict)
    detail: str = ""
    elapsed_s: float = 0.0  # when the run's loop ended, from the run's start
    killed: Optional[str] = None  # the kill cell's answer on a trip (CR_KILLED / CR_NOT_RUNNING / ...)
    salvaged: Optional[bool] = None
    stopped: Optional[bool] = None
    quarantined: list[dict[str, str]] = field(default_factory=list)
    # a run that ended on a named refusal (JOB_ALREADY_EXISTS, BINARY_DOWNLOAD_TOKEN, QUARANTINE_ESCAPE):
    # {code, message, hint, retryable}
    refusal: Optional[dict] = None
    # the end that a refusal during cleanup replaced with `error` (wall, cap, ...), else None
    prior_reason: Optional[str] = None
    # a `cap` end: {code, message, hint, retryable}, the code read from the trip's leading `CODE:` (see
    # errors.cap_trip_record); None on every other end. `detail` keeps the trip string as it was
    cap_trip: Optional[dict] = None


class RunInterrupted(Exception):
    """Raised into the run by its SIGINT / SIGTERM handler, so the `finally` stops the session."""

    def __init__(self, signum: int):
        super().__init__("signal %d" % signum)
        self.signum = signum


class _SalvageAborted(BaseException):
    """A second signal during the salvage: what is left of it is skipped, never the stop."""


class Runner:
    def __init__(
        self,
        colab: Colab,
        session: str,
        out_dir: Path,
        *,
        job: str = "job",
        content: str = "/content",
        poll_s: float = 60.0,
        poll_timeout_s: float = 60.0,
        backoff_max_s: float = 300.0,
        start_attempts: int = 5,
        start_timeout_s: float = 60.0,
        usage_timeout_s: float = 30.0,
        usage_retries: int = 2,
        kill_timeout_s: float = 60.0,
        kill_grace_s: float = 5.0,
        restart_timeout_s: float = 180.0,
        log_timeout_s: float = 300.0,
        stop_attempts: int = 3,
        stop_timeout_s: float = 120.0,
        download_timeout_s: float = DEFAULT_DOWNLOAD_TIMEOUT_S,
        pre_opened: bool = False,
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        log: Callable[[str], None] = lambda s: None,
        trace: Callable[[str], None] = lambda s: None,
        job_env: Mapping[str, str] | None = None,
    ):
        require_number("POLL_INVALID", "poll_s", poll_s)
        for name, value in (
            ("poll_timeout_s", poll_timeout_s), ("backoff_max_s", backoff_max_s), ("start_timeout_s", start_timeout_s),
            ("usage_timeout_s", usage_timeout_s), ("kill_timeout_s", kill_timeout_s),
            ("restart_timeout_s", restart_timeout_s), ("log_timeout_s", log_timeout_s),
            ("stop_timeout_s", stop_timeout_s), ("download_timeout_s", download_timeout_s),
        ):
            require_number("SETTING_INVALID", name, value)
        require_number("SETTING_INVALID", "kill_grace_s", kill_grace_s, inclusive=True)
        require_number("SETTING_INVALID", "start_attempts", start_attempts, low=1, inclusive=True, integer=True)
        require_number("SETTING_INVALID", "stop_attempts", stop_attempts, low=1, inclusive=True, integer=True)
        require_number("SETTING_INVALID", "usage_retries", usage_retries, inclusive=True, integer=True)
        self.usage_retries = int(usage_retries)
        self.colab, self.session, self.out_dir = colab, session, Path(out_dir)
        self.quarantine_dir = default_quarantine_dir(self.out_dir)
        self.job, self.content = job, content
        self.poll_s, self.poll_timeout_s, self.backoff_max_s = poll_s, poll_timeout_s, backoff_max_s
        self.start_attempts, self.start_timeout_s = int(start_attempts), start_timeout_s
        self.usage_timeout_s, self.kill_timeout_s, self.kill_grace_s = usage_timeout_s, kill_timeout_s, kill_grace_s
        self.restart_timeout_s, self.log_timeout_s = restart_timeout_s, log_timeout_s
        self.stop_attempts, self.stop_timeout_s = int(stop_attempts), stop_timeout_s
        self.download_timeout_s = download_timeout_s
        self._pre_opened = bool(pre_opened)
        self._end_t0: Optional[float] = None
        self.clock, self.sleep = clock, sleep
        self.log = lambda s: log(scrub(s))  # nothing reaches the log unscrubbed
        self.trace = lambda s: trace(scrub(s))  # one line per poll (the CLI's --verbose), scrubbed the same way
        self._resident = None
        self._cleanup_signals: list[int] = []
        self._phase = "idle"
        self._res = RunResult("error")
        # a download refused in cleanup (BinaryDownloadRefused, or QuarantineEscape): it ends the run as `error`
        self._download_refusal: Optional[BaseException] = None
        self._run_id = ""
        self._job_reused = False
        self._own_seen = False  # a record of this run (its own run id) has been seen, or its start confirmed
        self._foreign_id: Optional[str] = None  # the run id of the last record refused as another run's
        self._t0, self._wall_s, self._guard = 0.0, 0.0, None
        self._result_written = False  # runner_result.json holds this run's end: a later quarantine rewrites it
        self._prior_detail = ""
        self._job_env: dict[str, str] = {
            str(k): str(v) for k, v in (job_env or {}).items() if k not in _CELL_TRANSIENT_ENV
        }

    # -- environment the cells read -------------------------------------------------------------------------------
    def _env(self, extra: Mapping[str, str] | None = None) -> dict:
        base = self.content.rstrip("/\\") or "/"
        env = {
            "CR_CONTENT": self.content,
            "CR_JOB": self.job,
            "CR_OUT_DIR": f"{base}/{self.job}_out" if base != "/" else f"/{self.job}_out",
            "CR_PROGRESS_GLOB": "",
            "CR_UNITS_GLOB": "",
        }
        env.update(self._job_env)
        if self._run_id:
            env["CR_RUN_ID"] = self._run_id  # every cell: the VM answers for this run only
        env.update(extra or {})
        return env

    # -- whose record is it -----------------------------------------------------------------------------------------
    def _other_run(self, rec: Mapping[str, Any]) -> Optional[str]:
        """None if `rec` is this run's (its run_id, and for a record with an rc its status_run_id, are this run's
        id); else the other run id it carries. A runner with no run id yet (poll() before any start) checks nothing."""
        if not self._run_id:
            return None
        mine = self._run_id.strip()
        if not _is_run_id(rec.get("run_id"), mine):  # a JSON null is never compared as the string "None"
            return str(rec.get("run_id"))
        if rec.get("rc") is not None and not _is_run_id(rec.get("status_run_id"), mine):
            return str(rec.get("status_run_id"))
        return None

    def _refuse_record(self, other: str, where: str) -> None:
        self._foreign_id = other
        self.log("%s: a record of another run (run_id %s) refused: it is never this run's" % (where, other))

    def _record_quarantine(self, entry: dict[str, str]) -> None:
        item = {"path": entry["path"], "source": entry["source"], "pattern": entry["pattern"]}
        if all(existing.get("path") != item["path"] for existing in self._res.quarantined):
            self._res.quarantined.append(item)
            if self._result_written and self._phase == "idle":
                # a Runner.download after run() returned: runner_result.json is rewritten with it
                write_json_atomic(self.out_dir / "runner_result.json", asdict(self._res))

    @contextlib.contextmanager
    def _bind_quarantine(self):
        if not isinstance(self.colab, Colab):
            yield
            return
        prev = (self.colab.quarantine_dir, self.colab._base_dir, self.colab.log, self.colab.on_quarantine)
        self.colab.quarantine_dir = self.quarantine_dir
        self.colab._base_dir = self.out_dir
        self.colab.log = self.log
        self.colab.on_quarantine = self._record_quarantine
        try:
            yield
        finally:
            self.colab.quarantine_dir, self.colab._base_dir, self.colab.log, self.colab.on_quarantine = prev

    @staticmethod
    def _clean(res: CliResult) -> CliResult:
        """A CLI result with its out and err scrubbed (the real wrapper scrubs too; this covers any stand-in)."""
        return CliResult(res.rc, scrub(res.out), scrub(res.err))

    def stop_bound_s(self) -> float:
        """Worst-case seconds for `stop_session()` (all stop attempts, kill waits, and pauses)."""
        pauses = sum(_stop_pause(i) for i in range(1, self.stop_attempts))
        return self.stop_attempts * (self.stop_timeout_s + 2.0 * KILL_WAIT_S) + pauses

    def own_cleanup_bound_s(self, resident: bool) -> float:
        """The worst case of the runner's own cleanup after a trip: the kill cell, then (resident mode) the kernel
        restart that frees it and the run-log download, then every stop attempt with the pauses between them; each
        call can take 2 x KILL_WAIT_S more to be killed and reaped. In resident mode the background exec's report
        time (EXEC_REPORT_S, given on top of the window, see _start_resident) and its own kill (KILL_WAIT_S) count
        too. A caller's salvage is the caller's to add."""
        calls = [self.kill_timeout_s] + ([self.restart_timeout_s, self.log_timeout_s] if resident else [])
        resident_extra = EXEC_REPORT_S + KILL_WAIT_S if resident else 0.0
        return sum(calls) + 2 * KILL_WAIT_S * len(calls) + self.stop_bound_s() + resident_extra

    # -- time left before the wall and the ceiling ---------------------------------------------------------------
    def _time_left(self) -> tuple[float, str]:
        """Seconds left before the nearer of the wall and the cap's ceiling, and which one it is."""
        elapsed = self.clock() - self._t0
        left = lg.evaluate_wall_guard(elapsed_s=elapsed, wall_s=self._wall_s, grace_s=0.0)["remaining_s"]
        if self._guard is not None:
            cap_left = self._guard.seconds_left(elapsed)
            if cap_left < left:
                return cap_left, "cap"
        return left, "wall"

    def _budget(self, until: Optional[float] = None) -> tuple[float, str]:
        """_time_left(), also bounded by `until` (a clock time: the end of the resident start's setup window)."""
        left, bound = self._time_left()
        if until is not None and until - self.clock() < left:
            return until - self.clock(), "setup"
        return left, bound

    def _pause(self, delay: float, until: Optional[float] = None) -> None:
        """Sleep `delay`, clamped to the time left before the wall and the ceiling (and `until`)."""
        left, _ = self._budget(until)
        s = min(delay, max(left, 0.0))
        if s > 0:
            self.sleep(s)

    # -- start ----------------------------------------------------------------------------------------------------
    def start(self, env: Mapping[str, str]) -> tuple[bool, str]:
        """Start the job; a lost reply is retried and can never start a second job (the cell is idempotent).
        Each attempt's timeout is clamped to what is left before the wall and the ceiling after its backoff,
        min(start_timeout_s, left - pause); the retries stop when that is under MIN_CALL_S."""
        full = dict(env)
        # stripped as run() strips it: a blank id would leave cr_run_id() empty on the VM and turn the refusal off
        given = full.get("CR_RUN_ID")
        given = str(given).strip() if given is not None else ""  # None never becomes the string "None"
        full["CR_RUN_ID"] = given or self._run_id.strip() or uuid.uuid4().hex
        if not self._run_id.strip():
            self._run_id = full["CR_RUN_ID"]
        delay, last = 2.0, ""
        for attempt in range(1, self.start_attempts + 1):
            pause = delay if attempt > 1 else 0.0
            left, bound = self._time_left()
            timeout = min(self.start_timeout_s, left - pause)
            if timeout < MIN_CALL_S:
                self.log("start attempt %d not made: it could pass the %s (%.0fs left)" % (attempt, bound, left))
                return False, last or "no time for a start attempt before the %s" % bound
            if pause:
                self.sleep(pause)
                delay = min(delay * 2, self.backoff_max_s)
            res = self._clean(self.colab.exec_cell(self.session, cell_source("start"), self._env(full), timeout))
            text = (res.out or res.err or "").strip()
            last = text.splitlines()[-1] if text else ""
            if res.ok and "CR_JOB_EXISTS" in res.out:
                self._job_reused = True
                msg = (
                    "JOB_ALREADY_EXISTS: --job %r already exists in %s (%s); pass a distinct --job for each step"
                    % (self.job, self.content, last)
                )
                self.log("start refused: %s" % msg)
                return False, msg
            if res.ok and ("CR_STARTED" in res.out or "CR_ALREADY_RUNNING" in res.out):
                self.log("start ok (attempt %d): %s" % (attempt, last))
                return True, last
            # a lost reply may still have started the job: the next attempt finds it (CR_ALREADY_RUNNING)
            self.log("start attempt %d failed rc=%s: %s" % (attempt, res.rc, last))
        return False, last

    def _start_resident(self, env: Mapping[str, str], left: float) -> None:
        """The resident cell writes its poll file until CR_RESIDENT_MAX_S (the window's end). Its exec gets the CLI's
        report time on top (EXEC_REPORT_S, counted in own_cleanup_bound_s), so `colab exec --timeout`
        (kernel_timeout) is never shorter than CR_RESIDENT_MAX_S and cannot end the cell before the window does.
        Nothing bills for the extra: every exit kills the child (_end) before the stop."""
        full = self._env(env)
        full.setdefault("CR_POLL_EVERY_S", str(self.poll_s))
        ceil_left = max(1, math.ceil(left))
        if "CR_RESIDENT_MAX_S" in full:
            max_s = max(1.0, _parse_resident_max_s(full["CR_RESIDENT_MAX_S"]))
        else:
            full["CR_RESIDENT_MAX_S"] = str(ceil_left)
            max_s = float(ceil_left)
        self._resident = self.colab.start_cell(self.session, cell_source("resident"), full, max_s + EXEC_REPORT_S)

    # -- poll -----------------------------------------------------------------------------------------------------
    def poll(self, offset: int, timeout: Optional[float] = None) -> Optional[tuple[dict, list[str]]]:
        """One poll cell. A record of another run is refused: it counts as a failed poll, never as this job's."""
        res = self._clean(self.colab.exec_cell(
            self.session, cell_source("poll"), self._env({"CR_POLL_LOG_OFFSET": str(offset)}),
            self.poll_timeout_s if timeout is None else timeout,
        ))
        got = parse_poll(res.out) if res.ok else None
        if got is not None:
            other = self._other_run(got[0])
            if other is not None:
                self._refuse_record(other, "poll")
                return None
        return got

    def _read_usage(self) -> tuple[Optional[CliResult], bool]:
        """One `colab usage` reading: a failed read is retried up to `usage_retries` times, each attempt (and the
        pause before it) clamped to the time left before the wall and the ceiling. Returns (last_try, skipped_for_limit)."""
        last, skipped = None, False
        for attempt in range(self.usage_retries + 1):
            if attempt:
                self._pause(USAGE_RETRY_PAUSE_S)
            left, _ = self._time_left()
            if left < MIN_CALL_S:
                skipped = True
                break
            last = self._clean(self.colab.usage(min(self.usage_timeout_s, left)))
            if last.ok:
                break
            self.log("colab usage attempt %d failed rc=%s: %s" % (attempt + 1, last.rc, (last.err or "")[-200:]))
        return last, skipped

    def download(self, remote: str, local: Path | str, timeout: Optional[float] = None) -> CliResult:
        """`colab download`; whatever is at `local` afterwards passes through `scrub_local` (quarantining binary
        token hits in `<out-dir>.quarantine`) before anything reads it, in a `finally`."""
        eff_timeout = self.download_timeout_s if timeout is None else timeout
        target = Path(local)
        target.parent.mkdir(parents=True, exist_ok=True)
        with self._bind_quarantine():
            try:
                return self._clean(self.colab.download(self.session, remote, str(target), eff_timeout))
            except BinaryDownloadRefused as exc:
                for entry in exc.quarantined:
                    self._record_quarantine(entry)
                raise
            finally:
                try:
                    scrub_local(
                        target, quarantine_dir=self.quarantine_dir, base_dir=self.out_dir,
                        log=self.log, on_quarantine=self._record_quarantine,
                    )
                except BinaryDownloadRefused as exc:
                    for entry in exc.quarantined:
                        self._record_quarantine(entry)
                    raise

    def _download(self, remote: str, local: Path, timeout: float) -> CliResult:
        return self.download(remote, local, timeout)

    def _fetch_record(self, name: str, timeout: float) -> Optional[dict]:
        """`<content>/<job>.<name>` downloaded into the out dir and read as a JSON object, or None."""
        local = self.out_dir / ("%s.%s" % (self.job, name))
        res = self._download("%s/%s.%s" % (self.content.rstrip("/"), self.job, name), local, timeout)
        if not res.ok or not local.is_file():
            return None
        try:
            rec = json.loads(local.read_text(encoding="utf-8"))
        except ValueError:
            return None
        return rec if isinstance(rec, dict) else None

    def poll_resident(self, timeout: Optional[float] = None) -> Optional[tuple[dict, list[str]]]:
        """The resident cell's poll file, if it is this run's. A poll file another run left under this `--job` is
        refused (it is "not yet seen", and its local copy is removed); then this run's own refusal record, if its
        cell has written one, `<job>.refused.json` stamped with this run's id. Both downloads share `timeout`."""
        budget = self.poll_timeout_s if timeout is None else timeout
        t0 = self.clock()
        rec = self._fetch_record("poll.json", budget)
        if rec is not None:
            other = self._other_run(rec)
            if other is None:
                return rec, []
            self._refuse_record(other, "resident poll file")
            (self.out_dir / ("%s.poll.json" % self.job)).unlink(missing_ok=True)
        left = budget - (self.clock() - t0)
        if left < MIN_CALL_S:
            return None
        ref = self._fetch_record("refused.json", left)
        if ref is None:
            return None
        mine = self._run_id.strip()
        if ref.get("error") == "JOB_ALREADY_EXISTS" and mine and _is_run_id(ref.get("run_id"), mine):
            return ref, []
        (self.out_dir / ("%s.refused.json" % self.job)).unlink(missing_ok=True)  # another run's refusal
        return None

    def _collect_resident_log(self, timeout: float) -> bool:
        """Resident mode: the job's whole run log, downloaded (and scrubbed) into the out dir."""
        local = self.out_dir / ("%s.run.out" % self.job)
        try:
            res = self._download("%s/%s.run.out" % (self.content.rstrip("/"), self.job), local, timeout)
        except (BinaryDownloadRefused, QuarantineEscape):  # refused either way: never downloaded again in _end
            self._log_collected = True
            raise
        if res.ok and local.is_file():
            self._log_collected = True
            if lg.OOM_KILL_RE.search(local.read_text(encoding="utf-8", errors="replace")):
                self._oom_seen = True
            return True
        self.log("run log not collected rc=%s: %s" % (res.rc, (res.err or "")[-200:]))
        return False

    def _append_log(self, run_log: Path, lines: list[str]) -> None:
        clean = [scrub(ln) for ln in lines]
        with run_log.open("a", encoding="utf-8", newline="\n") as fh:
            fh.write("\n".join(clean) + "\n")
        if not self._oom_seen and any(lg.OOM_KILL_RE.search(ln) for ln in clean):
            self._oom_seen = True

    # -- the run --------------------------------------------------------------------------------------------------
    def run(
        self,
        *,
        script: Optional[str] = None,
        hold_s: Optional[float] = None,
        heartbeat_s: float = 30.0,
        env: Mapping[str, str] | None = None,
        wall_s: float,
        guard=None,
        progress_stall_s: float = 900.0,
        setup_stall_s: float = 600.0,
        dead_confirm: int = 2,
        resident: bool = False,
        salvage: Optional[Callable[[str], bool]] = None,
        collect: Optional[Callable[[], bool]] = None,
        opened_monotonic: Optional[float] = None,
        unreachable_polls: int = 10,
        unreachable_s: float = 1800.0,
    ) -> RunResult:
        """`guard` (a SpendGuard, or anything with check(elapsed_s) and seconds_left(elapsed_s), and optionally
        fold_usage(...)) ends the run at the cap and bounds every step by its ceiling. Refusals (Refused) are raised
        before any `colab` call; after that, every end goes through the stop."""
        kit_env = self._check_run(script, hold_s, heartbeat_s, env, wall_s, guard, progress_stall_s, setup_stall_s,
                                  dead_confirm, resident, unreachable_polls, unreachable_s, opened_monotonic)
        if env:
            self._job_env = {
                str(k): str(v) for k, v in env.items() if k not in _CELL_TRANSIENT_ENV
            }
        hold_mode = hold_s is not None
        now_m = self.clock()
        self._t0 = min(float(opened_monotonic), now_m) if opened_monotonic is not None else now_m
        self._wall_s, self._guard = float(wall_s), guard
        self._resident, self._oom_seen, self._log_collected, self._skip_salvage = None, False, False, False
        self._res = RunResult("error")
        self._download_refusal = None
        self._run_id = kit_env["CR_RUN_ID"]
        self._job_reused = False
        self._own_seen, self._foreign_id = False, None
        self._result_written, self._prior_detail = False, ""
        self._cleanup_signals: list[int] = []
        self._phase = "run"
        self._end_t0 = None
        handlers: Optional[dict] = None
        crash: Optional[BaseException] = None
        with self._bind_quarantine():
            try:
                handlers = self._arm_signals()  # inside the try: a signal before it is armed still reaches the finally
                # written inside the try, before the start cell: no gap between the marker and the stop's finally
                write_json_atomic(
                    self.out_dir / "session_open.json", {"session": self.session, "job": self.job, "content": self.content}
                )
                self._run_body(kit_env, wall_s, guard, progress_stall_s, setup_stall_s, dead_confirm, resident, hold_mode,
                               unreachable_polls, unreachable_s)
            except RunInterrupted as exc:
                self._end_as("interrupted", "signal %s" % _signal_name(exc.signum))
            except BaseException as exc:  # any crash, KeyboardInterrupt included: stop first, then let it go on
                self._end_as("interrupted" if isinstance(exc, KeyboardInterrupt) else "error",
                             "%s: %s" % (type(exc).__name__, exc))
                if isinstance(exc, (BinaryDownloadRefused, QuarantineEscape)):
                    self._res.refusal = error_record(exc.code, scrub(exc.message))
                crash = exc
            finally:
                if self._phase == "run":
                    self._phase = "stopping"
                try:
                    self._end(self._res, salvage, resident, collect)
                finally:
                    self._phase = "idle"
                    self._disarm_signals(handlers)
                    if self._res.reason != "cap":  # an end that replaced a cap (a refused download) has no cap_trip
                        self._res.cap_trip = None
                    write_json_atomic(self.out_dir / "runner_result.json", asdict(self._res))
                    self._result_written = True
        if crash is None and self._download_refusal is not None:
            crash = self._download_refusal
        if crash is not None:
            raise crash
        return self._res

    def _check_run(self, script, hold_s, heartbeat_s, env, wall_s, guard, progress_stall_s, setup_stall_s,
                   dead_confirm, resident, unreachable_polls, unreachable_s, opened_monotonic=None) -> dict:
        """Every refusal, by name, before any `colab` call."""
        require_number("WALL_INVALID", "wall_s", wall_s)
        if opened_monotonic is not None and (
            not isinstance(opened_monotonic, (int, float))
            or isinstance(opened_monotonic, bool)
            or not math.isfinite(opened_monotonic)
        ):
            raise Refused("SETTING_INVALID", "opened_monotonic must be a finite number (got %r)" % (opened_monotonic,))
        require_number("HEARTBEAT_INVALID", "heartbeat_s", heartbeat_s, inclusive=True)
        require_number("PROGRESS_STALL_INVALID", "progress_stall_s", progress_stall_s)
        require_number("SETUP_STALL_INVALID", "setup_stall_s", setup_stall_s)
        require_number("DEAD_CONFIRM_INVALID", "dead_confirm", dead_confirm, low=1, inclusive=True, integer=True)
        require_number("UNREACHABLE_INVALID", "unreachable_polls", unreachable_polls, low=1, inclusive=True,
                       integer=True)
        require_number("UNREACHABLE_INVALID", "unreachable_s", unreachable_s)
        kit_env = dict(env or {})
        if resident and "CR_RESIDENT_MAX_S" in kit_env:
            _parse_resident_max_s(kit_env["CR_RESIDENT_MAX_S"])
        if "CR_RUN_ID" in kit_env:
            # reserved: a lane id reused across steps would turn the --job refusal off between them
            raise Refused("RUN_ID_RESERVED", "CR_RUN_ID is the runner's own (every run gets a fresh one); drop it "
                          "from the run's environment")
        kit_env["CR_RUN_ID"] = uuid.uuid4().hex  # one id per run
        if hold_s is not None:
            require_number("HOLD_INVALID", "hold_s", hold_s)
            # a heartbeat-only job with no pause between beats spins writes on the VM; 0 has no meaning here
            require_number("HEARTBEAT_INVALID", "heartbeat_s (with hold_s)", heartbeat_s)
            kit_env.update({"CR_MODE": "hold", "CR_HOLD_S": str(hold_s), "CR_HEARTBEAT_S": str(heartbeat_s)})
        elif script:
            kit_env.update({"CR_MODE": "script", "CR_SCRIPT": script})
        else:
            raise Refused("NOTHING_TO_RUN", "run() needs script= or hold_s=")
        bound = getattr(guard, "cleanup_bound_s", None)
        if bound is not None and bound < self.own_cleanup_bound_s(resident):
            raise Refused("CLEANUP_BOUND_TOO_SMALL", "cleanup_bound_s %g is below the runner's own cleanup bound %g s "
                          "(kill, %sstop attempts)" % (bound, self.own_cleanup_bound_s(resident),
                                                       "kernel restart, run log, " if resident else ""))
        try:
            if not self._pre_opened and self.out_dir.exists() and (not self.out_dir.is_dir() or any(self.out_dir.iterdir())):
                # the confirmation markers are only true of this run if nothing was there before it (colab_window.sh
                # refuses the same: colab_refuse_nonempty_out_dir)
                raise Refused("OUT_DIR_NOT_EMPTY", "%s already exists and is not an empty directory" % self.out_dir)
            if self.out_dir.exists() and not self.out_dir.is_dir():
                raise Refused("OUT_DIR_INVALID", "%s exists and is not a directory" % self.out_dir)
            self.out_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:  # refused by name before the marker, so exit 1 keeps meaning "no session was owned"
            raise Refused("OUT_DIR_INVALID", "%s cannot be created or listed (%s: %s)"
                          % (self.out_dir, type(exc).__name__, exc)) from None
        return kit_env

    def _run_body(self, kit_env, wall_s, guard, progress_stall_s, setup_stall_s, dead_confirm, resident, hold_mode,
                  unreachable_polls, unreachable_s) -> None:
        if resident:
            left, bound = self._time_left()
            if left < MIN_CALL_S:
                return self._end_as("start_failed", "no time left to start before the %s" % bound)
            self._start_resident(kit_env, left)
        else:
            started, last = self.start(kit_env)
            if not started:
                if self._job_reused:
                    return self._end_refused("start_failed", "JOB_ALREADY_EXISTS", last.partition(": ")[2])
                return self._end_as("start_failed", "start not confirmed: %s" % last)
            self._own_seen = True  # the start cell answered for this run id: the job is this run's
        self._loop(wall_s, guard, progress_stall_s, setup_stall_s, dead_confirm, resident, hold_mode,
                   unreachable_polls, unreachable_s)

    def _end_as(self, reason: str, detail: str) -> None:
        self._res.reason, self._res.detail = reason, scrub(detail)
        self._res.elapsed_s = self.clock() - self._t0

    def _end_refused(self, reason: str, code: str, message: str) -> None:
        """An end on a named refusal: the detail is `CODE: message`, and runner_result.json records the refusal."""
        self._end_as(reason, "%s: %s" % (code, message))
        self._res.refusal = error_record(code, scrub(message))

    def _end_on_download_refusal(self) -> None:
        """A binary download refused in cleanup ends the run as `error`; the end it replaced is kept
        (`prior_reason`, and named in the detail)."""
        exc = self._download_refusal
        if self._res.reason != "error" and self._res.prior_reason is None:
            self._res.prior_reason, self._prior_detail = self._res.reason, self._res.detail
        detail = "%s: %s" % (type(exc).__name__, exc)
        if self._res.prior_reason is not None:
            prior = self._res.prior_reason + (": %s" % self._prior_detail if self._prior_detail else "")
            detail += " (the run had ended %s)" % prior
        self._end_as("error", detail)
        self._res.refusal = error_record(exc.code, scrub(exc.message))

    def _finished(self, rc: int) -> None:
        """The job's status file is in: `oom` on rc 137 or an OOM kill line in its log, `failed` on any other
        non-zero rc, else `done`."""
        self._res.rc = rc
        if rc == 137 or self._oom_seen:
            why = "rc 137 (SIGKILL)" if rc == 137 else "an OOM kill line in the job log (OOM_KILL_RE)"
            return self._end_as("oom", "the job was OOM-killed: %s" % why)
        if rc != 0:
            return self._end_as("failed", "the job exited with rc %d" % rc)
        return self._end_as("done", "")

    def _loop(self, wall_s, guard, progress_stall_s, setup_stall_s, dead_confirm, resident, hold_mode,
              unreachable_polls, unreachable_s) -> None:
        res = self._res
        offset, delay, failed_run, dead_seen = 0, self.poll_s, 0, 0
        loop_t0 = self.clock()
        last_key, key_t, last_setup, setup_t, last_phase_setup = None, loop_t0, None, loop_t0, True
        last_phase: Optional[str] = None
        last_good_t, file_seen, usage_t = loop_t0, False, None
        run_log = self.out_dir / ("%s.run.out" % self.job)
        fold = getattr(guard, "fold_usage", None)
        while True:
            if self._resident is not None:
                self._resident.poll()  # reaps it, or kills it at its deadline
            if lg.evaluate_wall_guard(elapsed_s=self.clock() - self._t0, wall_s=wall_s, grace_s=0.0)["tripped"]:
                return self._end_as("wall", "wall %.0fs reached" % wall_s)
            if guard is not None:
                u, usage_skipped = None, False
                if fold is not None and (usage_t is None or self.clock() - usage_t >= self.poll_s):
                    u, usage_skipped = self._read_usage()
                    if u is not None:
                        usage_t = self.clock()
                        fold(u.out if u.ok else u.err, rc=u.rc, epoch=usage_t, at=_utc_now())
                left_now, nearer = self._time_left()
                trip = guard.check(self.clock() - self._t0)
                if trip:
                    usage_cut = usage_skipped or (u is not None and not u.ok and left_now < MIN_CALL_S)
                    reason = nearer if usage_cut else "cap"
                    if reason == "cap":
                        self._res.cap_trip = cap_trip_record(scrub(trip))
                    return self._end_as(reason, trip)
            # resident mode: until its poll file has appeared, the start is unverified and has the setup window
            until = loop_t0 + setup_stall_s if resident and not file_seen else None
            left, bound = self._budget(until)
            if left < REACHED_EPS_S:
                if bound == "setup":
                    if self._foreign_id is not None:
                        # only another run's records under this --job: this run's cell never ran its start
                        self._job_reused = True
                        return self._end_refused(
                            "start_failed", "JOB_ALREADY_EXISTS",
                            "--job %r in %s holds another run's records (run_id %s); this run's own record did not "
                            "appear within %.0fs; pass a distinct --job for each step"
                            % (self.job, self.content, self._foreign_id, setup_stall_s),
                        )
                    return self._end_as("start_failed", "the resident poll file did not appear within %.0fs"
                                        % setup_stall_s)
                if bound == "cap":  # the ceiling reached by the clock at the priced rate, before the guard tripped
                    self._res.cap_trip = error_record("CAP_CEILING_REACHED", "the cap was reached")
                return self._end_as(bound, "the %s was reached" % bound)
            if left < MIN_CALL_S:
                self.sleep(left)
                continue
            timeout = min(self.poll_timeout_s, left)
            got = self.poll_resident(timeout) if resident else self.poll(offset, timeout)
            now = self.clock()
            if got is None:
                res.polls_failed += 1
                failed_run += 1
                self.trace("poll failed (%d in a row)" % failed_run)
                res.max_consecutive_failed = max(res.max_consecutive_failed, failed_run)
                if until is None and (failed_run >= unreachable_polls or now - last_good_t >= unreachable_s):
                    return self._end_as("unreachable", "%d polls failed in a row, %.0fs without a good poll"
                                        % (failed_run, now - last_good_t))
                delay = min(delay * 2, self.backoff_max_s)
                self._pause(delay, until)
                continue
            rec, lines = got
            if rec.get("error") == "JOB_ALREADY_EXISTS":
                self._job_reused = True
                return self._end_refused(
                    "start_failed", "JOB_ALREADY_EXISTS",
                    "--job %r already exists in %s (%s); pass a distinct --job for each step"
                    % (self.job, self.content, rec.get("detail", "")),
                )
            file_seen = self._own_seen = True
            self.trace("poll ok: phase=%s progress=%s units=%s alive=%s rc=%s" % (
                rec.get("phase"), rec.get("progress"), rec.get("units_done"), rec.get("alive"), rec.get("rc")))
            res.polls_ok += 1
            failed_run, delay, last_good_t = 0, self.poll_s, now
            res.last = scrub_obj(rec)
            if lines:
                self._append_log(run_log, lines)
            try:
                new_offset = int(rec.get("log_offset", offset))
            except (TypeError, ValueError):
                new_offset = offset
            more_log = not resident and not rec.get("log_eof", True) and new_offset > offset
            offset = new_offset
            if rec.get("rc") is not None:
                res.rc = int(rec["rc"])  # known now, even if the wall or the cap ends the drain below
                if more_log:
                    continue  # the job has ended: drain the rest of its log first, inside the wall and the cap
                if resident:
                    left, _ = self._time_left()
                    if left >= MIN_CALL_S:
                        self._collect_resident_log(min(self.log_timeout_s, left))
                return self._finished(res.rc)
            if not rec.get("alive"):
                dead_seen += 1
                if dead_seen >= dead_confirm:
                    return self._end_as("dead", "job not alive and no status file")
            else:
                dead_seen = 0
            raw_phase = rec.get("phase")
            if raw_phase not in (None, ""):
                last_phase = raw_phase
            in_setup = last_phase in SETUP_PHASES and not hold_mode
            if in_setup:
                if not last_phase_setup:  # a real return to setup starts its own stall clock
                    last_setup, setup_t = rec.get("setup_bytes"), now
                elif rec.get("setup_bytes") != last_setup:
                    last_setup, setup_t = rec.get("setup_bytes"), now
                elif now - setup_t >= setup_stall_s:
                    return self._end_as("setup_stall", "setup.log unchanged for %.0fs" % (now - setup_t))
            else:
                if last_phase_setup:  # the run phase starts its own stall clock
                    key_t = now
                key = (rec.get("progress"), rec.get("units_done"), rec.get("hold_beats"))
                if key != last_key:
                    last_key, key_t = key, now
                elif now - key_t >= progress_stall_s:
                    return self._end_as("progress_stall", "no progress for %.0fs" % (now - key_t))
            last_phase_setup = in_setup
            if more_log:
                continue  # the offset advanced and more log is waiting: poll again at once
            self._pause(delay)

    # -- the end: kill on a trip, salvage, stop -------------------------------------------------------------------
    def _step(self, name: str, fn: Callable[[], Any]) -> Any:
        """One cleanup step; whatever it raises is logged, and the next step (the stop above all) still runs."""
        try:
            return fn()
        except (BinaryDownloadRefused, QuarantineEscape) as exc:
            if self._download_refusal is None:
                self._download_refusal = exc
            for entry in getattr(exc, "quarantined", []):
                self._record_quarantine(entry)
            self.log("cleanup step %s failed: %s: %s" % (name, type(exc).__name__, exc))
            return None
        except BaseException as exc:
            self.log("cleanup step %s failed: %s: %s" % (name, type(exc).__name__, exc))
            return None

    def _end(self, res: RunResult, salvage: Optional[Callable[[str], bool]], resident: bool,
             collect: Optional[Callable[[], bool]] = None) -> None:
        """On a trip, terminate the VM job's process group first; then collect (on done) or salvage (every other
        end); then stop this run's own session, always. Cleanup outputs stay bounded inside the cleanup reserve."""
        self._end_t0 = self.clock()
        if self._resident is not None:
            self._step("resident child", self._resident.kill)
        # a job this run never owned (refused, or only another run's records seen) is never killed or collected
        not_ours = self._job_reused or (self._foreign_id is not None and not self._own_seen)
        if res.reason in TRIPS and not not_ours:
            self._step("kill", lambda: self._kill_job(res, resident))
            if (res.killed or "").startswith("CR_NOT_OURS"):
                # the VM says the job under this --job is another run's (a trip before any record was seen): its
                # run log is never collected into this run's out dir
                not_ours = True
                self.log("the job under --job %r is another run's: its run log is not collected" % self.job)
        # the job is known to be this run's: a record of its own was seen, or the kill cell answered for this run id
        # (it answers CR_NOT_OURS for another run's job); a kill cell that failed outright proves nothing
        owned = self._own_seen or (res.killed or "").startswith(OWN_KILL_ANSWERS)
        if resident and not self._log_collected and not not_ours and not owned:
            self.log("the job under --job %r was never shown to be this run's: its run log is not collected" % self.job)
        if resident and not self._log_collected and not not_ours and owned:
            self._step("run log", lambda: self._collect_resident_log(self.log_timeout_s))
            if self._download_refusal is not None:
                self._end_on_download_refusal()
            elif res.reason in ("done", "failed") and self._oom_seen:  # `oom` whatever the rc, as _finished
                res.reason, res.detail = "oom", "the job was OOM-killed: an OOM kill line in the job log (OOM_KILL_RE)"
        if res.reason == "done" and collect is not None and not not_ours and owned:
            if self._skip_salvage:
                self.log("collect skipped: a signal arrived during cleanup")
            else:
                self._step("collect", lambda: self._collect(collect))
        elif res.reason != "done" and salvage is not None:
            if self._skip_salvage:
                res.salvaged = False
                self.log("salvage skipped: a signal arrived during cleanup")
            else:
                self._step("salvage", lambda: self._salvage(res, salvage))
        if self._download_refusal is not None:
            self._end_on_download_refusal()
        self._phase = "stop"
        res.stopped = bool(self._step("stop", self.stop_session))
        for signum in self._cleanup_signals:
            self.log("signal %s during cleanup: salvage skipped, the stop still ran" % _signal_name(signum))

    def _collect(self, collect: Callable[[], bool]) -> None:
        self._phase = "salvage"
        try:
            ok = bool(collect())
        except _SalvageAborted:
            ok = False
            self.log("collect aborted by a second signal; the stop still runs")
        finally:
            self._phase = "stopping"
        if ok:
            write_json_atomic(self.out_dir / "collect_confirmed.json", {"reason": "done"})

    def _kill_job(self, res: RunResult, resident: bool) -> None:
        """Terminate the job's process group on the VM (vm/kill.py). In resident mode the kernel is first restarted:
        the resident cell holds it busy, and the job survives the restart (it runs as its own session)."""
        if resident:
            r = self._clean(self.colab.restart_kernel(self.session, self.restart_timeout_s))
            self.log("restart-kernel before the kill: rc=%s" % r.rc)
        k = self._clean(self.colab.exec_cell(
            self.session, cell_source("kill"), self._env({"CR_KILL_GRACE_S": str(self.kill_grace_s)}),
            self.kill_timeout_s,
        ))
        text = (k.out or k.err or "").strip()
        res.killed = text.splitlines()[-1] if text else "rc=%s" % k.rc
        self.log("kill: %s" % res.killed)

    def _salvage(self, res: RunResult, salvage: Callable[[str], bool]) -> None:
        res.salvaged = False
        self._phase = "salvage"
        try:
            ok = bool(salvage(res.reason))
        except _SalvageAborted:
            ok = False
            self.log("salvage aborted by a second signal; the stop still runs")
        finally:
            self._phase = "stopping"
        res.salvaged = ok
        if ok:  # marker only after the salvage is confirmed
            write_json_atomic(self.out_dir / "salvage_confirmed.json", {"reason": res.reason})

    # -- signals: raise into the run, never into the stop ----------------------------------------------------------
    def _on_signal(self, signum, frame) -> None:
        if self._phase == "run":
            self._phase = "stopping"
            raise RunInterrupted(signum)
        self._skip_salvage = True
        self._cleanup_signals.append(signum)  # logged after the stop (_end), never from inside the handler
        if self._phase == "salvage":
            raise _SalvageAborted()

    def _arm_signals(self) -> Optional[dict]:
        if threading.current_thread() is not threading.main_thread():
            return None  # only the main thread can take signals; a caller's thread keeps the try/finally
        sigs = [signal.SIGINT, signal.SIGTERM]
        if hasattr(signal, "SIGHUP"):
            sigs.append(signal.SIGHUP)
        old = {}
        for sig in sigs:
            old[sig] = signal.signal(sig, self._on_signal)
        return old

    @staticmethod
    def _disarm_signals(old: Optional[dict]) -> None:
        for sig, handler in (old or {}).items():
            signal.signal(sig, handler)

    # -- stop, with the markers written only after it is confirmed -----------------------------------------------
    def stop_session(self, attempts: Optional[int] = None, timeout_s: Optional[float] = None) -> bool:
        """`colab stop -s <this runner's session>`, retried; no other session is ever stopped. `stop_confirmed.json`
        is written, and `session_open.json` removed, only after a stop returned rc 0. A stop that fails leaves
        `stop_pending.json` (so nothing treats the session as stopped) and returns False for a later retry."""
        attempts = self.stop_attempts if attempts is None else attempts
        timeout_s = self.stop_timeout_s if timeout_s is None else timeout_s
        for i in range(1, attempts + 1):
            last = self._clean(self.colab.stop(self.session, timeout_s))
            if last.ok:
                write_json_atomic(self.out_dir / "stop_confirmed.json", {"session": self.session, "attempt": i})
                for name in ("stop_pending.json", "session_open.json"):
                    (self.out_dir / name).unlink(missing_ok=True)
                self.log("stop confirmed: session %s (attempt %d)" % (self.session, i))
                return True
            write_json_atomic(
                self.out_dir / "stop_pending.json",
                {"session": self.session, "attempts": i, "last_rc": last.rc, "last_err": (last.err or "")[-300:]},
            )
            self.log("stop attempt %d failed rc=%s: %s" % (i, last.rc, (last.err or "")[-300:]))
            if i < attempts:
                self.sleep(_stop_pause(i))
        return False


def _is_run_id(value: Any, mine: str) -> bool:
    """`value` is this run's id: a string equal to `mine` once stripped (a JSON null or a number never is)."""
    return isinstance(value, str) and value.strip() == mine


def _signal_name(signum: int) -> str:
    try:
        return signal.Signals(signum).name
    except ValueError:  # pragma: no cover
        return str(signum)

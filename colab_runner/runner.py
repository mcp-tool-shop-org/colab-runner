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

Kits supply: the job script (or hold seconds), a progress counter (CR_PROGRESS_GLOB / CR_UNITS_GLOB, see vm/lib.py),
and a phase file (`<out>/phase.txt`: "setup" while installing, anything else once running)."""

from __future__ import annotations

import inspect
import json
import math
import os
import signal
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Mapping, Optional

from colab_runner.colab_cli import EXEC_REPORT_S, KILL_WAIT_S, CliResult, Colab, scrub_local
from colab_runner.window_lib import launch_guards as lg

VM_DIR = Path(__file__).parent / "vm"
SETUP_PHASES = (None, "", "setup")
MIN_CALL_S = 1.0  # no call is started with less than this left before the wall or the ceiling
USAGE_RETRY_PAUSE_S = 2.0  # between the tries of a failed `colab usage` read
REACHED_EPS_S = 1e-6  # less than this left: the wall or the ceiling has been reached
# Ends on which the VM job may still be running: its process group is terminated before salvage and stop.
TRIPS = frozenset(
    {"wall", "cap", "setup_stall", "progress_stall", "dead", "unreachable", "start_failed", "interrupted", "error"}
)

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


class Refused(ValueError):
    """A run refused before any `colab` call, by name (`code`)."""

    def __init__(self, code: str, why: str):
        super().__init__("%s: %s" % (code, why))
        self.code, self.why = code, why


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
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        log: Callable[[str], None] = lambda s: None,
    ):
        require_number("POLL_INVALID", "poll_s", poll_s)
        for name, value in (
            ("poll_timeout_s", poll_timeout_s), ("backoff_max_s", backoff_max_s), ("start_timeout_s", start_timeout_s),
            ("usage_timeout_s", usage_timeout_s), ("kill_timeout_s", kill_timeout_s),
            ("restart_timeout_s", restart_timeout_s), ("log_timeout_s", log_timeout_s),
            ("stop_timeout_s", stop_timeout_s),
        ):
            require_number("SETTING_INVALID", name, value)
        require_number("SETTING_INVALID", "kill_grace_s", kill_grace_s, inclusive=True)
        require_number("SETTING_INVALID", "start_attempts", start_attempts, low=1, inclusive=True, integer=True)
        require_number("SETTING_INVALID", "stop_attempts", stop_attempts, low=1, inclusive=True, integer=True)
        require_number("SETTING_INVALID", "usage_retries", usage_retries, inclusive=True, integer=True)
        self.usage_retries = int(usage_retries)
        self.colab, self.session, self.out_dir = colab, session, Path(out_dir)
        self.job, self.content = job, content
        self.poll_s, self.poll_timeout_s, self.backoff_max_s = poll_s, poll_timeout_s, backoff_max_s
        self.start_attempts, self.start_timeout_s = int(start_attempts), start_timeout_s
        self.usage_timeout_s, self.kill_timeout_s, self.kill_grace_s = usage_timeout_s, kill_timeout_s, kill_grace_s
        self.restart_timeout_s, self.log_timeout_s = restart_timeout_s, log_timeout_s
        self.stop_attempts, self.stop_timeout_s = int(stop_attempts), stop_timeout_s
        self.clock, self.sleep = clock, sleep
        self.log = lambda s: log(scrub(s))  # nothing reaches the log unscrubbed
        self._resident = None
        self._cleanup_signals: list[int] = []
        self._phase = "idle"
        self._t0, self._wall_s, self._guard = 0.0, 0.0, None

    # -- environment the cells read -------------------------------------------------------------------------------
    def _env(self, extra: Mapping[str, str] | None = None) -> dict:
        env = {"CR_CONTENT": self.content, "CR_JOB": self.job}
        env.update(extra or {})
        return env

    @staticmethod
    def _clean(res: CliResult) -> CliResult:
        """A CLI result with its out and err scrubbed (the real wrapper scrubs too; this covers any stand-in)."""
        return CliResult(res.rc, scrub(res.out), scrub(res.err))

    def own_cleanup_bound_s(self, resident: bool) -> float:
        """The worst case of the runner's own cleanup after a trip: the kill cell, then (resident mode) the kernel
        restart that frees it and the run-log download, then every stop attempt with the pauses between them; each
        call can take 2 x KILL_WAIT_S more to be killed and reaped. In resident mode the background exec's report
        time (EXEC_REPORT_S, given on top of the window, see _start_resident) and its own kill (KILL_WAIT_S) count
        too. A caller's salvage is the caller's to add."""
        calls = [self.kill_timeout_s] + ([self.restart_timeout_s, self.log_timeout_s] if resident else [])
        calls += [self.stop_timeout_s] * self.stop_attempts
        pauses = sum(_stop_pause(i) for i in range(1, self.stop_attempts))
        resident_extra = EXEC_REPORT_S + KILL_WAIT_S if resident else 0.0
        return sum(calls) + pauses + 2 * KILL_WAIT_S * len(calls) + resident_extra

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
            res = self._clean(self.colab.exec_cell(self.session, cell_source("start"), self._env(env), timeout))
            text = (res.out or res.err or "").strip()
            last = text.splitlines()[-1] if text else ""
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
        full.setdefault("CR_RESIDENT_MAX_S", str(max(1, int(left))))
        try:
            max_s = max(1.0, float(full["CR_RESIDENT_MAX_S"]))
        except ValueError:
            max_s = float(max(1, int(left)))
        self._resident = self.colab.start_cell(self.session, cell_source("resident"), full, max_s + EXEC_REPORT_S)

    # -- poll -----------------------------------------------------------------------------------------------------
    def poll(self, offset: int, timeout: Optional[float] = None) -> Optional[tuple[dict, list[str]]]:
        res = self._clean(self.colab.exec_cell(
            self.session, cell_source("poll"), self._env({"CR_POLL_LOG_OFFSET": str(offset)}),
            self.poll_timeout_s if timeout is None else timeout,
        ))
        return parse_poll(res.out) if res.ok else None

    def _read_usage(self) -> Optional[CliResult]:
        """One `colab usage` reading: a failed read is retried up to `usage_retries` times, each attempt (and the
        pause before it) clamped to the time left before the wall and the ceiling. What is returned is the last try;
        a reading that still failed then folds into COLAB_USAGE_PARSE_FAILED, the library's fail-closed rule. None
        when no attempt fitted."""
        last = None
        for attempt in range(self.usage_retries + 1):
            if attempt:
                self._pause(USAGE_RETRY_PAUSE_S)
            left, _ = self._time_left()
            if left < MIN_CALL_S:
                break
            last = self._clean(self.colab.usage(min(self.usage_timeout_s, left)))
            if last.ok:
                break
            self.log("colab usage attempt %d failed rc=%s: %s" % (attempt + 1, last.rc, (last.err or "")[-200:]))
        return last

    def _download(self, remote: str, local: Path, timeout: float) -> CliResult:
        """`colab download`; whatever is at `local` afterwards is scrubbed in place (scrub_local) before anything
        reads it, in a `finally`: a timed-out, failed or interrupted transfer can leave partial bytes."""
        Path(local).parent.mkdir(parents=True, exist_ok=True)
        try:
            return self._clean(self.colab.download(self.session, remote, str(local), timeout))
        finally:
            scrub_local(local)

    def poll_resident(self, timeout: Optional[float] = None) -> Optional[tuple[dict, list[str]]]:
        local = self.out_dir / ("%s.poll.json" % self.job)
        res = self._download(
            "%s/%s.poll.json" % (self.content.rstrip("/"), self.job), local,
            self.poll_timeout_s if timeout is None else timeout,
        )
        if not res.ok or not local.is_file():
            return None
        try:
            rec = json.loads(local.read_text(encoding="utf-8"))
        except ValueError:
            return None
        return (rec, []) if isinstance(rec, dict) else None

    def _collect_resident_log(self, timeout: float) -> bool:
        """Resident mode: the job's whole run log, downloaded (and scrubbed) into the out dir."""
        local = self.out_dir / ("%s.run.out" % self.job)
        res = self._download("%s/%s.run.out" % (self.content.rstrip("/"), self.job), local, timeout)
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
        unreachable_polls: int = 10,
        unreachable_s: float = 1800.0,
    ) -> RunResult:
        """`guard` (a SpendGuard, or anything with check(elapsed_s) and seconds_left(elapsed_s), and optionally
        fold_usage(...)) ends the run at the cap and bounds every step by its ceiling. Refusals (Refused) are raised
        before any `colab` call; after that, every end goes through the stop."""
        kit_env = self._check_run(script, hold_s, heartbeat_s, env, wall_s, guard, progress_stall_s, setup_stall_s,
                                  dead_confirm, resident, unreachable_polls, unreachable_s)
        hold_mode = hold_s is not None
        self._t0, self._wall_s, self._guard = self.clock(), float(wall_s), guard
        self._resident, self._oom_seen, self._log_collected, self._skip_salvage = None, False, False, False
        self._res = RunResult("error")
        self._cleanup_signals: list[int] = []
        self._phase = "run"
        handlers: Optional[dict] = None
        crash: Optional[BaseException] = None
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
            crash = exc
        finally:
            if self._phase == "run":
                self._phase = "stopping"
            try:
                self._end(self._res, salvage, resident)
            finally:
                self._phase = "idle"
                self._disarm_signals(handlers)
                write_json_atomic(self.out_dir / "runner_result.json", asdict(self._res))
        if crash is not None:
            raise crash
        return self._res

    def _check_run(self, script, hold_s, heartbeat_s, env, wall_s, guard, progress_stall_s, setup_stall_s,
                   dead_confirm, resident, unreachable_polls, unreachable_s) -> dict:
        """Every refusal, by name, before any `colab` call."""
        require_number("WALL_INVALID", "wall_s", wall_s)
        require_number("HEARTBEAT_INVALID", "heartbeat_s", heartbeat_s, inclusive=True)
        require_number("PROGRESS_STALL_INVALID", "progress_stall_s", progress_stall_s)
        require_number("SETUP_STALL_INVALID", "setup_stall_s", setup_stall_s)
        require_number("DEAD_CONFIRM_INVALID", "dead_confirm", dead_confirm, low=1, inclusive=True, integer=True)
        require_number("UNREACHABLE_INVALID", "unreachable_polls", unreachable_polls, low=1, inclusive=True,
                       integer=True)
        require_number("UNREACHABLE_INVALID", "unreachable_s", unreachable_s)
        kit_env = dict(env or {})
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
        if self.out_dir.exists() and (not self.out_dir.is_dir() or any(self.out_dir.iterdir())):
            # the confirmation markers are only true of this run if nothing was there before it (colab_window.sh
            # refuses the same: colab_refuse_nonempty_out_dir)
            raise Refused("OUT_DIR_NOT_EMPTY", "%s already exists and is not an empty directory" % self.out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
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
                return self._end_as("start_failed", "start not confirmed: %s" % last)
        self._loop(wall_s, guard, progress_stall_s, setup_stall_s, dead_confirm, resident, hold_mode,
                   unreachable_polls, unreachable_s)

    def _end_as(self, reason: str, detail: str) -> None:
        self._res.reason, self._res.detail = reason, scrub(detail)
        self._res.elapsed_s = self.clock() - self._t0

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
        last_good_t, file_seen, usage_t = loop_t0, False, None
        run_log = self.out_dir / ("%s.run.out" % self.job)
        fold = getattr(guard, "fold_usage", None)
        while True:
            if self._resident is not None:
                self._resident.poll()  # reaps it, or kills it at its deadline
            if lg.evaluate_wall_guard(elapsed_s=self.clock() - self._t0, wall_s=wall_s, grace_s=0.0)["tripped"]:
                return self._end_as("wall", "wall %.0fs reached" % wall_s)
            if guard is not None:
                if fold is not None and (usage_t is None or self.clock() - usage_t >= self.poll_s):
                    u = self._read_usage()
                    if u is not None:
                        usage_t = self.clock()
                        fold(u.out if u.ok else u.err, rc=u.rc, epoch=usage_t, at=_utc_now())
                trip = guard.check(self.clock() - self._t0)
                if trip:
                    return self._end_as("cap", trip)
            # resident mode: until its poll file has appeared, the start is unverified and has the setup window
            until = loop_t0 + setup_stall_s if resident and not file_seen else None
            left, bound = self._budget(until)
            if left < REACHED_EPS_S:
                if bound == "setup":
                    return self._end_as("start_failed", "the resident poll file did not appear within %.0fs"
                                        % setup_stall_s)
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
                res.max_consecutive_failed = max(res.max_consecutive_failed, failed_run)
                if until is None and (failed_run >= unreachable_polls or now - last_good_t >= unreachable_s):
                    return self._end_as("unreachable", "%d polls failed in a row, %.0fs without a good poll"
                                        % (failed_run, now - last_good_t))
                delay = min(delay * 2, self.backoff_max_s)
                self._pause(delay, until)
                continue
            rec, lines = got
            file_seen = True
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
            in_setup = rec.get("phase") in SETUP_PHASES and not hold_mode
            if in_setup:
                if rec.get("setup_bytes") != last_setup:
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
        except BaseException as exc:
            self.log("cleanup step %s failed: %s: %s" % (name, type(exc).__name__, exc))
            return None

    def _end(self, res: RunResult, salvage: Optional[Callable[[str], bool]], resident: bool) -> None:
        """On a trip, terminate the VM job's process group first; then salvage (every end but done); then stop this
        run's own session, always. Nothing here is clamped to the wall: the cap's cleanup reserve pays for it."""
        if self._resident is not None:
            self._step("resident child", self._resident.kill)
        if res.reason in TRIPS:
            self._step("kill", lambda: self._kill_job(res, resident))
        if resident and not self._log_collected:
            self._step("run log", lambda: self._collect_resident_log(self.log_timeout_s))
            if res.reason == "done" and self._oom_seen:
                res.reason, res.detail = "oom", "the job was OOM-killed: an OOM kill line in the job log (OOM_KILL_RE)"
        if res.reason != "done" and salvage is not None:
            if self._skip_salvage:
                res.salvaged = False
                self.log("salvage skipped: a signal arrived during cleanup")
            else:
                self._step("salvage", lambda: self._salvage(res, salvage))
        self._phase = "stop"
        res.stopped = bool(self._step("stop", self.stop_session))
        for signum in self._cleanup_signals:
            self.log("signal %s during cleanup: salvage skipped, the stop still ran" % _signal_name(signum))

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
        old = {}
        for sig in (signal.SIGINT, signal.SIGTERM):
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


def _signal_name(signum: int) -> str:
    try:
        return signal.Signals(signum).name
    except ValueError:  # pragma: no cover
        return str(signum)

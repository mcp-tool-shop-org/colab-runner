"""The rig-side detached runner.

Starts a job on the VM as its own session (idempotent), then polls it with short, bounded exec calls and backoff.
A failed poll never touches the job. The run ends only on: the job's status file (rc), dead-without-status, a
progress stall, a setup stall, the wall, or the cap. On every non-done end the optional `salvage` callable runs, and a
"stopped" or "salvaged" marker is written only AFTER the action is confirmed (a stop that failed leaves
`stop_pending.json`, never `stop_confirmed.json`, so a later retry still happens).

Kits supply: the job script (or hold seconds), a progress counter (CR_PROGRESS_GLOB / CR_UNITS_GLOB, see vm/lib.py),
and a phase file (`<out>/phase.txt`: "setup" while installing, anything else once running)."""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Callable, Mapping, Optional

from colab_runner.colab_cli import Colab

VM_DIR = Path(__file__).parent / "vm"
SETUP_PHASES = (None, "", "setup")

# A fresh `colab exec` kernel handshake can be slower than the CLI's default request timeout, so a start or poll that
# fails is retried with backoff rather than read as a dead job.


def cell_source(name: str) -> str:
    """lib.py + <name>.py: one self-contained kernel cell."""
    return (VM_DIR / "lib.py").read_text(encoding="utf-8") + "\n" + (VM_DIR / (name + ".py")).read_text(encoding="utf-8")


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
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(obj, indent=2) + "\n", encoding="utf-8", newline="\n")
    os.replace(tmp, path)


@dataclass
class RunResult:
    reason: str  # done | dead | progress_stall | setup_stall | wall | cap | start_failed
    rc: Optional[int] = None
    polls_ok: int = 0
    polls_failed: int = 0
    max_consecutive_failed: int = 0
    last: dict = field(default_factory=dict)
    detail: str = ""
    salvaged: Optional[bool] = None


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
        clock: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        log: Callable[[str], None] = lambda s: None,
    ):
        self.colab, self.session, self.out_dir = colab, session, Path(out_dir)
        self.job, self.content = job, content
        self.poll_s, self.poll_timeout_s, self.backoff_max_s = poll_s, poll_timeout_s, backoff_max_s
        self.start_attempts, self.start_timeout_s = start_attempts, start_timeout_s
        self.clock, self.sleep, self.log = clock, sleep, log
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self._resident: Optional[threading.Thread] = None

    # -- environment the cells read -------------------------------------------------------------------------------
    def _env(self, extra: Mapping[str, str] | None = None) -> dict:
        env = {"CR_CONTENT": self.content, "CR_JOB": self.job}
        env.update(extra or {})
        return env

    # -- start ----------------------------------------------------------------------------------------------------
    def start(self, env: Mapping[str, str]) -> tuple[bool, str]:
        """Start the job; a lost reply is retried and can never start a second job (the cell is idempotent)."""
        delay, last = 2.0, ""
        for attempt in range(1, self.start_attempts + 1):
            res = self.colab.exec_cell(self.session, cell_source("start"), self._env(env), self.start_timeout_s)
            text = (res.out or res.err or "").strip()
            last = text.splitlines()[-1] if text else ""
            if res.ok and ("CR_STARTED" in res.out or "CR_ALREADY_RUNNING" in res.out):
                self.log("start ok (attempt %d): %s" % (attempt, last))
                return True, last
            self.log("start attempt %d failed rc=%s: %s" % (attempt, res.rc, last))
            # a lost reply may still have started the job: the next attempt finds it (CR_ALREADY_RUNNING)
            self.sleep(delay)
            delay = min(delay * 2, self.backoff_max_s)
        return False, last

    def _start_resident(self, env: Mapping[str, str], wall_s: float) -> None:
        full = self._env(env)
        full.setdefault("CR_RESIDENT_MAX_S", str(int(wall_s)))
        src = cell_source("resident")

        def body() -> None:
            self.colab.exec_cell(self.session, src, full, wall_s + 30)

        self._resident = threading.Thread(target=body, daemon=True)
        self._resident.start()

    # -- poll -----------------------------------------------------------------------------------------------------
    def poll(self, offset: int) -> Optional[tuple[dict, list[str]]]:
        res = self.colab.exec_cell(
            self.session, cell_source("poll"), self._env({"CR_POLL_LOG_OFFSET": str(offset)}), self.poll_timeout_s
        )
        return parse_poll(res.out) if res.ok else None

    def poll_resident(self) -> Optional[tuple[dict, list[str]]]:
        local = self.out_dir / ("%s.poll.json" % self.job)
        res = self.colab.download(
            self.session, "%s/%s.poll.json" % (self.content.rstrip("/"), self.job), str(local), self.poll_timeout_s
        )
        if not res.ok or not local.is_file():
            return None
        try:
            return json.loads(local.read_text(encoding="utf-8")), []
        except ValueError:
            return None

    # -- the run --------------------------------------------------------------------------------------------------
    def run(
        self,
        *,
        script: Optional[str] = None,
        hold_s: Optional[float] = None,
        heartbeat_s: float = 30.0,
        env: Mapping[str, str] | None = None,
        wall_s: float,
        cap_check: Optional[Callable[[float], Optional[str]]] = None,
        progress_stall_s: float = 900.0,
        setup_stall_s: float = 600.0,
        dead_confirm: int = 2,
        resident: bool = False,
        salvage: Optional[Callable[[str], bool]] = None,
    ) -> RunResult:
        kit_env = dict(env or {})
        if hold_s is not None:
            kit_env.update({"CR_MODE": "hold", "CR_HOLD_S": str(hold_s), "CR_HEARTBEAT_S": str(heartbeat_s)})
        elif script:
            kit_env.update({"CR_MODE": "script", "CR_SCRIPT": script})
        else:
            raise ValueError("run() needs script= or hold_s=")
        hold_mode = hold_s is not None
        t0 = self.clock()
        if resident:
            self._start_resident(kit_env, wall_s)
            started = True
        else:
            started, _ = self.start(kit_env)
        result = RunResult("start_failed")
        if started:
            result = self._loop(
                t0, wall_s, cap_check, progress_stall_s, setup_stall_s, dead_confirm, resident, hold_mode
            )
        if result.reason != "done" and salvage is not None:
            ok = bool(salvage(result.reason))
            result.salvaged = ok
            if ok:  # marker only after the salvage is confirmed
                write_json_atomic(self.out_dir / "salvage_confirmed.json", {"reason": result.reason})
        write_json_atomic(self.out_dir / "runner_result.json", asdict(result))
        return result

    def _loop(self, t0, wall_s, cap_check, progress_stall_s, setup_stall_s, dead_confirm, resident, hold_mode) -> RunResult:
        offset, delay, failed_run = 0, self.poll_s, 0
        res = RunResult("wall")
        dead_seen, last_key, key_t, last_setup, setup_t, last_phase_setup = 0, None, t0, None, t0, True
        run_log = self.out_dir / ("%s.run.out" % self.job)
        while True:
            now = self.clock()
            elapsed = now - t0
            if elapsed >= wall_s:
                res.reason, res.detail = "wall", "wall %.0fs reached" % wall_s
                return res
            if cap_check is not None:
                trip = cap_check(elapsed)
                if trip:
                    res.reason, res.detail = "cap", trip
                    return res
            got = self.poll_resident() if resident else self.poll(offset)
            if got is None:
                res.polls_failed += 1
                failed_run += 1
                res.max_consecutive_failed = max(res.max_consecutive_failed, failed_run)
                delay = min(delay * 2, self.backoff_max_s)
                self.sleep(delay)
                continue
            rec, lines = got
            res.polls_ok += 1
            failed_run, delay = 0, self.poll_s
            res.last = rec
            if lines:
                with run_log.open("a", encoding="utf-8") as fh:
                    fh.write("\n".join(lines) + "\n")
            offset = int(rec.get("log_offset", offset))
            now = self.clock()
            if rec.get("rc") is not None:
                res.reason, res.rc = "done", int(rec["rc"])
                return res
            if not rec.get("alive"):
                dead_seen += 1
                if dead_seen >= dead_confirm:
                    res.reason, res.detail = "dead", "job not alive and no status file"
                    return res
            else:
                dead_seen = 0
            in_setup = rec.get("phase") in SETUP_PHASES and not hold_mode
            if in_setup:
                if rec.get("setup_bytes") != last_setup:
                    last_setup, setup_t = rec.get("setup_bytes"), now
                elif now - setup_t >= setup_stall_s:
                    res.reason = "setup_stall"
                    res.detail = "setup.log unchanged for %.0fs" % (now - setup_t)
                    return res
            else:
                if last_phase_setup:  # the run phase starts its own stall clock
                    key_t = now
                key = (rec.get("progress"), rec.get("units_done"), rec.get("hold_beats"))
                if key != last_key:
                    last_key, key_t = key, now
                elif now - key_t >= progress_stall_s:
                    res.reason = "progress_stall"
                    res.detail = "no progress for %.0fs" % (now - key_t)
                    return res
            last_phase_setup = in_setup
            if not lines and not resident and not rec.get("log_eof", True):
                continue  # more log to read: poll again at once
            self.sleep(delay)

    # -- stop, with the marker written only after it is confirmed -------------------------------------------------
    def stop_session(self, attempts: int = 3, timeout_s: float = 120.0) -> bool:
        """`colab stop`, retried. `stop_confirmed.json` is written only after a stop returned rc 0. A stop that fails
        leaves `stop_pending.json` (so nothing treats the session as stopped) and returns False for a later retry."""
        last = None
        for i in range(1, attempts + 1):
            last = self.colab.stop(self.session, timeout_s)
            if last.ok:
                pending = self.out_dir / "stop_pending.json"
                if pending.exists():
                    pending.unlink()
                write_json_atomic(self.out_dir / "stop_confirmed.json", {"session": self.session, "attempt": i})
                return True
            write_json_atomic(
                self.out_dir / "stop_pending.json",
                {"session": self.session, "attempts": i, "last_rc": last.rc, "last_err": (last.err or "")[-300:]},
            )
            self.sleep(min(2.0 * i, 10.0))
        return False

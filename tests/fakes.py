"""Test doubles: a deterministic clock, an in-memory stand-in for colab_runner.colab_cli.Colab, and a VM job log
served by the real poll code."""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Optional
from unittest import mock

from colab_runner.colab_cli import CliResult
from colab_runner.runner import VM_DIR, Runner, _scrub_source
from colab_runner.spend import SpendGuard

USAGE_TEXT = "Current balance: {balance:.2f} compute units\nUsage rate: {rate:.2f} / hr\nActive assignments: {n}\n"


def usage_text(rate=0.26, n=1, balance=100.0) -> str:
    return USAGE_TEXT.format(rate=rate, n=n, balance=balance)


def guard(**kw) -> SpendGuard:
    """A guard with a cleanup bound above the runner's own (466 s by default, 966 s resident)."""
    kw.setdefault("max_rate", 0.3)
    kw.setdefault("cap", 50.0)
    kw.setdefault("cleanup_bound_s", 1000.0)
    return SpendGuard(**kw)


class FakeClock:
    def __init__(self):
        self.now_s = 1000.0
        self.sleeps: list[tuple[float, float]] = []  # (when, how long)

    def now(self) -> float:
        return self.now_s

    def sleep(self, s: float) -> None:
        if s < 0:
            raise ValueError("sleep length must be non-negative")  # as time.sleep
        self.sleeps.append((self.now_s, s))
        self.now_s += s

    def advance(self, s: float) -> None:
        self.now_s += s


def rec(**kw) -> dict:
    base = {
        "job": "job", "pid": 11, "alive": True, "rc": None, "ended_at": None, "phase": "run", "step": None,
        "setup_bytes": 100, "progress": 0, "units_done": 0, "hold_beats": 0, "hold_heartbeat_epoch": None,
        "now_epoch": 0.0, "boot_id": "b", "log_offset": 0, "log_eof": True,
    }
    base.update(kw)
    return base


class FakeCall:
    """A background `colab exec` (the resident cell). It runs until `done` is set (the fake sets it once the poll
    file it serves carries an rc), or until it is killed."""

    def __init__(self, result: Optional[CliResult] = None, done: bool = False):
        self.final = result or CliResult(0, "CR_STARTED pid=1\nCR_RESIDENT_DONE\n")
        self.done, self.killed, self.result = done, False, None
        self.kill_calls = 0

    def poll(self) -> Optional[CliResult]:
        if self.result is None and self.done:
            self.result = self.final
        return self.result

    def kill(self) -> CliResult:
        self.kill_calls += 1
        self.poll()  # a call that has ended is only reaped
        if self.result is None:
            self.killed = True
            self.result = CliResult(124, "", "killed")
        return self.result


class FakeColab:
    """`polls` is a callable (poll_number, env) -> record dict, (record, lines), or None for a lost reply.
    `starts` is a list of 'ok' | 'lose_reply' | 'drop' consumed per start call ('ok' after it runs out).
    'lose_reply' runs the start on the VM but loses the answer; 'drop' never reaches the VM.
    `vm_poll_file` (resident mode) is a dict, a string, None (not there), or a callable (download_number) -> one.
    `run_log` is the VM's <job>.run.out for resident downloads. `usage` is the `colab usage` text, or a callable
    (usage_number) -> text or CliResult.
    With a clock, a lost or failed call costs its whole timeout, as a hung call does. Every call is recorded in
    `calls` and `events`; `on_call(kind, fake)` runs at each call (tests raise or signal from it)."""

    def __init__(self, polls=None, starts=None, stops=None, vm_poll_file=None, run_log=None, usage=None,
                 clock: Optional[FakeClock] = None, resident_result: Optional[CliResult] = None,
                 resident_done_at_start: bool = False, on_call=None, max_polls: int = 5000):
        self.polls = polls or (lambda n, env: rec())
        self.starts = list(starts or [])
        self.stop_results = list(stops or [])
        self.vm_poll_file = vm_poll_file
        self.run_log = run_log
        self.usage_reply = usage_text() if usage is None else usage
        self.clock = clock
        self.resident_result, self.resident_done_at_start = resident_result, resident_done_at_start
        self.on_call, self.max_polls = on_call, max_polls
        self.jobs_started = 0
        self.vm_has_job = False
        self.vm_jobs: dict[tuple[str, str], str] = {}
        self._resident_job_exists: Optional[dict] = None
        self.poll_count = self.start_calls = self.resident_calls = self.stop_calls = 0
        self.kill_calls = self.restart_calls = self.usage_count = self.poll_file_count = 0
        self.envs: list[dict] = []
        self.log_lines: list[str] = []
        self.calls: list[dict] = []
        self.events: list[str] = []
        self.stops: list[tuple[str, int]] = []
        self.resident_call: Optional[FakeCall] = None

    def _record(self, kind, session, timeout):
        self.events.append(kind)
        self.calls.append({"kind": kind, "session": session, "timeout": timeout,
                           "at": self.clock.now() if self.clock else None})
        if self.on_call is not None:
            self.on_call(kind, self)

    def _fail(self, timeout, err="RuntimeError: Connection was lost") -> CliResult:
        if self.clock is not None:
            self.clock.advance(timeout)
        return CliResult(1, "", err)

    # the cell is recognised by its environment: poll (CR_POLL_LOG_OFFSET), kill (CR_KILL_GRACE_S), else start
    def exec_cell(self, session, source, env=None, timeout=60.0) -> CliResult:
        env = dict(env or {})
        self.envs.append(env)
        if "CR_KILL_GRACE_S" in env:
            self._record("kill", session, timeout)
            self.kill_calls += 1
            return CliResult(0, "CR_KILLED pid=1\n")
        if "CR_POLL_LOG_OFFSET" in env:
            self._record("poll", session, timeout)
            self.poll_count += 1
            if self.poll_count > self.max_polls:
                raise AssertionError("more than %d polls: the loop is spinning" % self.max_polls)
            r = self.polls(self.poll_count, env)
            if r is None:
                return self._fail(timeout)
            r, lines = r if isinstance(r, tuple) else (r, self.log_lines)
            out = "".join("CR_LOG %s\n" % ln for ln in lines) + "CR_POLL " + json.dumps(r) + "\n"
            return CliResult(0, out)
        self._record("start", session, timeout)
        self.start_calls += 1
        action = self.starts.pop(0) if self.starts else "ok"
        if action == "drop":
            return self._fail(timeout)
        key = (env.get("CR_CONTENT", "/content"), env.get("CR_JOB", "job"))
        run_id = env.get("CR_RUN_ID", "")
        if self.vm_has_job and not self.vm_jobs:
            out = "CR_ALREADY_RUNNING pid=1\n"
        elif key in self.vm_jobs:
            if run_id and self.vm_jobs[key] != run_id:
                out = "CR_JOB_EXISTS job=%s pid=1\n" % key[1]
            else:
                out = "CR_ALREADY_RUNNING pid=1\n"
        else:
            self.vm_jobs[key] = run_id
            self.vm_has_job = True
            self.jobs_started += 1
            out = "CR_STARTED pid=1\n"
        if action == "lose_reply":
            return self._fail(timeout)
        return CliResult(0, out)

    def start_cell(self, session, source, env=None, timeout=60.0) -> FakeCall:
        env = dict(env or {})
        self.envs.append(env)
        self._record("resident", session, timeout)
        self.resident_calls += 1
        key = (env.get("CR_CONTENT", "/content"), env.get("CR_JOB", "job"))
        run_id = env.get("CR_RUN_ID", "")
        if key in self.vm_jobs and run_id and self.vm_jobs[key] != run_id:
            self._resident_job_exists = {
                "job": key[1], "error": "JOB_ALREADY_EXISTS", "detail": "CR_JOB_EXISTS job=%s pid=1" % key[1],
            }
            self.resident_call = FakeCall(
                CliResult(0, "CR_JOB_EXISTS job=%s pid=1\nCR_RESIDENT_DONE\n" % key[1]), done=True,
            )
            return self.resident_call
        self._resident_job_exists = None
        self.vm_jobs[key] = run_id
        self.jobs_started += 1
        self.vm_has_job = True
        self.resident_call = FakeCall(self.resident_result, done=self.resident_done_at_start)
        return self.resident_call

    def download(self, session, remote, local, timeout=120.0) -> CliResult:
        self._record("download", session, timeout)
        name = remote.rsplit("/", 1)[-1]
        if name.endswith(".poll.json"):
            self.poll_file_count += 1
            if self._resident_job_exists is not None:
                data = self._resident_job_exists
            else:
                data = self.vm_poll_file(self.poll_file_count) if callable(self.vm_poll_file) else self.vm_poll_file
            if isinstance(data, dict) and data.get("rc") is not None and self.resident_call is not None:
                self.resident_call.done = True  # the resident cell ends once the job's status is in
        elif name.endswith(".run.out"):
            data = self.run_log
        else:
            data = None
        if data is None:
            return self._fail(timeout, "no such file: %s" % remote)
        if isinstance(data, bytes):
            Path(local).write_bytes(data)
        else:
            Path(local).write_text(data if isinstance(data, str) else json.dumps(data), encoding="utf-8")
        return CliResult(0)

    def stop(self, session, timeout=120.0) -> CliResult:
        self._record("stop", session, timeout)
        self.stop_calls += 1
        res = self.stop_results.pop(0) if self.stop_results else 0
        self.stops.append((session, res))
        return CliResult(res, "", "" if res == 0 else "stop failed")

    def restart_kernel(self, session, timeout=180.0) -> CliResult:
        self._record("restart", session, timeout)
        self.restart_calls += 1
        return CliResult(0, "restarted")

    def usage(self, timeout=60.0) -> CliResult:
        self._record("usage", None, timeout)
        self.usage_count += 1
        got = self.usage_reply(self.usage_count) if callable(self.usage_reply) else self.usage_reply
        if isinstance(got, CliResult):
            return got if got.ok else self._fail(timeout, got.err)
        return CliResult(0, got)

    def confirmed_stops(self) -> list[str]:
        return [s for s, rc in self.stops if rc == 0]


def make(colab: FakeColab, tmp, **kw) -> tuple[Runner, FakeClock]:
    """A Runner for session "sess" on `colab`, with a fake clock it shares with the fake; its out dir is a new
    folder under `tmp`."""
    if colab.clock is None:
        colab.clock = FakeClock()
    clock = colab.clock
    runner = Runner(colab, "sess", Path(tmp) / "out", clock=clock.now, sleep=clock.sleep, **kw)
    return runner, clock


def exec_vm_lib(ns: dict) -> None:
    """Compile and execute `_scrub_source()` + `colab_runner/vm/lib.py` at its real path for coverage."""
    exec(compile(_scrub_source(), "<scrub_source>", "exec"), ns)
    lib = VM_DIR / "lib.py"
    exec(compile(lib.read_text(encoding="utf-8"), str(lib), "exec"), ns)


class VmLog:
    """A VM job log served by the real poll code: vm/lib.py's cr_collect, exec'd here against a temp folder (no pid
    file there, so no process is ever signalled)."""

    def __init__(self, folder, job="job"):
        self.folder, self.job = Path(folder), job
        with mock.patch.dict(os.environ, {"CR_CONTENT": str(self.folder), "CR_JOB": job}):
            self.ns: dict = {"__name__": "cr_vm_lib"}
            exec_vm_lib(self.ns)

    def write(self, data: bytes) -> None:
        (self.folder / (self.job + ".run.out")).write_bytes(data)

    def poll(self, offset: int, **over) -> tuple[dict, list[str]]:
        """(record, lines) for a poll at `offset`: the VM's chunk, with the record's job fields from `over`."""
        got, lines = self.ns["cr_collect"](offset)
        r = rec(log_offset=got["log_offset"], log_eof=got["log_eof"])
        r.update(over)
        return r, lines

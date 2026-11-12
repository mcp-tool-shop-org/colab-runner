"""Test doubles: a deterministic clock and an in-memory stand-in for colab_runner.colab_cli.Colab."""

from __future__ import annotations

import json
from pathlib import Path

from colab_runner.colab_cli import CliResult


class FakeClock:
    def __init__(self):
        self.now_s = 1000.0

    def now(self) -> float:
        return self.now_s

    def sleep(self, s: float) -> None:
        self.now_s += s


def rec(**kw) -> dict:
    base = {
        "job": "job", "pid": 11, "alive": True, "rc": None, "ended_at": None, "phase": "run", "step": None,
        "setup_bytes": 100, "progress": 0, "units_done": 0, "hold_beats": 0, "hold_heartbeat_epoch": None,
        "now_epoch": 0.0, "boot_id": "b", "log_offset": 0, "log_eof": True,
    }
    base.update(kw)
    return base


class FakeColab:
    """`polls` is a callable (poll_number, env) -> record dict, or None for a lost reply.
    `starts` is a list of 'ok' | 'lose_reply' | 'drop' consumed per start call ('ok' after it runs out).
    'lose_reply' runs the start on the VM but loses the answer; 'drop' never reaches the VM."""

    def __init__(self, polls=None, starts=None, stops=None, vm_poll_file: dict | None = None):
        self.polls = polls or (lambda n, env: rec())
        self.starts = list(starts or [])
        self.stop_results = list(stops or [])
        self.vm_poll_file = vm_poll_file
        self.jobs_started = 0
        self.vm_has_job = False
        self.poll_count = 0
        self.start_calls = 0
        self.resident_calls = 0
        self.stop_calls = 0
        self.envs: list[dict] = []
        self.log_lines: list[str] = []

    # the cell is recognised by what it runs, not by its file name
    def exec_cell(self, session, source, env=None, timeout=60.0) -> CliResult:
        env = dict(env or {})
        self.envs.append(env)
        if "CR_RESIDENT_DONE" in source:
            self.resident_calls += 1
            self.jobs_started += 1
            self.vm_has_job = True
            return CliResult(0, "CR_STARTED pid=1\nCR_RESIDENT_DONE\n")
        if "CR_POLL_LOG_OFFSET" in env:
            self.poll_count += 1
            r = self.polls(self.poll_count, env)
            if r is None:
                return CliResult(1, "", "RuntimeError: Connection was lost")
            out = "".join("CR_LOG %s\n" % ln for ln in self.log_lines) + "CR_POLL " + json.dumps(r) + "\n"
            return CliResult(0, out)
        self.start_calls += 1
        action = self.starts.pop(0) if self.starts else "ok"
        if action == "drop":
            return CliResult(1, "", "RuntimeError: Connection was lost")
        if self.vm_has_job:
            out = "CR_ALREADY_RUNNING pid=1\n"
        else:
            self.vm_has_job = True
            self.jobs_started += 1
            out = "CR_STARTED pid=1\n"
        if action == "lose_reply":
            return CliResult(1, "", "RuntimeError: Connection was lost")
        return CliResult(0, out)

    def download(self, session, remote, local, timeout=120.0) -> CliResult:
        if self.vm_poll_file is None:
            return CliResult(1, "", "no such file")
        data = self.vm_poll_file if isinstance(self.vm_poll_file, str) else json.dumps(self.vm_poll_file)
        Path(local).write_text(data, encoding="utf-8")
        return CliResult(0)

    def stop(self, session, timeout=120.0) -> CliResult:
        self.stop_calls += 1
        res = self.stop_results.pop(0) if self.stop_results else 0
        return CliResult(res, "", "" if res == 0 else "stop failed")

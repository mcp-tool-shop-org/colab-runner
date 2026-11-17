"""A thin, bounded wrapper over the `colab` CLI.

- Every call is bounded by its timeout: the CLI runs in a process group of its own, and a call that outlives its
  timeout is killed with that whole group (`os.killpg`), so a grandchild holding the pipes dies with it. It reports
  rc 124. A call interrupted by anything else (a signal, an exception) is killed the same way before the exception
  goes on.
- Every `out` and `err` is passed through the vendored library's token scrub (`launch_guards.scrub_colab_tokens`)
  before any caller sees it, and every downloaded file is scrubbed in place (`launch_guards.scrub_file_in_place`)
  before the call returns.
- A cell's environment travels inside the cell file, written owner-only (0600) where the OS has file modes, never
  as `colab exec --env` arguments, so no value reaches a command line.

The wrapper never parses CLI output beyond what its callers ask for, and never decides anything."""

from __future__ import annotations

import os
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Sequence

from colab_runner.window_lib import launch_guards as lg

TIMEOUT_RC = 124
KILL_WAIT_S = 5.0  # after a group kill: how long to wait for the pipes to close and the call to be reaped
EXEC_REPORT_S = 15.0  # of an exec call's bound, the time kept back for the CLI to report a kernel-side timeout


def scrub(text: Optional[str]) -> str:
    return lg.scrub_colab_tokens(text or "")


@dataclass
class CliResult:
    rc: int
    out: str = ""
    err: str = ""

    @property
    def timed_out(self) -> bool:
        return self.rc == TIMEOUT_RC

    @property
    def ok(self) -> bool:
        return self.rc == 0


def _own_group() -> dict:
    """Popen arguments that give the CLI a process group of its own."""
    if os.name == "posix":
        return {"start_new_session": True}
    return {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}  # pragma: no cover - Windows only; CI runs Linux


def kill_group(proc: subprocess.Popen) -> None:
    """SIGKILL the call's whole process group (POSIX), then the call itself."""
    if os.name == "posix":
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
    try:
        proc.kill()
    except OSError:  # pragma: no cover - the process is already gone
        pass


def _drain(proc: subprocess.Popen) -> str:
    """After a group kill: the rest of the call's stdout, if its pipes close within KILL_WAIT_S. A process outside
    the group can still hold a pipe; then its output is given up, the pipes are closed and the call returns."""
    try:
        out, _ = proc.communicate(timeout=KILL_WAIT_S)
        return out or ""
    except subprocess.TimeoutExpired:
        for pipe in (proc.stdout, proc.stderr):
            try:
                pipe.close()
            except OSError:  # pragma: no cover
                pass
        try:
            proc.wait(timeout=KILL_WAIT_S)
        except subprocess.TimeoutExpired:  # pragma: no cover - a SIGKILLed child is reaped at once
            pass
        return ""


def kernel_timeout(timeout: float) -> int:
    """The `colab exec --timeout` for a call bounded at `timeout` seconds: the bound, less the CLI's report time."""
    return max(1, int(timeout - min(EXEC_REPORT_S, timeout / 2.0)))


def env_preamble(env: Mapping[str, str] | None) -> str:
    """Python that sets `env` in the cell's os.environ before anything else in the cell runs."""
    if not env:
        return ""
    pairs = {str(k): str(v) for k, v in env.items()}
    return "import os as _cr_os\n_cr_os.environ.update(%r)\ndel _cr_os\n" % (pairs,)


def write_cell(folder: Path, source: str, env: Mapping[str, str] | None) -> Path:
    """The cell file: `env` first (env_preamble), then `source`. Created owner-only (0600) where the OS has file
    modes, inside a folder tempfile makes owner-only too; on Windows the mode only sets the read-only bit."""
    path = Path(folder) / "cell.py"
    fd = os.open(str(path), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(env_preamble(env) + source)
    return path


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:  # pragma: no cover
        return ""


class BackgroundCall:
    """One `colab exec` running in the background (the resident fallback), in a process group of its own, its output
    going to files (a pipe nobody reads would fill and stall it). `poll()` reaps it, or kills it once its deadline
    has passed; `kill()` kills its whole process group. When it ends, its output is read (scrubbed) and the temp
    folder holding the cell file is removed."""

    def __init__(self, proc, tmp, deadline: float, result: Optional[CliResult] = None):
        self.proc, self._tmp, self.deadline = proc, tmp, deadline
        self.result: Optional[CliResult] = result
        self.killed = False

    def poll(self) -> Optional[CliResult]:
        if self.result is None:
            if self.proc.poll() is not None:
                self._finish(self.proc.returncode)
            elif time.monotonic() >= self.deadline:
                self._kill(TIMEOUT_RC)
        return self.result

    def kill(self) -> CliResult:
        """Kill the call's process group, unless it has already ended (then it is only reaped)."""
        if self.result is None and self.proc.poll() is not None:
            self._finish(self.proc.returncode)
        if self.result is None:
            self._kill(None)
        return self.result

    def _kill(self, rc: Optional[int]) -> None:
        self.killed = True
        kill_group(self.proc)
        try:
            self.proc.wait(timeout=KILL_WAIT_S)
        except subprocess.TimeoutExpired:  # pragma: no cover - a SIGKILLed child is reaped at once
            pass
        self._finish(rc if rc is not None else (self.proc.returncode if self.proc.returncode is not None else -9))

    def _finish(self, rc: int) -> None:
        folder = Path(self._tmp.name)
        out, err = _read(folder / "out.txt"), _read(folder / "err.txt")
        self._tmp.cleanup()
        self.result = CliResult(int(rc), scrub(out), scrub(err))


class Colab:
    """`argv` is the command prefix, default ["colab"]; tests pass [sys.executable, "fake_colab.py"]."""

    def __init__(self, argv: Sequence[str] = ("colab",), env: Mapping[str, str] | None = None):
        self.argv = list(argv)
        self.env = dict(os.environ if env is None else env)

    def run(self, args: Sequence[str], timeout: float) -> CliResult:
        """One call, bounded at `timeout` seconds (plus at most 2 x KILL_WAIT_S to kill and reap it)."""
        try:
            proc = subprocess.Popen(
                self.argv + list(args), stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                text=True, encoding="utf-8", errors="replace", env=self.env, **_own_group(),
            )
        except OSError as exc:
            return CliResult(127, "", scrub(str(exc)))
        try:
            out, err = proc.communicate(timeout=max(0.0, float(timeout)))
        except subprocess.TimeoutExpired:
            kill_group(proc)
            return CliResult(TIMEOUT_RC, scrub(_drain(proc)), "timeout after %ss" % timeout)
        except BaseException:
            kill_group(proc)
            _drain(proc)
            raise
        return CliResult(proc.returncode, scrub(out), scrub(err))

    def exec_cell(
        self, session: str, source: str, env: Mapping[str, str] | None = None, timeout: float = 60.0
    ) -> CliResult:
        """Run `source` as a kernel cell (`colab exec -f`); the cell sees `env` as os.environ entries, set from the
        cell file itself (write_cell), never from the command line."""
        with tempfile.TemporaryDirectory(prefix="colab-runner-") as tmp:
            cell = write_cell(Path(tmp), source, env)
            return self.run(["exec", "-s", session, "-f", str(cell), "--timeout", str(kernel_timeout(timeout))], timeout)

    def start_cell(
        self, session: str, source: str, env: Mapping[str, str] | None = None, timeout: float = 60.0
    ) -> BackgroundCall:
        """`exec_cell` in the background: returns at once with a handle the caller polls and kills."""
        tmp = tempfile.TemporaryDirectory(prefix="colab-runner-")
        folder = Path(tmp.name)
        cell = write_cell(folder, source, env)
        args = ["exec", "-s", session, "-f", str(cell), "--timeout", str(kernel_timeout(timeout))]
        proc, error = None, None
        with (folder / "out.txt").open("w", encoding="utf-8") as out_fh, (folder / "err.txt").open(
            "w", encoding="utf-8"
        ) as err_fh:
            try:
                proc = subprocess.Popen(
                    self.argv + args, stdin=subprocess.DEVNULL, stdout=out_fh, stderr=err_fh, env=self.env,
                    **_own_group(),
                )
            except OSError as exc:
                error = exc
        if proc is None:  # the files are closed now, so the folder can go (Windows keeps open files)
            tmp.cleanup()
            return BackgroundCall(None, None, 0.0, CliResult(127, "", scrub(str(error))))
        return BackgroundCall(proc, tmp, time.monotonic() + max(0.0, float(timeout)))

    def download(self, session: str, remote: str, local: str, timeout: float = 120.0) -> CliResult:
        """`colab download`; whatever is at `local` afterwards is scrubbed in place (scrub_file_in_place) before the
        call returns or raises, whatever the rc: a timed-out, failed or interrupted transfer can leave partial bytes."""
        try:
            return self.run(["download", "-s", session, remote, local], timeout)
        finally:
            scrub_local(local)

    def stop(self, session: str, timeout: float = 120.0) -> CliResult:
        return self.run(["stop", "-s", session], timeout)

    def restart_kernel(self, session: str, timeout: float = 180.0) -> CliResult:
        return self.run(["restart-kernel", "-s", session], timeout)

    def usage(self, timeout: float = 60.0) -> CliResult:
        return self.run(["usage"], timeout)


def scrub_local(local) -> None:
    """scrub_file_in_place on `local`, if anything is there; a file that cannot be read or rewritten is removed."""
    path = Path(local)
    try:
        lg.scrub_file_in_place(path)
    except OSError:
        try:
            path.unlink()
        except OSError:  # pragma: no cover - it cannot be touched at all
            pass


def cli_from_env() -> Colab:
    """The CLI named by COLAB_RUNNER_CLI (a command line split on spaces), else `colab`."""
    raw = os.environ.get("COLAB_RUNNER_CLI", "").strip()
    return Colab(raw.split() if raw else ["colab"])


__all__ = [
    "BackgroundCall", "Colab", "CliResult", "KILL_WAIT_S", "TIMEOUT_RC", "cli_from_env", "env_preamble",
    "kernel_timeout", "kill_group", "scrub", "scrub_local", "write_cell",
]

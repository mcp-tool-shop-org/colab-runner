"""A thin, bounded wrapper over the `colab` CLI. Every call has a timeout; a timed-out call is killed and reports
rc 124. The wrapper never parses CLI output beyond what its callers ask for, and never decides anything."""

from __future__ import annotations

import os
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Sequence

TIMEOUT_RC = 124


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


class Colab:
    """`argv` is the command prefix, default ["colab"]; tests pass [sys.executable, "fake_colab.py"]."""

    def __init__(self, argv: Sequence[str] = ("colab",), env: Mapping[str, str] | None = None):
        self.argv = list(argv)
        self.env = dict(os.environ if env is None else env)

    def run(self, args: Sequence[str], timeout: float) -> CliResult:
        try:
            proc = subprocess.run(
                self.argv + list(args), capture_output=True, text=True, encoding="utf-8",
                stdin=subprocess.DEVNULL, timeout=timeout, env=self.env, check=False,
            )
        except subprocess.TimeoutExpired as exc:
            out = exc.stdout.decode("utf-8", "ignore") if isinstance(exc.stdout, bytes) else (exc.stdout or "")
            return CliResult(TIMEOUT_RC, out, "timeout after %ss" % timeout)
        except OSError as exc:
            return CliResult(127, "", str(exc))
        return CliResult(proc.returncode, proc.stdout, proc.stderr)

    def exec_cell(
        self, session: str, source: str, env: Mapping[str, str] | None = None, timeout: float = 60.0
    ) -> CliResult:
        """Run `source` as a kernel cell (`colab exec -f`); the cell sees `env` as os.environ entries."""
        with tempfile.TemporaryDirectory(prefix="colab-runner-") as tmp:
            cell = Path(tmp) / "cell.py"
            cell.write_text(source, encoding="utf-8", newline="\n")
            args = ["exec", "-s", session, "-f", str(cell), "--timeout", str(int(max(1, timeout)))]
            for key, val in (env or {}).items():
                args += ["--env", "%s=%s" % (key, val)]
            return self.run(args, timeout + 15)

    def download(self, session: str, remote: str, local: str, timeout: float = 120.0) -> CliResult:
        return self.run(["download", "-s", session, remote, local], timeout)

    def stop(self, session: str, timeout: float = 120.0) -> CliResult:
        return self.run(["stop", "-s", session], timeout)

    def restart_kernel(self, session: str, timeout: float = 180.0) -> CliResult:
        return self.run(["restart-kernel", "-s", session], timeout)

    def usage(self, timeout: float = 60.0) -> CliResult:
        return self.run(["usage"], timeout)


def cli_from_env() -> Colab:
    """The CLI named by COLAB_RUNNER_CLI (a command line split on spaces), else `colab`."""
    raw = os.environ.get("COLAB_RUNNER_CLI", "").strip()
    return Colab(raw.split() if raw else ["colab"])


__all__ = ["Colab", "CliResult", "TIMEOUT_RC", "cli_from_env"]

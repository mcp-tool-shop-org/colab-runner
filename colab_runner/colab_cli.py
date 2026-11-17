"""A thin, bounded wrapper over the `colab` CLI.

- Every call is bounded by its timeout: the CLI runs in a process group of its own, and a call that outlives its
  timeout is killed with that whole group (`os.killpg`), so a grandchild holding the pipes dies with it. It reports
  rc 124. A call interrupted by anything else (a signal, an exception) is killed the same way before the exception
  goes on.
- Every `out` and `err` is passed through the vendored library's token scrub (`launch_guards.scrub_colab_tokens`)
  before any caller sees it. Every downloaded file is checked by content: text is scrubbed in place with
  `launch_guards.scrub_colab_tokens`; binary files (containing NUL or failing strict UTF-8 decoding) are never
  rewritten — clean binaries stay byte-identical, while a token match (`binary_token_hit`) moves the file to
  quarantine and raises `BinaryDownloadRefused` (`BINARY_DOWNLOAD_TOKEN`). The quarantine is the caller's
  `quarantine_dir` (the Runner binds `<out-dir>.quarantine`); with none given, a new folder under the system temp
  dir (`bare_quarantine_dir`), never next to the downloaded file.
- A cell's environment travels inside the cell file, written owner-only (0600) where the OS has file modes, never
  as `colab exec --env` arguments, so no value reaches a command line.

The wrapper never parses CLI output beyond what its callers ask for, and never decides anything."""

from __future__ import annotations

import io
import os
import re
import shlex
import shutil
import signal
import subprocess
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Mapping, Optional, Sequence

from colab_runner.window_lib import launch_guards as lg

TIMEOUT_RC = 124
KILL_WAIT_S = 5.0  # after a group kill: how long to wait for the pipes to close and the call to be reaped
EXEC_REPORT_S = 15.0  # of an exec call's bound, the time kept back for the CLI to report a kernel-side timeout


def _pattern_name(pattern: re.Pattern[str], idx: int) -> str:
    cleaned = pattern.pattern.replace(r"\b", "").replace(r"\s", " ")
    m = re.search(r"[A-Za-z][A-Za-z0-9_-]*", cleaned)
    return m.group(0) if m else "pattern_%d" % idx


# Every character a str pattern's `\s` matches. They all lie below U+3001 (the tests check the whole code space).
_STR_SPACES = [chr(c) for c in range(0x3001) if re.fullmatch(r"\s", chr(c))]


def _hex(data: bytes) -> bytes:
    return b"".join(b"\\x%02x" % b for b in data)


_SPACE_ONE = b"".join(_hex(ch.encode("utf-8")) for ch in _STR_SPACES if len(ch.encode("utf-8")) == 1)
_SPACE_MULTI = b"|".join(_hex(ch.encode("utf-8")) for ch in _STR_SPACES if len(ch.encode("utf-8")) > 1)
_SPACE_ANY = b"(?:[" + _SPACE_ONE + b"]|" + _SPACE_MULTI + b")"  # str `\s`, as UTF-8 bytes


def _byte_twin_source(src: str) -> bytes:
    r"""A str pattern's source as a bytes pattern that matches, on UTF-8 input, exactly what the str pattern matches
    on the decoded text. `\s` (outside or inside a class) becomes the str class in UTF-8 (`_SPACE_ANY`: \x1c-\x1f,
    NEL, NBSP, U+1680, U+2000-U+200A, U+2028/9, U+202F, U+205F, U+3000 included). `\b` is kept: over bytes it
    sees a non-ASCII character as a non-word byte, so next to a non-ASCII letter it matches where the str `\b` does
    not (a superset, never a miss). Any other class shorthand is refused, so a library change cannot slip through."""
    out: list[bytes] = []
    i, n = 0, len(src)
    while i < n:
        c = src[i]
        if c == "\\":
            esc = src[i:i + 2]
            if esc == "\\s":
                out.append(_SPACE_ANY)
            elif esc[1:] in ("S", "w", "W", "d", "D"):
                raise ValueError("no byte twin for %r in %r" % (esc, src))
            else:
                out.append(esc.encode("ascii"))
            i += 2
            continue
        if c == "[":
            j = i + 1
            neg = j < n and src[j] == "^"
            j += 1 if neg else 0
            items: list[str] = []
            if j < n and src[j] == "]":  # a leading ] is a literal
                items.append("\\]")
                j += 1
            has_space = False
            while j < n and src[j] != "]":
                if src[j] == "\\":
                    esc = src[j:j + 2]
                    if esc == "\\s":
                        has_space = True
                    elif esc[1:] in ("S", "w", "W", "d", "D"):
                        raise ValueError("no byte twin for %r in %r" % (esc, src))
                    else:
                        items.append(esc)
                    j += 2
                    continue
                if ord(src[j]) > 0x7F:
                    raise ValueError("no byte twin for a non-ASCII class member in %r" % src)
                items.append(src[j])
                j += 1
            body = "".join(items).encode("ascii")
            if not has_space:
                out.append(b"[" + (b"^" if neg else b"") + body + b"]")
            elif neg:  # none of the members, and no str space (one byte, or a whole multi-byte one)
                out.append(b"(?:(?!" + _SPACE_MULTI + b")[^" + body + _SPACE_ONE + b"])")
            else:
                out.append(b"(?:[" + body + _SPACE_ONE + b"]|" + _SPACE_MULTI + b")")
            i = j + 1
            continue
        out.append(c.encode("utf-8"))
        i += 1
    return b"".join(out)


# The byte twins of the vendored library's TOKEN_SCRUB_PATTERNS, derived from their sources (never copied).
BYTE_TOKEN_PATTERNS: list[tuple[str, re.Pattern[bytes]]] = [
    (_pattern_name(p, i), re.compile(_byte_twin_source(p.pattern), p.flags & ~re.UNICODE))
    for i, (p, _) in enumerate(lg.TOKEN_SCRUB_PATTERNS)
]

# The binary scan's one departure from the twins: `Bearer` + space + a word is ordinary prose ("Flame Bearer of the
# North"), and a render's metadata carries its prompt. In a binary, a Bearer hit counts only when the run after the
# spaces is token-shaped: at least BEARER_TOKEN_MIN characters of the pattern's own class [A-Za-z0-9._-], using at
# least two of lower case, upper case and digits. Every bearer credential the runner can meet is that shape: a
# Google OAuth access token (`ya29.` and well over 100 characters), a JWT (`eyJ`..., three base64url parts), a
# Hugging Face token (`hf_` and 34 mixed-case letters, also caught by the `hf_` pattern on its own). Text files
# keep the library's scrub unchanged.
BEARER_NAME = "Bearer"
BEARER_TOKEN_MIN = 20
_LOWER, _UPPER, _DIGIT = re.compile(rb"[a-z]"), re.compile(rb"[A-Z]"), re.compile(rb"[0-9]")


def token_shaped(run: bytes) -> bool:
    """A Bearer value that looks like a credential, not a word (see BEARER_TOKEN_MIN)."""
    if len(run) < BEARER_TOKEN_MIN:
        return False
    return sum(1 for kind in (_LOWER, _UPPER, _DIGIT) if kind.search(run)) >= 2


def _bearer_hit(bpat: re.Pattern[bytes], data: bytes) -> bool:
    pos = 0
    while True:
        m = bpat.search(data, pos)
        if m is None:
            return False
        if token_shaped(data[m.end(1):m.end()]):
            return True
        pos = m.start() + 1  # a later Bearer inside this match is tried too


def binary_token_hit(data: bytes) -> Optional[str]:
    """The name of the first token pattern a binary file's bytes match, or None: every byte twin, with the Bearer
    twin's value required to be token-shaped."""
    for name, bpat in BYTE_TOKEN_PATTERNS:
        if name == BEARER_NAME:
            if _bearer_hit(bpat, data):
                return name
        elif bpat.search(data):
            return name
    return None


class BinaryDownloadRefused(RuntimeError):
    """A downloaded binary file matched a token pattern: quarantined unchanged, never rewritten, and refused."""

    def __init__(self, quarantined: list[dict[str, str]]):
        self.code = "BINARY_DOWNLOAD_TOKEN"
        self.quarantined = list(quarantined)
        summary = ", ".join("%s (pattern=%s)" % (q["source"], q["pattern"]) for q in self.quarantined)
        super().__init__("%s: binary download quarantined: %s" % (self.code, summary))


def default_quarantine_dir(target: Path) -> Path:
    """Sibling `<target>.quarantine` directory, outside `target` itself (the Runner's, for its out dir)."""
    p = Path(target)
    if not p.name:  # pragma: no cover - empty path resolved to concrete name
        p = p.resolve()
    return p.parent / (p.name + ".quarantine")


QUARANTINE_PREFIX = "colab-runner-quarantine-"


def bare_quarantine_dir() -> Path:
    """The quarantine of a download with no quarantine_dir (a bare `Colab.download` or `scrub_local`): a new folder
    `colab-runner-quarantine-*` under the system temp dir (`tempfile.mkdtemp`, owner-only), made at the first refused
    file. Never next to the file, so never inside the folder it was downloaded into."""
    return Path(tempfile.mkdtemp(prefix=QUARANTINE_PREFIX))


def is_binary_bytes(data: bytes) -> bool:
    """Decide text vs binary by content only (never by extension): NUL byte or invalid UTF-8 is binary."""
    if b"\x00" in data:
        return True
    try:
        data.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return True
    return False


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

    def __init__(
        self,
        argv: Sequence[str] = ("colab",),
        env: Mapping[str, str] | None = None,
        *,
        quarantine_dir: Optional[Path] = None,
        log: Optional[Callable[[str], None]] = None,
        on_quarantine: Optional[Callable[[dict[str, str]], None]] = None,
    ):
        self.argv = list(argv)
        self.env = dict(os.environ if env is None else env)
        self.quarantine_dir = Path(quarantine_dir) if quarantine_dir is not None else None
        self.log = log
        self.on_quarantine = on_quarantine
        self._base_dir: Optional[Path] = None

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
        """`colab download`; whatever is at `local` afterwards runs through `scrub_local` before the call returns or
        raises, whatever the rc: a timed-out, failed or interrupted transfer can leave partial bytes."""
        try:
            return self.run(["download", "-s", session, remote, local], timeout)
        finally:
            scrub_local(
                local, quarantine_dir=self.quarantine_dir, base_dir=self._base_dir,
                log=self.log, on_quarantine=self.on_quarantine,
            )

    def stop(self, session: str, timeout: float = 120.0) -> CliResult:
        return self.run(["stop", "-s", session], timeout)

    def restart_kernel(self, session: str, timeout: float = 180.0) -> CliResult:
        return self.run(["restart-kernel", "-s", session], timeout)

    def usage(self, timeout: float = 60.0) -> CliResult:
        return self.run(["usage"], timeout)


def _unlink_quiet(path: Path) -> None:
    try:
        path.unlink()
    except OSError:  # pragma: no cover - it cannot be touched at all
        pass


def _iter_files(root: Path) -> list[Path]:
    if root.is_symlink():
        _unlink_quiet(root)
        return []
    if not root.exists():
        return []
    if root.is_file():
        return [root]
    if not root.is_dir():  # pragma: no cover - special device or FIFO
        return []
    files: list[Path] = []
    for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
        parent = Path(dirpath)
        dirnames[:] = sorted(d for d in dirnames if not (parent / d).is_symlink())
        for name in sorted(filenames):
            p = parent / name
            if p.is_symlink():
                _unlink_quiet(p)
            else:
                files.append(p)
    return files


def _quarantine_dest(src: Path, root: Path, quarantine_dir: Path, base_dir: Optional[Path]) -> Path:
    rel: Optional[Path] = None
    if base_dir is not None:
        try:
            rel = src.relative_to(base_dir)
        except ValueError:
            rel = None
    if rel is None:
        if root.is_dir():
            inner = src.relative_to(root)
            rel = inner if quarantine_dir == default_quarantine_dir(root) else Path(root.name) / inner
        else:
            rel = Path(src.name)
    dest = quarantine_dir / rel
    if not dest.exists():
        return dest
    n = 1
    while True:
        cand = dest.with_name("%s.%d" % (dest.name, n))
        if not cand.exists():
            return cand
        n += 1


def scrub_local(
    local,
    *,
    quarantine_dir: Optional[Path] = None,
    base_dir: Optional[Path] = None,
    log: Optional[Callable[[str], None]] = None,
    on_quarantine: Optional[Callable[[dict[str, str]], None]] = None,
) -> None:
    """Binary-safe scrub of `local` (file or directory).

    Text vs binary is decided by content (NUL byte or failed strict UTF-8 decode). Text files are scrubbed in place
    with `launch_guards.scrub_colab_tokens`; binary files are never rewritten (clean binaries stay byte-identical, while
    a token match, `binary_token_hit`, moves the file to quarantine and raises `BinaryDownloadRefused`). The
    quarantine is `quarantine_dir`, or with none a new `bare_quarantine_dir()` under the system temp dir. Unreadable
    files are removed."""
    root = Path(local)
    files = _iter_files(root)
    if not files:
        return
    qdir = Path(quarantine_dir) if quarantine_dir is not None else None
    quarantined: list[dict[str, str]] = []
    for p in files:
        try:
            data = p.read_bytes()
        except OSError:
            _unlink_quiet(p)
            continue
        if not is_binary_bytes(data):
            raw = io.StringIO(data.decode("utf-8"), newline=None).read()
            scrubbed = lg.scrub_colab_tokens(raw)
            if scrubbed != raw:
                try:
                    p.write_text(scrubbed, encoding="utf-8", newline="\n")
                except OSError:
                    _unlink_quiet(p)
            continue
        hit = binary_token_hit(data)
        if hit is None:
            continue
        if qdir is None:
            qdir = bare_quarantine_dir()
        dest = _quarantine_dest(p, root, qdir, base_dir)
        try:
            dest.parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(p), str(dest))
        except OSError:
            _unlink_quiet(p)
            raise
        entry = {"path": str(dest), "source": str(p), "pattern": hit}
        quarantined.append(entry)
        if log is not None:
            log(scrub("BINARY_DOWNLOAD_TOKEN: quarantined %s -> %s (pattern=%s)" % (p, dest, hit)))
        if on_quarantine is not None:
            on_quarantine(entry)
    if quarantined:
        raise BinaryDownloadRefused(quarantined)


def cli_from_env() -> Colab:
    """The CLI named by COLAB_RUNNER_CLI (split with shlex.split), else `colab`."""
    raw = os.environ.get("COLAB_RUNNER_CLI", "").strip()
    parts = shlex.split(raw) if raw else []
    return Colab(parts or ["colab"])


__all__ = [
    "BEARER_TOKEN_MIN", "BYTE_TOKEN_PATTERNS", "BackgroundCall", "BinaryDownloadRefused", "Colab", "CliResult",
    "KILL_WAIT_S", "TIMEOUT_RC", "bare_quarantine_dir", "binary_token_hit", "cli_from_env", "default_quarantine_dir",
    "env_preamble", "is_binary_bytes", "kernel_timeout", "kill_group", "scrub", "scrub_local", "token_shaped",
    "write_cell",
]
